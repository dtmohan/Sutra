"""Literal repetition remains an observation, not a new inference or action."""
import contextlib
import copy
import io
import json
import unittest
from unittest.mock import patch

from context_manager import canonical
from generation_progress import Progress
from karma_adapter import KarmaAdapter
from model_activity import ACTIVE, Activity
from review_budget import usage
import test_chat


REPETITION = ('Consider whether this point needs another explanation before producing the final answer. ' * 100)


class GenerationActivityTests(unittest.TestCase):
    def test_live_notice_survives_display_cap_and_finishes_without_changing_usage(self):
        output = io.StringIO()
        with patch.dict('os.environ', {'SUTRA_ACTIVITY': '1'}), contextlib.redirect_stderr(output):
            activity = Activity({'id': 'a'*32, 'role': 'architect', 'model': 'fixture'})
            activity.emit('start')
            for _ in range(4):
                for index in range(0, len(REPETITION), 91): activity.thinking(REPETITION[index:index+91])
            progress = activity.progress_snapshot(finish=True)
            activity.finish(True, {'input_tokens': 31, 'output_tokens': 250})
        events = [json.loads(line[12:]) for line in output.getvalue().splitlines()]
        notices = [event for event in events if event['kind'] == 'progress']
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]['kernel']['decision'], 'BRIDGE')
        self.assertTrue(notices[0]['kernel']['observation_only'])
        self.assertTrue(progress['repetition_indicator'])
        self.assertEqual(events[-1]['generation_progress'], progress)
        self.assertEqual(events[-1]['usage'], {'input_tokens': 31, 'output_tokens': 250})
        self.assertTrue(events[-1]['display_truncated'])
        self.assertEqual(sum(len(event.get('text', '')) for event in events), 16000)
        self.assertNotIn('Consider whether', canonical(notices[0]['generation_progress']))

    def test_malformed_snapshot_never_becomes_kernel_observation(self):
        observer = Progress(); observer.feed(REPETITION)
        valid = observer.snapshot()
        self.assertEqual(KarmaAdapter().generation_progress(valid)['decision'], 'BRIDGE')
        for key, value in (('received_chars', True), ('recent_duplicate_fraction', float('nan')),
                           ('repetition_indicator', 1), ('permissions', {'write': True})):
            invalid = dict(valid); invalid[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                KarmaAdapter().generation_progress(invalid)

    def test_observer_failure_does_not_truncate_the_original_stream(self):
        output = io.StringIO()
        with patch.dict('os.environ', {'SUTRA_ACTIVITY': '1'}), contextlib.redirect_stderr(output):
            activity = Activity({'id': 'b'*32})
            with patch.object(Progress, 'feed', side_effect=RuntimeError('diagnostic only')):
                activity.thinking('Original reasoning remains visible.')
            activity.finish(True, {})
        events = [json.loads(line[12:]) for line in output.getvalue().splitlines()]
        self.assertEqual(''.join(event.get('text', '') for event in events), 'Original reasoning remains visible.')
        self.assertNotIn('generation_progress', events[-1])
        self.assertEqual(events[-1]['status'], 'accepted')

    def test_intentional_cancellation_is_not_an_ordinary_diagnostic_error(self):
        from lifecycle import Interrupted
        activity = Activity({'id': 'c'*32})
        with patch.object(Progress, 'feed', side_effect=Interrupted('cancelled')):
            with self.assertRaises(Interrupted): activity.thinking('A cancelled stream.')
        with patch.object(Progress, 'finish', side_effect=Interrupted('cancelled')):
            with self.assertRaises(Interrupted): activity.progress_snapshot(finish=True)


class GenerationGatewayTests(unittest.TestCase):
    setUp = test_chat.ChatTests.setUp
    tearDown = test_chat.ChatTests.tearDown
    ask = test_chat.ChatTests.ask

    def test_repetition_record_is_immutable_and_does_not_retry_or_grant(self):
        class Repeating(test_chat.Fake):
            def call(self, *args):
                activity = ACTIVE.get()
                for index in range(0, len(REPETITION), 100): activity.thinking(REPETITION[index:index+100])
                return super().call(*args)
        before = usage(self.s, self.sid)
        contract = copy.deepcopy(self.s.current_contract(self.sid))
        model = Repeating()
        result = self.ask(provider=model)['result']
        self.assertTrue(result['admission']['admitted'])
        self.assertEqual(len(model.payloads), 1)
        observation = result['generation_progress_observation']
        self.assertEqual(observation['mode'], 'observe_only')
        self.assertTrue(observation['snapshot']['repetition_indicator'])
        self.assertEqual(observation['kernel']['decision'], 'BRIDGE')
        self.assertTrue(observation['kernel']['observation_only'])
        self.assertNotIn('generation_progress_observation', result['accepted_output'])
        after = usage(self.s, self.sid)
        self.assertEqual(after['used']['calls'], before['used']['calls'] + 1)
        self.assertEqual(after['limits'], before['limits'])
        self.assertEqual(self.s.current_contract(self.sid), contract)
        raw = self.s.db.execute('SELECT record_json FROM invocation_receipts WHERE invocation_id=?',
                               (result['invocation_id'],)).fetchone()[0]
        self.assertEqual(json.loads(raw)['generation_progress_observation'], observation)
        self.assertTrue(self.s.verify_invocations(self.sid))
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM host_actions').fetchone()[0], 0)


if __name__ == '__main__': unittest.main()

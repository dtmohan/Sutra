import contextlib
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import logit_sensor
from local_model import LocalModel
from logit_sensor import Sensor, baseline, json_classes, read
from paging import Budget


def lp(token, p, *alternatives):
    """One Ollama logprob entry; alternatives are (token, probability) pairs."""
    top = [{'token': token, 'logprob': math.log(p)}] + [{'token': t, 'logprob': math.log(q)} for t, q in alternatives]
    return {'token': token, 'logprob': math.log(p), 'bytes': list(token.encode()),
            'top_logprobs': sorted(top, key=lambda a: -a['logprob'])}


def done(content, logprobs=None, **extra):
    chunk = {'message': {'content': content}, 'done': True, 'done_reason': 'stop',
             'prompt_eval_count': 80, 'eval_count': 12}
    if logprobs is not None:
        chunk['logprobs'] = logprobs
    chunk.update(extra)
    return chunk


ANSWER = [  # {"verdict":"safe","count":3}
    ({'message': {'content': '{"'}, 'logprobs': [lp('{"', .99, ('{', .01))]}),
    ({'message': {'content': 'verdict'}, 'logprobs': [lp('verdict', .999, ('result', .001))]}),
    ({'message': {'content': '":"'}, 'logprobs': [lp('":"', .99, ('":', .01))]}),
    ({'message': {'content': 'safe'}, 'logprobs': [lp('safe', .52, ('unsafe', .47))]}),
    ({'message': {'content': '","'}, 'logprobs': [lp('","', .99, ('"}', .01))]}),
    ({'message': {'content': 'count'}, 'logprobs': [lp('count', .999, ('total', .001))]}),
    ({'message': {'content': '":'}, 'logprobs': [lp('":', .99, ('":"', .01))]}),
]
TEXT = '{"verdict":"safe","count":3}'


class Harness(unittest.TestCase):
    def invoke(self, chunks, setting='1'):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        model = LocalModel(self.root / 'calls', budget=Budget(response_reserve=512))
        response = io.BytesIO(b''.join(json.dumps(c).encode() + b'\n' for c in chunks))
        response.status = 200
        connection = MagicMock()
        connection.getresponse.return_value = response
        environment = {'SUTRA_LOGIT_SENSOR': setting} if setting is not None else {}
        with patch('invocation_gateway.consume_dispatch'), patch('http.client.HTTPConnection', return_value=connection), \
                patch.dict('os.environ', environment), contextlib.redirect_stderr(io.StringIO()):
            if setting is None:
                import os
                os.environ.pop('SUTRA_LOGIT_SENSOR', None)
            answer, record = model.call('call', 'system', {}, {})
        return answer, record, json.loads(connection.request.call_args.args[2])


class AttachmentTests(Harness):
    def test_disabled_sensor_leaves_request_record_and_storage_unchanged(self):
        for setting in (None, '', '0', 'yes'):
            answer, record, sent = self.invoke(ANSWER + [done('3}', [lp('3}', .9, ('4}', .1))])], setting)
            self.assertEqual(answer, {'verdict': 'safe', 'count': 3})
            self.assertNotIn('logprobs', sent)
            self.assertNotIn('top_logprobs', sent)
            self.assertNotIn('logit_sensor', record)
            self.assertFalse((self.root / 'logit_sensor.jsonl').exists())

    def test_enabled_sensor_requests_logprobs_and_flags_only_the_uncertain_value(self):
        answer, record, sent = self.invoke(ANSWER + [done('3}', [lp('3}', .9, ('4}', .1))])])
        self.assertEqual(answer, {'verdict': 'safe', 'count': 3})
        self.assertIs(sent['logprobs'], True)
        self.assertEqual(sent['top_logprobs'], 3)
        self.assertEqual(sent['options'], {'num_ctx': 8192, 'num_predict': 512, 'temperature': 0, 'seed': 42})
        summary = record['logit_sensor']
        self.assertTrue(summary['available'])
        self.assertEqual(summary['attribution'], 'text')
        self.assertTrue(summary['structured'])
        # Keys and punctuation are forced by the schema; only "safe" and 3 carry content.
        self.assertEqual(summary['counts'], {'answer': 8, 'value': 2, 'thinking': 0, 'unattributed': 0})
        self.assertEqual(summary['answer']['n'], 2)
        self.assertEqual(summary['answer']['low_margin'], 1)
        self.assertEqual(summary['answer']['low_margin_ratio'], .5)
        self.assertEqual([f['index'] for f in summary['flags']], [3])
        self.assertAlmostEqual(summary['flags'][0]['margin'], math.log(.52 / .47), places=3)
        self.assertTrue(summary['tripped'])
        self.assertEqual(summary['thresholds']['status'], 'bootstrap')
        self.assertNotIn('logprobs', record['response'])
        self.assertEqual(record['response']['eval_count'], 12)

    def test_numbers_only_mode_stores_no_model_text(self):
        _, record, _ = self.invoke(ANSWER + [done('3}', [lp('3}', .9, ('4}', .1))])])
        stored = (self.root / 'logit_sensor.jsonl').read_text()
        self.assertEqual(len(stored.splitlines()), 1)
        self.assertNotIn('safe', stored)
        self.assertNotIn('token', record['logit_sensor']['flags'][0])
        row = json.loads(stored)
        self.assertEqual((row['call_id'], row['model'], row['ok'], row['output_tokens']), ('call', 'qwen3:4b', True, 12))

    def test_tokens_mode_keeps_only_flagged_token_text(self):
        _, record, _ = self.invoke(ANSWER + [done('3}', [lp('3}', .9, ('4}', .1))])], 'tokens')
        flag = record['logit_sensor']['flags'][0]
        self.assertEqual((flag['token'], flag['runner_up']), ('safe', 'unsafe'))
        self.assertNotIn('verdict', (self.root / 'logit_sensor.jsonl').read_text())

    def test_confident_answer_does_not_trip(self):
        chunks = [dict(c, logprobs=[lp('safe', .97, ('unsafe', .02))]) if c['message']['content'] == 'safe' else c
                  for c in ANSWER]
        _, record, _ = self.invoke(chunks + [done('3}', [lp('3}', .9, ('4}', .1))])])
        self.assertFalse(record['logit_sensor']['tripped'])
        self.assertEqual(record['logit_sensor']['flags'], [])

    def test_thinking_is_measured_apart_from_the_answer(self):
        thinking = [{'message': {'thinking': 'Maybe'}, 'logprobs': [lp('Maybe', .4, ('Perhaps', .35))]},
                    {'message': {'thinking': ' unsafe.'}, 'logprobs': [lp(' unsafe', .5, (' safe', .45)), lp('.', .9, ('!', .1))]}]
        _, record, _ = self.invoke(thinking + ANSWER + [done('3}', [lp('3}', .9, ('4}', .1))])])
        summary = record['logit_sensor']
        self.assertEqual(summary['counts']['thinking'], 3)
        self.assertEqual(summary['thinking']['n'], 2)
        self.assertEqual(summary['thinking']['low_margin'], 2)
        self.assertEqual(summary['answer']['low_margin'], 1)
        self.assertEqual([f['index'] for f in summary['flags']], [6])

    def test_buffered_logprob_frames_are_attributed_by_text(self):
        # Ollama may emit logprob-only frames while its parser buffers visible text.
        frames = [{'message': {'content': ''}, 'logprobs': [lp('<think>', .9, ('{', .1)), lp('hm', .5, ('ok', .45)), lp('</think>', .9, ('.', .1))]}]
        frames += [{'message': {'content': ''}, 'logprobs': c['logprobs']} for c in ANSWER]
        _, record, _ = self.invoke(frames + [done(TEXT, [lp('3}', .9, ('4}', .1))])])
        summary = record['logit_sensor']
        self.assertEqual(summary['attribution'], 'text')
        self.assertEqual(summary['counts'], {'answer': 8, 'value': 2, 'thinking': 3, 'unattributed': 0})
        self.assertEqual([f['index'] for f in summary['flags']], [6])

    def test_older_server_without_logprobs_reports_unavailable(self):
        answer, record, sent = self.invoke([{'message': {'content': '{"a":'}}, done('1}')])
        self.assertEqual(answer, {'a': 1})
        self.assertTrue(sent['logprobs'])
        self.assertEqual(record['logit_sensor']['available'], False)
        self.assertNotIn('tripped', record['logit_sensor'])

    def test_malformed_sensor_data_never_blocks_an_answer(self):
        bad = [{'message': {'content': '{"a":'}, 'logprobs': [{'token': 7}, 'x', {'token': 'a', 'logprob': float('nan')},
                                                              {'token': 'a', 'logprob': True}, lp('{"a":', .9)]},
               done('1}', {'not': 'a list'})]
        answer, record, _ = self.invoke(bad)
        self.assertEqual(answer, {'a': 1})
        self.assertEqual(record['logit_sensor']['malformed'], 4)
        self.assertEqual(record['logit_sensor']['tokens'], 5)
        with patch.object(Sensor, 'summary', side_effect=ZeroDivisionError):
            answer, record, _ = self.invoke(ANSWER + [done('3}')])
        self.assertEqual(answer, {'verdict': 'safe', 'count': 3})
        self.assertEqual(record['logit_sensor'], {'v': 2, 'available': False, 'error': 'ZeroDivisionError'})

    def test_sensor_hook_failures_leave_model_response_available(self):
        for method in ('__init__','observe','finish'):
            with self.subTest(method=method),patch.object(Sensor,method,side_effect=OSError('diagnostic unavailable')):
                answer,record,_=self.invoke([done('{"ok":true}')])
                self.assertEqual(answer,{'ok':True});self.assertTrue(record['ok'])

    def test_withheld_call_is_logged_as_failed_without_becoming_accepted(self):
        with self.assertRaisesRegex(RuntimeError, 'Incomplete generation') as error:
            self.invoke(ANSWER + [done('3', [lp('3', .9, ('4', .1))], done_reason='length')])
        self.assertNotIn('answer', error.exception.call_record)
        self.assertTrue(error.exception.call_record['logit_sensor']['available'])
        self.assertFalse(read(self.root)[0]['ok'])


class HardwareJoinTests(Harness):
    def sample(self, age=1, **extra):
        import time
        return dict({'wall_time': time.time() - age, 'thermal_state': 2, 'memory_pressure_raw': 1,
                     'free_bytes': 9 * 1024 ** 3, 'inactive_bytes': 1024 ** 3, 'nested': {'drop': 1},
                     'long': 'x' * 41}, **extra)

    def invoke(self, chunks, setting='1', sample=None):
        if sample is None:
            return super().invoke(chunks, setting)
        original = logit_sensor.hardware
        def with_sample(root, now=None):
            Path(root).mkdir(parents=True, exist_ok=True)
            (Path(root) / 'telemetry.json').write_text(json.dumps(sample))
            return original(root, now)
        with patch.object(logit_sensor, 'hardware', with_sample):
            return super().invoke(chunks, setting)

    def test_rows_carry_bounded_hardware_samples_before_and_after(self):
        self.invoke(ANSWER + [done('3}', [lp('3}', .9, ('4}', .1))], eval_duration=5, load_duration=7)], sample=self.sample())
        row = read(self.root)[0]
        for moment in ('before', 'after'):
            snapshot = row['hardware'][moment]
            self.assertEqual((snapshot['available'], snapshot['stale'], snapshot['thermal_state']), (True, False, 2))
            self.assertNotIn('nested', snapshot)
            self.assertNotIn('long', snapshot)
            self.assertIsNone(snapshot['cpu_max_celsius'])
        self.assertEqual((row['eval_duration'], row['load_duration']), (5, 7))
        self.assertIsInstance(row['elapsed_s'], float)

    def test_stale_or_missing_telemetry_is_marked_and_never_blocks(self):
        answer, _, _ = self.invoke(ANSWER + [done('3}')], sample=self.sample(age=60))
        self.assertEqual(answer, {'verdict': 'safe', 'count': 3})
        self.assertTrue(read(self.root)[0]['hardware']['before']['stale'])
        answer, _, _ = self.invoke(ANSWER + [done('3}')])
        self.assertEqual(read(self.root)[0]['hardware'], {'before': {'available': False}, 'after': {'available': False}})
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'telemetry.json').write_text('[1]')
            self.assertEqual(logit_sensor.hardware(tmp), {'available': False})

    def test_disabled_sensor_does_not_read_telemetry(self):
        with patch.object(logit_sensor, 'hardware', side_effect=AssertionError('read')):
            answer, record, _ = Harness.invoke(self, ANSWER + [done('3}')], None)
        self.assertNotIn('logit_sensor', record)


class MeasurementTests(unittest.TestCase):
    def test_json_classes_separate_keys_values_and_structure(self):
        text = '{"k":"v\\"x","n":[1.5,true,null],"o":{"z":"}"}}'
        classes = ''.join(json_classes(text))
        self.assertEqual(len(classes), len(text))
        values = ''.join(ch for ch, c in zip(text, classes) if c == 'V')
        keys = ''.join(ch for ch, c in zip(text, classes) if c == 'K')
        self.assertEqual(values, 'v\\"x1.5truenull}')
        self.assertEqual(keys, 'knoz')

    def test_forced_token_is_counted_as_overridden_not_confident(self):
        sensor = Sensor()
        sensor.mode = '1'
        # The format forced "no" although the model preferred "yes".
        sensor.observe({'message': {'content': 'no'}, 'logprobs': [lp('no', .1, ('yes', .85))]})
        summary = sensor.summary('no')
        self.assertFalse(summary['structured'])
        self.assertEqual(summary['answer']['selected_below_alternative'], 1)
        self.assertLess(summary['answer']['min_margin'], 0)
        self.assertTrue(summary['tripped'])

    def test_memory_and_log_are_bounded(self):
        sensor = Sensor()
        sensor.mode = '1'
        with patch.object(logit_sensor, 'MAX_TOKENS', 4):
            for _ in range(6):
                sensor.observe({'message': {'content': 'a'}, 'logprobs': [lp('a', .9, ('b', .1))]})
        self.assertEqual(len(sensor.entries), 4)
        self.assertTrue(sensor.summary('')['truncated'])
        with tempfile.TemporaryDirectory() as tmp, patch.object(logit_sensor, 'MAX_LOG_BYTES', 600):
            path = Path(tmp) / 'logit_sensor.jsonl'
            for i in range(50):
                logit_sensor.append(path, {'i': i, 'pad': 'x' * 40})
            rows = read(tmp)
            self.assertLessEqual(path.stat().st_size, 600)
            self.assertEqual(rows[-1]['i'], 49)
            path.write_text(path.read_text() + 'damaged\n')
            self.assertEqual(read(tmp)[-1]['i'], 49)

    def test_split_utf8_bytes_preserve_value_token_alignment(self):
        first=lp('replacement',.7,('x',.2));first['bytes']=[0xc3]
        second=lp('replacement',.7,('x',.2));second['bytes']=[0xa9]
        sensor=Sensor();sensor.mode='1'
        entries=[lp('{"name":"',.99),first,second,lp('"}',.99)]
        sensor.observe({'message':{'content':''},'logprobs':entries})
        result=sensor.summary('{"name":"é"}')
        self.assertEqual(result['attribution'],'text')
        self.assertEqual(result['counts']['value'],2)
        self.assertEqual(result['counts']['unattributed'],0)
    def test_byte_cap_and_different_schema_cohorts(self):
        sensor=Sensor();sensor.mode='1'
        with patch.object(logit_sensor,'MAX_CAPTURE_BYTES',8):
            sensor.observe({'message':{'content':'x'},'logprobs':[lp('x',.9)]})
        self.assertEqual(sensor.entries,[]);self.assertTrue(sensor.summary('')['truncated'])
        row={'available':True,'model':'m','answer':{'low_margin_ratio':0,'min_margin':2},'cohort':{'schema_sha256':'a'}}
        other=dict(row,cohort={'schema_sha256':'b'})
        self.assertEqual(len(baseline([row,other])),2)

    def test_baseline_groups_wait_phase_measurements_by_model(self):
        row = lambda model, ratio, floor, tripped, ok=True: {
            'available': True, 'model': model, 'ok': ok, 'tripped': tripped,
            'answer': {'low_margin_ratio': ratio, 'min_margin': floor}}
        rows = [row('a', 0, 3.0, False), row('a', .2, .1, True), row('a', .5, -.4, True, False),
                row('b', 0, 2.0, False), {'available': False, 'model': 'a'}, {'available': True, 'model': 'a', 'answer': {'n': 0}}]
        result = baseline(rows)
        self.assertEqual(result['a | {}']['calls'], 3)
        self.assertEqual(result['a | {}']['tripped'], 2)
        self.assertEqual(result['a | {}']['failed_calls'], 1)
        self.assertEqual(result['a | {}']['low_margin_ratio']['max'], .5)
        self.assertEqual(result['b | {}']['min_margin']['p50'], 2.0)
        self.assertEqual(result['a | {}']['low_margin_ratio_by_thermal_state'], {'unknown': {'calls': 3, 'mean': .2333}})
        rows[0]['hardware'] = {'before': {'thermal_state': 0}}
        rows[1]['hardware'] = {'before': {'thermal_state': 2}}
        self.assertEqual(baseline(rows)['a | {}']['low_margin_ratio_by_thermal_state'],
                         {'0': {'calls': 1, 'mean': 0.0}, '2': {'calls': 1, 'mean': .2}, 'unknown': {'calls': 1, 'mean': .5}})


if __name__ == '__main__':
    unittest.main()

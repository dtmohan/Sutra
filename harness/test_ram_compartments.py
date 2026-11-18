import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import conversation_memory as memory
from context_manager import canonical
from invocation_gateway import InvocationLedger
from paging import PageFault, PagedContext
from ram_compartments import Compartments, CURRENT, decode
from review_budget import usage
import test_conversation as fixture


class CompartmentTests(unittest.TestCase):
    def setUp(self):
        self.cache = Compartments(1024 * 1024)

    def test_default_decode_has_no_retained_worker(self):
        self.assertIsNone(CURRENT.get())
        self.assertEqual(decode('page', '{"value":[1]}'), {'value': [1]})
        self.assertIsNone(CURRENT.get())

    def test_active_hit_returns_detached_nested_values(self):
        raw = '{"facts":{"label":"Cedar"},"rows":[{"value":4}]}'
        with self.cache.request('workspace'):
            first = decode(('session', 'page'), raw)
            first['facts']['label'] = 'changed'
            first['rows'][0]['value'] = 99
            second = decode(('session', 'page'), raw)
            self.assertEqual(second, json.loads(raw))
        observed = self.cache.snapshot()
        self.assertEqual(observed['active_hits'], 1)
        self.assertEqual(observed['misses'], 1)
        self.assertGreater(observed['active_bytes'], len(raw))
        self.assertGreater(observed['copy_ns'], 0)
        self.assertEqual(observed['last_request']['active_hits'], 1)

    def test_request_boundary_demotes_active_and_promotes_warm(self):
        with self.cache.request('workspace'):
            decode('old', '{"turn":1}')
        with self.cache.request('workspace'):
            self.assertEqual(self.cache.snapshot()['warm_entries'], 1)
            decode('new', '{"turn":2}')
        with self.cache.request('workspace'):
            self.assertEqual(decode('old', '{"turn":1}'), {'turn': 1})
        observed = self.cache.snapshot()
        self.assertEqual(observed['warm_hits'], 1)
        self.assertEqual(observed['active_entries'], 1)
        self.assertEqual(observed['warm_entries'], 1)

    def test_workspace_session_version_and_content_scopes_do_not_alias(self):
        with self.cache.request('workspace-a'):
            decode(('session-a', 'page', 1), '{"v":1}')
        for workspace, scope, raw in [
            ('workspace-b', ('session-a', 'page', 1), '{"v":1}'),
            ('workspace-a', ('session-b', 'page', 1), '{"v":1}'),
            ('workspace-a', ('session-a', 'page', 2), '{"v":1}'),
            ('workspace-a', ('session-a', 'page', 1), '{"v":2}'),
        ]:
            with self.cache.request(workspace):
                self.assertEqual(decode(scope, raw), json.loads(raw))
                self.assertEqual(self.cache.snapshot()['active_hits'], 0)
                self.assertEqual(self.cache.snapshot()['warm_hits'], 0)
        self.assertEqual(self.cache.snapshot()['misses'], 5)

    def test_memory_trim_evicts_warm_before_active(self):
        with self.cache.request('workspace'):
            decode('old', '{"turn":1}')
        with self.cache.request('workspace'):
            decode('current', '{"turn":2}')
            active_bytes = self.cache.snapshot()['active_bytes']
            self.cache.resize(active_bytes)
            observed = self.cache.snapshot()
            self.assertEqual(observed['warm_entries'], 0)
            self.assertEqual(observed['active_entries'], 1)
            decode('current', '{"turn":2}')
        self.assertEqual(self.cache.snapshot()['active_hits'], 1)
        self.assertEqual(self.cache.snapshot()['evictions'], 1)
        self.assertLessEqual(self.cache.snapshot()['retained_bytes'], active_bytes)

    def test_warm_eviction_is_lru(self):
        raw = '{"value":"same-length"}'
        with self.cache.request('workspace'):
            decode('a', raw)
            one_entry = self.cache.snapshot()['active_bytes']
            decode('b', raw)
        self.cache.resize(2 * one_entry)
        with self.cache.request('workspace'):
            decode('c', raw)
        with self.cache.request('workspace'):
            before = self.cache.snapshot()
            decode('b', raw)
            self.assertEqual(self.cache.snapshot()['warm_hits'], before['warm_hits'] + 1)
            decode('a', raw)
            self.assertEqual(self.cache.snapshot()['misses'], before['misses'] + 1)

    def test_active_copies_can_be_evicted_without_losing_returned_data(self):
        with self.cache.request('workspace'):
            first = decode('a', '{"value":"retained by caller"}')
            self.cache.resize(0)
            self.assertEqual(first['value'], 'retained by caller')
            self.assertEqual(self.cache.snapshot()['retained_bytes'], 0)
            self.assertEqual(decode('a', '{"value":"retained by caller"}'), first)
        self.assertEqual(self.cache.snapshot()['bypasses'], 1)

    def test_oversized_values_bypass_without_evicting_small_entries(self):
        self.cache.resize(4096)
        with self.cache.request('workspace'):
            decode('small', '{"value":1}')
            before = self.cache.snapshot()
            raw = json.dumps({'text': 'x' * 8192})
            self.assertEqual(decode('large', raw), json.loads(raw))
            after = self.cache.snapshot()
            self.assertEqual(after['active_entries'], before['active_entries'])
            self.assertEqual(after['evictions'], before['evictions'])
            self.assertEqual(after['bypasses'], 1)

    def test_object_overhead_is_included_in_capacity(self):
        raw = '[0,1,2,3,4,5,6,7,8,9]'
        self.cache.resize(len(raw) * 2)
        with self.cache.request('workspace'):
            decode('page', raw)
        self.assertEqual(self.cache.snapshot()['retained_bytes'], 0)
        self.assertEqual(self.cache.snapshot()['bypasses'], 1)

    def test_valid_deep_json_bypasses_optional_retention_when_copying_would_recurse(self):
        raw = '[' * 700 + '0' + ']' * 700
        with self.cache.request('workspace'):
            value = decode('page', raw)
        depth = 0
        while isinstance(value, list):
            depth += 1
            value = value[0]
        self.assertEqual((depth, value), (700, 0))
        self.assertEqual(self.cache.snapshot()['retained_bytes'], 0)
        self.assertEqual(self.cache.snapshot()['bypasses'], 1)

    def test_request_failure_restores_context_and_records_failed_decode_time(self):
        with self.assertRaises(json.JSONDecodeError):
            with self.cache.request('workspace'):
                decode('bad', '{')
        self.assertIsNone(CURRENT.get())
        observed = self.cache.snapshot()
        self.assertEqual(observed['active_entries'], 0)
        self.assertEqual(observed['misses'], 1)
        self.assertGreater(observed['decode_ns'], 0)

    def test_pressure_clear_and_resize_do_not_change_durable_state(self):
        with tempfile.TemporaryDirectory() as directory:
            durable = Path(directory) / 'checkpoint'
            durable.write_bytes(b'Exact task state and consumed budget')
            with self.cache.request('workspace'):
                decode('a', '{"value":1}')
            with self.cache.request('workspace'):
                decode('b', '{"value":2}')
            self.cache.clear('memory_pressure')
            self.cache.resize(0)
            self.assertEqual(self.cache.snapshot()['retained_bytes'], 0)
            self.assertEqual(self.cache.snapshot()['clear_reason'], 'memory_pressure')
            self.assertEqual(durable.read_bytes(), b'Exact task state and consumed budget')

    def test_disabled_compartments_measure_decoding_without_retention(self):
        self.cache.resize(0)
        with self.cache.request('workspace'):
            for _ in range(2):
                decode('page', '{"value":1}')
        observed = self.cache.snapshot()
        self.assertEqual(observed['misses'], 2)
        self.assertEqual(observed['bypasses'], 2)
        self.assertEqual(observed['retained_bytes'], 0)

    def test_resize_during_decode_cannot_exceed_new_capacity(self):
        import ram_compartments
        original = ram_compartments._detach

        def shrink(value):
            self.cache.resize(0)
            return original(value)

        with patch('ram_compartments._detach', side_effect=shrink):
            with self.cache.request('workspace'):
                self.assertEqual(decode('page', '{"value":1}'), {'value': 1})
        self.assertEqual(self.cache.snapshot()['retained_bytes'], 0)

    def test_nested_requests_and_invalid_allocations_are_rejected(self):
        for value in (-1, True, 1.5, '1024'):
            with self.assertRaises(ValueError):
                self.cache.resize(value)
        with self.cache.request('workspace'):
            with self.assertRaises(RuntimeError):
                with self.cache.request('nested'):
                    self.fail('Nested request should not enter')
        self.assertIsNone(CURRENT.get())


class PageValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = PagedContext(self.root / 'state.sqlite')
        self.addCleanup(self.store.close)
        self.store.create('session')
        self.cache = Compartments(1024 * 1024)

    def test_warm_payload_still_checks_live_source_contents(self):
        source = self.root / 'module.py'
        source.write_text('value = 1\n')
        self.store.import_file('session', 'source', source)
        with self.cache.request('workspace'):
            self.assertIn('value = 1', self.store.read_page('session', 'source').payload['text'])
        source.write_text('value = 2\n')
        with self.cache.request('workspace'):
            with self.assertRaises(PageFault) as caught:
                self.store.read_page('session', 'source')
        self.assertEqual(caught.exception.code, 'source_changed')

    def test_page_scope_is_transient_and_returned_payloads_are_independent(self):
        page = self.store.put_page('session', 'page', 'notes', 'Page', 'text')
        with self.cache.request('workspace'):
            first = self.store.read_page('session', 'page')
            first.payload['text'] = 'mutated'
            self.assertEqual(self.store.read_page('session', 'page').payload['text'], 'text')
            self.assertEqual(first, page)
        columns = [row[1] for row in self.store.db.execute('PRAGMA table_info(pages)')]
        self.assertNotIn('cache_scope', columns)

    def test_superseded_and_expired_pages_keep_existing_faults(self):
        first = self.store.put_page('session', 'page', 'notes', 'Page', 'old', ttl_s=1)
        with self.cache.request('workspace'):
            self.store.read_page('session', 'page').payload
        self.store.put_page('session', 'page', 'notes', 'Page', 'new', expected_version=1)
        with self.cache.request('workspace'):
            with self.assertRaises(PageFault) as caught:
                self.store.read_page('session', 'page', 1)
            self.assertEqual(caught.exception.code, 'superseded')
            second = self.store.read_page('session', 'page')
            self.assertEqual(second.payload['text'], 'new')
            with self.assertRaises(PageFault) as expired:
                self.store.read_page('session', 'page', now=second.committed_at + second.ttl_s)
            self.assertEqual(expired.exception.code, 'expired')


class ConversationValidationTests(unittest.TestCase):
    setUp = fixture.ConversationTests.setUp
    tearDown = fixture.ConversationTests.tearDown
    ask = fixture.ConversationTests.ask

    def test_warm_history_survives_reload_and_model_switch_without_budget_reset(self):
        cache = Compartments(4 * 1024 * 1024)
        first = self.ask('Remember Cedar CED724. No network. Keep the kernel unchanged.')
        sid, parent = first['session'], first['result']['invocation_id']
        before = usage(self.s, sid)
        contract = self.s.origin(sid)
        with cache.request(str(self.storage)):
            original = memory.history(self.s, sid, parent)
            memory.materialize(self.s, sid, original)
        self.s.close()
        self.s = InvocationLedger(self.storage / 'state.sqlite')
        with cache.request(str(self.storage)):
            self.assertEqual(memory.history(self.s, sid, parent), original)
            self.assertGreater(cache.snapshot()['warm_hits'], 0)
            self.assertEqual(usage(self.s, sid), before)
            model = fixture.Fake('large')
            continued = self.ask('Recall the tag and constraints.', parent, model, 'high')
        self.assertTrue(continued['result']['admission']['admitted'])
        self.assertEqual(self.s.origin(sid), contract)
        self.assertIn('CED724', canonical(model.payloads[0]['memory']))
        self.assertFalse(model.payloads[0]['contract']['constraints']['network_allowed'])
        self.assertFalse(model.payloads[0]['contract']['constraints']['edits_allowed'])
        self.assertEqual(usage(self.s, sid)['used']['calls'], before['used']['calls'] + 1)

    def test_quarantine_is_checked_after_history_has_warmed(self):
        cache = Compartments(4 * 1024 * 1024)
        first = self.ask('Remember Cedar CED724.')
        sid, parent = first['session'], first['result']['invocation_id']
        with cache.request(str(self.storage)):
            self.assertTrue(memory.history(self.s, sid, parent))
        self.s.quarantine_invocation(sid, parent, 'Host quarantine')
        with cache.request(str(self.storage)):
            with self.assertRaises(PageFault):
                memory.history(self.s, sid, parent)

    def test_forks_reuse_only_their_verified_ancestry(self):
        cache = Compartments(4 * 1024 * 1024)
        first = self.ask('Remember shared ancestor.')
        sid, parent = first['session'], first['result']['invocation_id']
        a = self.ask('Branch A marker A-724.', parent)
        b = self.ask('Branch B marker B-821.', parent)
        with cache.request(str(self.storage)):
            memory.history(self.s, sid, a['result']['invocation_id'])
        with cache.request(str(self.storage)):
            rows = memory.history(self.s, sid, b['result']['invocation_id'])
        self.assertIn('B-821', canonical(rows))
        self.assertNotIn('A-724', canonical(rows))

    def test_released_0454_conversation_restores_under_cache_without_action_authority(self):
        cache = Compartments(4 * 1024 * 1024)
        current = self.s.logic_hash
        previous = memory.RELEASE_POLICIES['0.45.4']
        self.s.logic_hash = previous
        with patch('governance.policy_hash', return_value=previous):
            first = self.ask('Remember released Cedar CED724. Do not edit files.')
        self.s.logic_hash = current
        sid, parent = first['session'], first['result']['invocation_id']
        before = usage(self.s, sid)
        with cache.request(str(self.storage)):
            self.assertTrue(memory.history(self.s, sid, parent)[0]['legacy'])
        with cache.request(str(self.storage)):
            model = fixture.Fake('large')
            result = self.ask('Continue the remembered request.', parent, model, 'high')
        self.assertTrue(result['result']['admission']['admitted'])
        self.assertTrue(model.payloads[0]['memory']['legacy_data_restored'])
        self.assertFalse(model.payloads[0]['contract']['constraints']['edits_allowed'])
        self.assertEqual(usage(self.s, sid)['used']['calls'], before['used']['calls'] + 1)
        with self.assertRaises(PageFault):
            self.s.load_result(sid, parent)


if __name__ == '__main__':
    unittest.main()

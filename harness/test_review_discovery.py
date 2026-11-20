"""Real tool decisions over complete catalogs and truthful source spans."""
import copy
import unittest
from unittest.mock import patch
from invocation_gateway import consume_dispatch
from paging import Budget
from review_role import constraint_rows
import repository_tools as repo
import review_discovery as discovery
import test_orchestration as fixture
from test_chat import Fake


class ToolModel:
    def __init__(self, model, actions, calls):
        self.model, self.actions, self.calls = model, actions, calls
        self.budget = Budget(65536, 2048, 256)

    def call(self, cid, system, p, schema):
        consume_dispatch(cid, self.model, system, p, schema)
        index = len(self.calls); self.calls.append(copy.deepcopy(p))
        action = copy.deepcopy(self.actions[min(index, len(self.actions)-1)])
        kind = action['kind']
        if kind in ('report', 'clarify'): action.update(target='', query='', line=1)
        elif kind == 'read': action.update(query='', line=action.get('line', 1))
        elif kind == 'search': action.update(target='', line=1)
        else: action.update(target='', query='')
        by_path = {}
        for entry in reversed(p['evidence']): by_path.setdefault(entry['path'], entry)
        findings = [{'page_id': e['id'], 'quote': next(line.strip()[:240] for line in e['text'].splitlines() if line.strip()),
                     'note': 'Observed this source body.'} for e in list(by_path.values())[:3]]
        value = {'task_id': p['contract']['task_id'], 'constraints': constraint_rows(p['contract']),
                 'summary': 'Trace actual code using the returned tool results.', 'findings': findings,
                 'completed': [], 'remaining': ['Inspect unresolved caller and callee behavior.'],
                 'tool': action, 'edges': [], 'complete': False}
        return value, {'call_id': cid, 'ok': True, 'answer': value,
                       'response': {'prompt_eval_count': 400, 'eval_count': 80}}


class DiscoveryToolTests(unittest.TestCase):
    tearDown = fixture.OrchestrationTests.tearDown
    step = fixture.OrchestrationTests.step
    until = fixture.OrchestrationTests.until

    def setUp(self):
        fixture.OrchestrationTests.setUp(self)
        self.prompt = 'Explain the architecture and real tool request flow. ' + self.prompt
        self.calls = []; self.captured = []; self.actions = [{'kind': 'clarify'}]

    def provider(self, stage):
        if stage != 'synthesis': return fixture.OrchestrationTests.provider(self, stage)
        model = Fake(fixture.MODELS[self.tier]); model.budget = Budget(65536, 4096, 256)
        model.discovery_provider = ToolModel(model.model, self.actions, self.calls)
        self.models.append((stage, model)); return model

    def discover(self):
        current = self.until('SYNTHESIS'); advance = discovery.advance
        def watch(*args, **kwargs):
            result = advance(*args, **kwargs)
            self.captured.append((copy.deepcopy(args[2]), result)); return result
        with patch.object(discovery, 'advance', side_effect=watch):
            for _ in range(20):
                current = self.step(current)
                self.assertEqual(current['state'], 'SYNTHESIS', current.get('stop'))
                if self.captured[-1][1]['complete']: return current, self.captured[-1][0]
        self.fail('Deterministic tool sequence did not stop')

    def test_full_catalog_list_search_read_and_pinned_context(self):
        for i in range(24): (self.ws/f'aaa{i:02}.py').write_text(f'VALUE = {i}\n')
        (self.ws/'zz_last.py').write_text('DEEP_TARGET_ANCHOR = 724\n')
        self.actions = [{'kind': 'list', 'line': 1}, {'kind': 'list', 'line': 21},
                        {'kind': 'search', 'query': 'DEEP_TARGET_ANCHOR'}, {'kind': 'read', 'target': 'zz_last.py'},
                        {'kind': 'read', 'target': 'part1.py'}, {'kind': 'report'}]
        _, p = self.discover()
        self.assertEqual(len(self.calls), 6)
        self.assertGreater(len(self.calls[0]['repository']['catalog']), 20)
        self.assertIn('zz_last.py', self.calls[0]['repository']['catalog'])
        self.assertTrue(self.calls[0]['initial_excerpts'])
        self.assertIn('CED724', str(self.calls[0]['memory']['pinned_user_statements']))
        self.assertIn('zz_last.py', self.calls[2]['repository']['last_tool']['files'])
        self.assertTrue(any('DEEP_TARGET_ANCHOR' in e['text'] for e in self.calls[3]['evidence']))
        self.assertEqual(p['discovery_coverage']['stop_reason'], 'evidence_complete')
        self.assertEqual(p['discovery_coverage']['tool_results'], 5)
        imported = discovery.load(self.s, self.meta['active'], p)
        self.assertEqual(set(imported), {'zz_last.py', 'part1.py'})
        self.assertIn('DEEP_TARGET_ANCHOR', imported['zz_last.py']['text'])
        self.assertFalse(p['discovery_coverage']['exhaustive_semantic_review'])

    def test_large_read_uses_actual_source_reservation_and_legacy_stays_bounded(self):
        body = 'def part0():\n    return 0\n' + ''.join('# detail '+str(i)+' '+'x'*80+'\n' for i in range(900))
        (self.ws/'part0.py').write_text(body)
        self.assertGreater(len(body.encode()), repo.MAX_FILE)
        with self.assertRaises(repo.SourceReadLimitExceeded): repo.read_bytes(self.ws, 'part0.py')
        self.actions = [{'kind': 'read', 'target': 'part0.py'}, {'kind': 'read', 'target': 'part1.py'}, {'kind': 'report'}]
        _, p = self.discover(); sid = self.meta['active']
        records = discovery._tool_receipts(self.s, sid, discovery._accepted(self.s, sid,
            discovery.history(self.s, sid, p['evidence_discovery']['key'])))
        self.assertEqual(records[0]['result']['status'], 'ok')
        self.assertEqual(records[0]['operation']['task_cost']['source_bytes'], len(body.encode()))
        self.assertIn('return 0', discovery.load(self.s, sid, p)['part0.py']['text'])

    def test_search_does_not_overwrite_earned_read_or_become_full_read(self):
        self.actions = [{'kind': 'read', 'target': 'part0.py'}, {'kind': 'search', 'query': 'return'}, {'kind': 'clarify'}]
        _, p = self.discover(); imported = discovery.load(self.s, self.meta['active'], p)
        self.assertEqual(set(imported), {'part0.py'})
        self.assertTrue(imported['part0.py']['text'].startswith('def '))
        self.assertIn('\n', imported['part0.py']['text'])
        self.assertEqual(imported['part0.py']['line'], 1)
        self.assertEqual(p['discovery_coverage']['tool_results'], 2)
        self.assertEqual(p['discovery_coverage']['stop_reason'], 'clarification')

    def test_source_page_limit_stops_partially_without_charging_unexecuted_read(self):
        self.actions = [{'kind': 'read', 'target': 'part0.py'}]
        execute = repo.execute
        def source_limit(*args, **kwargs):
            if kwargs.get('invocation_id'):
                raise repo.SourceReadLimitExceeded('Fixture exceeds the scoped source-page allocation')
            return execute(*args, **kwargs)
        with patch.object(repo, 'execute', side_effect=source_limit):
            _, p = self.discover()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(p['discovery_coverage']['stop_reason'], 'operation_failed')
        self.assertEqual(p['discovery_coverage']['tool_results'], 0)
        self.assertEqual(discovery.load(self.s, self.meta['active'], p), {})
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM repository_ops WHERE "
            "json_extract(record_json,'$.invocation_id') IS NOT NULL").fetchone()[0], 0)


if __name__ == '__main__': unittest.main()

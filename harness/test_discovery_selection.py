"""Selection-only native wire replies retain governed architect authority."""
import copy
import json
import unittest
from unittest.mock import patch

from context_manager import InvalidUpdate, canonical, sha
from invocation_gateway import consume_dispatch
from model_packet import build, descriptor, bind_output
from paging import Budget
import architecture_role as role
import context_window as window
import repository_tools as repo
import review_discovery as discovery
import test_orchestration as fixture
from test_chat import Fake


class SelectionWireModel:
    model_interface = 1

    def __init__(self, model, actions, calls, fault=None):
        self.model, self.actions, self.calls, self.fault = model, actions, calls, fault
        self.budget = Budget(65536, 2048, 256)

    def call(self, cid, system, payload, schema):
        packet = build(system, payload, schema)
        consume_dispatch(cid, self.model, system, payload, schema, packet=packet)
        if set(packet['schema']['properties']) != {'tool', 'summary', 'remaining'}:
            raise AssertionError('The actual model interface still requests report fields')
        tool = copy.deepcopy(self.actions[min(len(self.calls), len(self.actions)-1)])
        kind = tool['kind']
        if kind in ('report', 'clarify'): tool.update(target='', query='', line=1)
        elif kind == 'read': tool.update(query='', line=tool.get('line', 1))
        elif kind == 'search': tool.update(target='', line=1)
        else: tool.update(target='', query='')
        # This is the complete model reply. Host fields are never supplied here.
        raw = {'tool': tool, 'summary': 'Follow actual source and tool results.',
               'remaining': ['Inspect missing caller and callee links.']}
        if self.fault: self.fault(raw, payload)
        self.calls.append((copy.deepcopy(payload), copy.deepcopy(packet), copy.deepcopy(raw)))
        return raw, {'call_id': cid, 'ok': True, 'answer': copy.deepcopy(raw),
                     'response': {'prompt_eval_count': 400, 'eval_count': 80}}


class SelectionWireTests(unittest.TestCase):
    tearDown = fixture.OrchestrationTests.tearDown
    step = fixture.OrchestrationTests.step
    until = fixture.OrchestrationTests.until

    def setUp(self):
        fixture.OrchestrationTests.setUp(self)
        self.prompt = 'Explain the architecture and real tool request flow. ' + self.prompt
        self.calls = []; self.captured = []; self.fault = None
        self.actions = [{'kind': 'read', 'target': 'part0.py'},
                        {'kind': 'read', 'target': 'part1.py'}, {'kind': 'report'}]

    def provider(self, stage):
        if stage != 'synthesis': return fixture.OrchestrationTests.provider(self, stage)
        model = Fake(fixture.MODELS[self.tier]); model.budget = Budget(65536, 4096, 256)
        model.discovery_provider = SelectionWireModel(model.model, self.actions, self.calls, self.fault)
        self.models.append((stage, model)); return model

    def discover(self):
        current = self.until('SYNTHESIS'); advance = discovery.advance
        def watch(*args, **kwargs):
            result = advance(*args, **kwargs)
            self.captured.append((copy.deepcopy(args[2]), result)); return result
        with patch.object(discovery, 'advance', side_effect=watch):
            for _ in range(12):
                current = self.step(current)
                self.assertEqual(current['state'], 'SYNTHESIS', current.get('stop'))
                if self.captured[-1][1]['complete']: return current, self.captured[-1][0]
        self.fail('Selection fixture did not reach a bounded discovery outcome')

    def test_first_read_needs_no_findings_and_gateway_binds_only_empty_report_fields(self):
        _, p = self.discover(); sid = self.meta['active']
        self.assertEqual(len(self.calls), 3)
        first, packet, raw = self.calls[0]
        self.assertEqual(first['evidence'], [])
        self.assertTrue(first['initial_excerpts'])
        self.assertEqual(first['discovery_selection_version'], 3)
        self.assertTrue(all(delivery['discovery_tool_prompt_version'] == 1 for delivery, _, _ in self.calls))
        self.assertEqual(set(raw), {'tool', 'summary', 'remaining'})
        self.assertNotIn('Quote exact', packet['messages'][0]['content'])
        self.assertNotIn('findings', packet['messages'][0]['content'])
        self.assertEqual(p['discovery_coverage']['read_files'], ['part0.py', 'part1.py'])
        self.assertEqual(p['discovery_coverage']['stop_reason'], 'evidence_complete')
        self.assertFalse(p['discovery_coverage']['exhaustive_semantic_review'])
        rows = self.s.db.execute("SELECT r.record_json FROM invocation_receipts r JOIN invocations i "
            "ON i.id=r.invocation_id WHERE json_extract(i.record_json,'$.role')='architect' ORDER BY i.rowid").fetchall()
        for row, (_, _, original) in zip(rows, self.calls):
            receipt = json.loads(row[0]); accepted = receipt['proposal']
            self.assertEqual(receipt['status'], 'accepted')
            self.assertEqual(receipt['model_response_hash'], sha(canonical(original)))
            for key in ('findings', 'edges', 'completed'):
                self.assertEqual(receipt['host_fields'][key], [])
                self.assertEqual(accepted[key], [])
            self.assertIs(accepted['complete'], False)
        self.assertEqual(set(discovery.load(self.s, sid, p)), {'part0.py', 'part1.py'})
        self.assertTrue(self.s.verify_invocations(sid))

    def test_unknown_report_fields_reject_entire_reply_without_salvaging_valid_tool(self):
        self.fault = lambda raw, _: raw.update(findings=[{'page_id': 'part0.py', 'quote': 'ellipsized...'}])
        _, p = self.discover(); sid = self.meta['active']
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(p['discovery_coverage']['stop_reason'], 'proposal_rejected')
        self.assertEqual(p['discovery_coverage']['read_files'], [])
        self.assertEqual(p['discovery_coverage']['tool_results'], 0)
        for _, result in self.captured[:3]:
            self.assertFalse(result['result']['admission']['admitted'])
            self.assertIn('unexpected fields', ' '.join(result['result']['admission']['errors']))
        self.assertFalse(any(record['operation'].get('invocation_id') for record in repo.records(self.s, sid)))

    def test_out_of_scope_read_remains_rejected_before_a_repository_effect(self):
        self.fault = lambda raw, _: raw['tool'].update(target='../private.py')
        _, p = self.discover()
        self.assertEqual(p['discovery_coverage']['stop_reason'], 'proposal_rejected')
        self.assertEqual(p['discovery_coverage']['tool_results'], 0)

    def test_unknown_cursor_remains_rejected_before_a_repository_effect(self):
        self.actions = [{'kind': 'read', 'target': 'part0.py'}]
        self.fault = lambda raw, _: raw['tool'].update(cursor={'line': 1, 'column': 0, 'sha256': '0'*64})
        _, p = self.discover()
        self.assertEqual(p['discovery_coverage']['stop_reason'], 'proposal_rejected')
        self.assertEqual(p['discovery_coverage']['tool_results'], 0)
        self.assertEqual(len(self.calls), 3)
        for _, result in self.captured[:3]:
            self.assertIn('Unknown read continuation', ' '.join(result['result']['admission']['errors']))

    def test_search_hits_and_initial_snapshot_do_not_certify_actual_read_readiness(self):
        self.actions = [{'kind': 'search', 'query': 'def '}, {'kind': 'report'}]
        _, p = self.discover()
        self.assertEqual(len(self.calls), 4)  # One search, three rejected readiness proposals.
        self.assertGreater(len(self.calls[1][0]['evidence']), 1)
        self.assertEqual(p['discovery_coverage']['read_files'], [])
        self.assertEqual(p['discovery_coverage']['tool_results'], 1)
        self.assertEqual(p['discovery_coverage']['stop_reason'], 'proposal_rejected')
        self.assertEqual(discovery.load(self.s, self.meta['active'], p), {})


class SelectionCompatibilityTests(unittest.TestCase):
    def test_absent_selection_flag_keeps_0511_schema_packet_charge_and_physical_proof(self):
        fixture = LEGACY_FIXTURE
        p = fixture['payload']; fmt = role.schema(p); packet = build(role.SYSTEM, p, fmt)
        self.assertNotIn('discovery_selection_version', p)
        self.assertEqual(sha(canonical(fmt)), fixture['golden']['schema_hash'])
        self.assertEqual(sha(canonical(packet)), fixture['golden']['packet_hash'])
        self.assertEqual(descriptor(packet, 256), fixture['golden']['descriptor'])
        self.assertEqual(Budget(32768, 1024, 256).charge(role.SYSTEM, p, fmt),
                         fixture['golden']['descriptor']['input_reservation'])
        budget = Budget(**fixture['budget']); profile = budget.context_window
        identity = {'server_model': profile['model'], 'server_started': profile['started'],
                    'server_context_tokens': profile['context_tokens']}
        with patch.object(window, '_rpc', side_effect=AssertionError('Replay must not contact a tokenizer')):
            window.validate_proof(fixture['proof'], budget, role.SYSTEM, p, fmt, identity)
        self.assertIn('findings', packet['schema']['properties'])
        self.assertIn('edges', packet['schema']['properties'])
        new = dict(p, discovery_selection_version=1)
        reduced = build(role.SYSTEM, new, role.schema(new))
        self.assertEqual(set(reduced['schema']['properties']), {'tool', 'summary', 'remaining'})
        with self.assertRaises(InvalidUpdate):
            window.validate_proof(fixture['proof'], budget, role.SYSTEM, new, role.schema(new), identity)
        self.assertLess(descriptor(reduced, 256)['input_reservation'], descriptor(packet, 256)['input_reservation'])
        for raw in ({'tool': {'kind': 'read', 'target': 'a.py', 'query': '', 'line': 1}, 'summary': 'Read it.'},
                    {'tool': {'kind': 'read', 'target': 'a.py', 'query': '', 'line': 1},
                     'summary': 'Read it.', 'remaining': [], 'completed': []}):
            with self.assertRaises(InvalidUpdate): bind_output('architect', new, role.schema(new), raw)


LEGACY_FIXTURE = {'budget': {'context_limit': 4096,
            'context_profile': 2,
            'context_window': {'context_tokens': 8192,
                               'model': 'fixture-native',
                               'port': 19600,
                               'provider': 'strata',
                               'started': 111,
                               'template_sha256': 'f5861d01a7e37a19520ec945e1378e0199876502461de4faea1a6a3cf318b12f',
                               'version': 1},
            'reservation_limit': 20000,
            'response_reserve': 512,
            'template_reserve': 479},
 'golden': {'descriptor': {'estimator': 'model_packet_utf8_upper_bound_v1',
                           'input_reservation': 4105,
                           'packet_hash': 'b24690fcf0393075e96a53243bf3694c079c65ceb55f5f91e6be1a9d45ec0dcd',
                           'prompt_utf8_bytes': 2547,
                           'role': 'architect',
                           'schema_allowance': 1302,
                           'template_allowance': 256,
                           'version': 1},
            'packet_hash': 'b24690fcf0393075e96a53243bf3694c079c65ceb55f5f91e6be1a9d45ec0dcd',
            'schema_hash': '5be92a179bbd7a089bda95bf8e6d2ba66c0ec8f487751ae3d5bf3ec626fe3212'},
 'payload': {'contract': {'constraints': {},
                          'goal': 'Trace the real request flow.',
                          'task_id': 'fixture-task'},
             'discovery_notes_unverified': [],
             'evidence': [],
             'evidence_discovery_version': 4,
             'initial_excerpts': {'a.py': 'def start():\n    return work()\n'},
             'phase': 'explore',
             'repository': {'budget': {'decision': 'ALLOW'},
                            'catalog': ['a.py', 'b.py'],
                            'last_tool': None,
                            'operations_remaining': 9,
                            'total_files': 2},
             'request': 'Trace the real request flow.'},
 'proof': {'basis': 'strata_count_tokens_v1',
           'capacity': {'adapter': 'smriti',
                        'context_limit': 4096,
                        'decision': 'ALLOW',
                        'input_charge': 500,
                        'k': 0.15,
                        'omega': 0.037353515625,
                        'output_reserve': 520,
                        'profile': 'context-capacity-v1',
                        'profile_version': 1,
                        'sigma': 0.0,
                        'sigma_clause': 1.0},
           'context_slack_tokens': 8,
           'context_tokens': 4096,
           'output_reserve': 512,
           'packet_sha256': 'b24690fcf0393075e96a53243bf3694c079c65ceb55f5f91e6be1a9d45ec0dcd',
           'prompt_tokens': 500,
           'thinking': False,
           'version': 1,
           'window': {'context_tokens': 8192,
                      'model': 'fixture-native',
                      'port': 19600,
                      'provider': 'strata',
                      'started': 111,
                      'template_sha256': 'f5861d01a7e37a19520ec945e1378e0199876502461de4faea1a6a3cf318b12f',
                      'version': 1},
           'wire_sha256': 'b38e1fdb7ea04cb1f2063430aeb195bb900475712a7b0b0f31ec993256e1bf24'}}


if __name__ == '__main__': unittest.main()

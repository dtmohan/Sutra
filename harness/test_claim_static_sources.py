"""Claim review gets the exact source statements delivered to its writer."""
import copy
import json
import unittest
from unittest.mock import patch

import auto_review
import chat_runtime as chat
import claim_pages as pages
import context_window
import model_packet
import report_review as review
import review_report
import test_chat as fixture
from context_manager import InvalidUpdate, canonical, sha
from paging import Budget, PageFault
from test_context_window_recovery import CountService


def source_fixture():
    return {'contract': {'task_id': 'task', 'goal': 'Explain the architecture.', 'constraints': {}},
            'request': 'Explain the architecture.', 'constraint_echo': [], 'workspace': '/fixture',
            'snapshot_receipts': ['snapshot'], 'claim_source_version': 2, 'report_schema_version': 2,
            'excerpts': {'entry.py': 'def start():\n'},
            'excerpt_locations': {'entry.py': {'start_line': 1, 'end_line': 2, 'partial_file': True}},
            'map_bytes': 4096,
            'facts': {'nodes': [{'id': 'start', 'path': 'entry.py', 'line': 1, 'name': 'start'},
                                {'id': 'run', 'path': 'entry.py', 'line': 20, 'name': 'run'}],
                      'calls': [{'id': 'call', 'path': 'entry.py', 'line': 8, 'text': 'return run()',
                                 'expression': 'run', 'source': 'start', 'target': 'run',
                                 'relation': 'static_call_reference'}],
                      'diagnostics': [], 'diagnostic_count': 0,
                      'meaning': 'Static references, not runtime execution proof.'}}


class StaticSourceInventoryTests(unittest.TestCase):
    def test_only_actually_projected_statements_enter_inventory_and_model_page(self):
        source = source_fixture()
        writer = model_packet.build(chat.SYSTEM, source, chat.schema(source))
        wire = json.loads(writer['messages'][1]['content'])
        self.assertEqual(model_packet.projected_source_statements(source),
                         [{key: call[key] for key in ('path', 'line', 'text')}
                          for call in wire['facts']['calls']])
        evidence = pages.inventory('a' * 32, source)
        static = {eid: row for eid, row in evidence.items() if row.get('kind') == 'static_source_statement'}
        self.assertEqual(len(static), 1)
        row = next(iter(static.values()))
        self.assertEqual((row['path'], row['text'], row['start_line'], row['end_line']),
                         ('entry.py', 'return run()', 8, 8))
        self.assertNotIn(row['text'], source['excerpts']['entry.py'])
        self.assertIn('not proof of runtime', row['meaning'])
        self.assertEqual(evidence, pages.inventory('a' * 32, source))
        self.assertFalse(set(evidence) & set(pages.inventory('b' * 32, source)))
        draft = {'report': {'overview': 'start statically calls run.'}}
        claims = {'overview': {'text': draft['report']['overview'], 'paths': ['entry.py']}}
        plan = {'id': 'p' * 32, 'pages': pages.partition(claims, evidence, lambda _: True)}
        packet = pages.packet(source, draft, 'a' * 32, plan, 0, claims, evidence, {}, False)
        body = json.loads(model_packet.build(review.SYSTEM, packet, review.schema(packet))['messages'][1]['content'])
        self.assertEqual(body['source_evidence'], evidence)
        self.assertIn('not proof of runtime', body['review_scope']['meaning'])

    def test_paged_out_host_statements_and_diagnostics_stay_excluded(self):
        source = source_fixture(); source['map_bytes'] = 1000
        hidden = {'id': 'hidden', 'path': 'hidden.py', 'line': 40, 'text': 'hidden(' + 'x' * 800 + ')',
                  'expression': 'hidden', 'source': 'start', 'target': 'run', 'relation': 'static_call_reference'}
        source['facts']['calls'].append(hidden)
        source['facts']['diagnostics'] = [{'path': 'diagnostic.py', 'line': 1, 'message': 'not source text ' * 100}]
        source['facts']['diagnostic_count'] = 1
        wire = json.loads(model_packet.build(chat.SYSTEM, source, chat.schema(source))['messages'][1]['content'])
        self.assertEqual(len(wire['facts']['calls']), 1)
        self.assertEqual(wire['facts']['omitted_sample_calls'], 1)
        evidence = pages.inventory('a' * 32, source)
        self.assertFalse(any(row['path'] in ('hidden.py', 'diagnostic.py') for row in evidence.values()))
        self.assertEqual(pages.source_paths(source), ['entry.py'])
        self.assertNotIn('hidden.py', review_report.schema(source)['properties']['flow']['items']['properties']['paths']['items']['enum'])
        original = pages.source_hash(source)
        source['facts']['calls'][1]['text'] = 'hidden(' + 'y' * 800 + ')'
        self.assertEqual(pages.source_hash(source), original)
        self.assertEqual(pages.inventory('a' * 32, source), evidence)

    def test_source_hash_binds_delivered_text_line_and_projection_not_hidden_host_facts(self):
        source = source_fixture(); initial = pages.source_hash(source)
        draft = {'report': {'overview': 'The source statically calls run.'}}
        claims = {'overview': {'paths': ['entry.py'], 'text': draft['report']['overview']}}
        evidence = pages.inventory('a' * 32, source)
        plan = {'id': 'p' * 32, 'version': pages.VERSION, 'candidate_id': 'a' * 32,
                'candidate_hash': sha(canonical(draft)), 'source_hash': initial,
                'contract': source['contract'], 'strict': False, 'attempts_per_page': 2,
                'pages': pages.partition(claims, evidence, lambda _: True)}
        pages.validate_plan(plan, 'a' * 32, draft, source, claims, evidence)
        for field, value in (('text', 'return other()'), ('line', 9), ('path', 'other.py')):
            changed = copy.deepcopy(source); changed['facts']['calls'][0][field] = value
            self.assertNotEqual(pages.source_hash(changed), initial)
            with self.assertRaisesRegex(InvalidUpdate, 'binding changed'):
                pages.validate_plan(plan, 'a' * 32, draft, changed, claims,
                                    pages.inventory('a' * 32, changed))
        source['excerpts']['entry.py'] += '    return run()\n'
        source['facts']['calls'][0]['line'] = 2
        self.assertFalse(any(row.get('kind') == 'static_source_statement'
                             for row in pages.inventory('a' * 32, source).values()))
        # The same text at another source line cannot borrow this location.
        source['facts']['calls'][0]['line'] = 8
        self.assertTrue(any(row.get('kind') == 'static_source_statement'
                            for row in pages.inventory('a' * 32, source).values()))

    def test_long_static_statement_keeps_exact_text_in_bounded_evidence_units(self):
        source = source_fixture()
        source['map_bytes'] = 16384
        text = 'return call(' + 'λ' * 1500 + ')'
        source['facts']['calls'][0]['text'] = text
        static = [row for row in pages.inventory('a' * 32, source).values()
                  if row.get('kind') == 'static_source_statement']
        self.assertEqual(''.join(row['text'] for row in static), text)
        self.assertTrue(all(len(row['text']) <= 512 for row in static))
        self.assertTrue(all(row['start_line'] == row['end_line'] == 8 for row in static))

    def test_delivered_fact_path_outside_excerpts_is_in_writer_and_modern_claim_scope(self):
        source = source_fixture()
        source['facts']['calls'][0]['path'] = 'fact_only.py'
        for node in source['facts']['nodes']:
            node['path'] = 'fact_only.py'
        source['workspace_map'] = {'files': ['entry.py', 'fact_only.py']}
        self.assertNotIn('fact_only.py', source['excerpts'])
        self.assertEqual(pages.source_paths(source), ['entry.py', 'fact_only.py'])
        report = {'overview': 'The delivered statement statically calls run.',
                  'components': [{'name': 'Caller', 'responsibility': 'Statically calls run.', 'paths': ['fact_only.py']}],
                  'flow': [{'description': 'The delivered statement statically calls run.', 'paths': ['fact_only.py']}],
                  'strengths': [], 'risks': [], 'next_steps': []}
        spec = review_report.schema(source)
        self.assertIn('fact_only.py', spec['properties']['flow']['items']['properties']['paths']['items']['enum'])
        review_report.validate(source, {'report': report})
        claims = review.claims(report, pages.source_paths(source))
        self.assertIn('fact_only.py', claims['overview']['paths'])
        evidence = pages.inventory('a' * 32, source)
        plan = pages.partition(claims, evidence, lambda _: True)
        self.assertTrue(any(evidence[eid]['path'] == 'fact_only.py'
                            for page in plan if 'flow/0' in page['claims'] for eid in page['evidence']))
        legacy = dict(source); legacy.pop('claim_source_version')
        self.assertEqual(pages.source_paths(legacy), ['entry.py'])
        with self.assertRaises(InvalidUpdate):
            review_report.validate(legacy, {'report': report})

    def test_absent_flag_preserves_legacy_inventory_hash_schema_and_physical_proof(self):
        source = source_fixture(); source.pop('claim_source_version')
        evidence = pages.inventory('a' * 32, source)
        self.assertEqual(evidence, LEGACY_EVIDENCE)
        self.assertEqual(pages.source_hash(source), LEGACY_SOURCE_HASH)
        changed = copy.deepcopy(source); changed['facts']['calls'][0]['text'] = 'return other()'
        self.assertEqual(pages.source_hash(changed), LEGACY_SOURCE_HASH)
        fmt = chat.schema(source); wire = model_packet.build(chat.SYSTEM, source, fmt)
        opted = dict(source, claim_source_version=2)
        self.assertEqual(fmt, chat.schema(opted))
        self.assertEqual(wire, model_packet.build(chat.SYSTEM, opted, chat.schema(opted)))
        counter = CountService(); profile = counter.descriptor()
        budget = Budget(8192, 1024, 256, context_profile=2, reservation_limit=20000, context_window=profile)
        context_window._counts.clear()
        with patch.object(context_window, '_rpc', counter.rpc):
            proof = context_window.check(budget, chat.SYSTEM, source, fmt, thinking=False)
        identity = {'server_model': profile['model'], 'server_started': profile['started'],
                    'server_context_tokens': profile['context_tokens']}
        with patch.object(context_window, '_rpc', side_effect=AssertionError('Historical proof replay must be offline')):
            context_window.validate_proof(proof, budget, chat.SYSTEM, source, fmt, identity)


class StaticSourcePublicationTests(unittest.TestCase):
    setUp = fixture.ChatTests.setUp
    tearDown = fixture.ChatTests.tearDown

    def test_fresh_static_body_outside_excerpt_can_support_publication_and_reject_source_change(self):
        source = ('from leaf import run\ndef start():\n' +
                  ''.join('    # Source detail %03d remains outside the small delivered body.\n' % n for n in range(150)) +
                  '    return run()\n')
        (self.ws/'entry.py').write_text(source)
        auto_review.step(self.s, self.meta, self.ws, self.storage, {'explain': False}, provider=fixture.Fake())
        writer, reviewer = fixture.Fake(), fixture.Fake('reviewer')
        claim = 'The start function statically calls run to obtain its result.'

        def compose(value, packet):
            statements = model_packet.projected_source_statements(packet)
            self.assertTrue(any(row['path'] == 'entry.py' and 'return run()' in row['text'] for row in statements))
            self.assertNotIn('return run()', packet['excerpts']['entry.py'])
            value['report'].update(overview=claim,
                components=[{'name': 'Start', 'responsibility': claim, 'paths': ['entry.py']}],
                flow=[{'description': claim, 'paths': ['entry.py']}], strengths=[], risks=[], next_steps=[])

        def judge(value, packet):
            for key, row in packet['claims'].items():
                ids = [eid for eid, entry in packet['source_evidence'].items()
                       if entry.get('kind') == 'static_source_statement' and entry['path'] in row['paths']
                       and 'return run()' in entry['text']]
                value['claims'][key] = {'verdict': 'supported' if ids else 'insufficient_evidence',
                    'reason': 'Exact delivered static source statement.' if ids else 'This page lacks the static statement.',
                    'evidence_ids': ids}

        writer.fault, reviewer.review_fault = compose, judge
        out = chat.answer(self.s, self.meta, self.ws, self.storage,
                          {'prompt': 'Explain the architecture of start.', 'models': fixture.MODELS, 'tier': 'low'},
                          provider=writer, review_provider=reviewer)
        self.assertTrue(out['result']['admission']['admitted'], out)
        self.assertIn(claim, out['report_markdown'])
        self.assertTrue(any(item['path'] == 'entry.py' and item['start_line'] == source.count('\n')
                            for item in out['result']['accepted_output']['claim_evidence']))
        self.assertTrue(self.s.verify_invocations(self.sid))
        candidate_id = out['result']['invocation_id']
        (self.ws/'entry.py').write_text(source.replace('return run()', 'return bad()'))
        with self.assertRaises((InvalidUpdate, PageFault)):
            review.candidate(self.s, self.sid, candidate_id)


LEGACY_EVIDENCE = {'eba87639326d5': {'path': 'entry.py', 'text': 'def start():\n',
                   'excerpt_start': 0, 'excerpt_end': 13, 'start_line': 1, 'end_line': 2}}
LEGACY_SOURCE_HASH = 'eb7b28e08cdb37f7225abbdb8f739198f573caa79c0b256f42d23e87f1dd778e'


if __name__ == '__main__':
    unittest.main()

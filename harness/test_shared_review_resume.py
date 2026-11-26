"""One durable prose review can use newly selected providers for unfinished stages."""

import copy
import json
import unittest
from contextlib import nullcontext
from unittest.mock import patch

from context_manager import ContextBudgetExceeded, InvalidUpdate
from invocation_gateway import InvocationLedger
from monitoring import ResourceWait
from paging import Budget
import orchestration as machine
import orchestration_flow as flow
import review_budget
import review_discovery
import section_synthesis
import test_orchestration as fixture
import test_parent_section_recovery as adoption
import test_resource_routing as fallback_fixture


class ReviewProvider:
    def __init__(self, model, calls):
        self.model = model
        self.calls = calls
        self.budget = Budget(65536, 4096, 256)
        self.context_metadata = {'model_info': {'fixture.context_length': 65536},
                                 'thinking': {'values': [False, True]}}

    def call(self, cid, system, packet, schema):
        self.calls.append((self.model, copy.deepcopy(packet)))
        index = packet['section_index']
        if packet['section_role'] == 'section_writer':
            eid = packet['section_sources'][index][0]
            paragraph = {'sentences': [{'text': f'The checked file is part of the project. [{eid}]',
                                        'sources': [eid]}]}
            if index == section_synthesis.ASSESSMENT_INDEX:
                paragraph['kind'] = 'finding'
            value = {'sections': {str(index): [paragraph]}}
        else:
            value = {'verdicts': {str(i): 'supported' for i, _ in enumerate(packet['claim']['sentences'])}}
        return value, {'call_id': cid, 'ok': True,
                       'response': {'done': True, 'prompt_eval_count': 200, 'eval_count': 32}}


class SharedReviewResumeTests(unittest.TestCase):
    setUp = fixture.OrchestrationTests.setUp
    tearDown = fixture.OrchestrationTests.tearDown
    until = fixture.OrchestrationTests.until
    provider = fixture.OrchestrationTests.provider
    step = adoption.ParentSectionRecoveryTests.step

    def test_section_memory_preserves_verified_pins_with_a_large_host_record(self):
        from chat_runtime import conversation_pages
        from conversation import answer, workspace_handoff
        from model_packet import memory_view
        from test_conversation import Fake
        from vscode_bridge import handle

        options = {'models': fixture.MODELS, 'tier': 'low'}
        directive = 'Remember the Cedar architecture and keep it read only.'
        first = answer(self.s, self.meta, self.root, dict(options, prompt=directive), Fake())
        review = 'Explain the architecture and suggest next steps for Cedar.'
        parent = first['result']['invocation_id']
        followup = answer(self.s, self.meta, self.root, dict(options, prompt=review, parent=parent), Fake())
        iid = followup['result']['invocation_id']
        workspace = handle(self.root, self.ws, {'op': 'auto_new', 'goal': review,
                                                'roots': ['.'], 'conversation_id': iid})
        sid = workspace['session']
        pin = self.s.pinned(sid)
        source = {
            'contract': pin, 'workspace': str(self.ws), 'request': review,
            'conversation_handoff': workspace_handoff(self.s, sid, iid, review),
            'excerpts': {'part0.py': 'def part0():\n    return 0\n'}, 'constraint_echo': [],
            'review_intent_version': 1, 'workspace_map': {'host_record': 'x' * (52 * 1024)},
        }
        budget = Budget(32768, 2048, 256)
        with self.assertRaisesRegex(ContextBudgetExceeded, 'required request and response format'):
            conversation_pages(self.s, source, budget, materialize=True)
        before = review_budget.usage(self.s, sid)
        saved = conversation_pages(self.s, source, budget, materialize=True, prose_source=True)
        delivered = memory_view(saved)
        self.assertIn(directive, [row['quote'] for row in delivered['pinned_user_statements']])
        self.assertEqual(delivered['recent_turns'][0]['user'], review)
        self.s.close()
        self.s = InvocationLedger(self.root / 'state.sqlite')
        self.assertEqual(conversation_pages(self.s, source, budget, prose_source=True), saved)
        self.assertEqual(review_budget.usage(self.s, sid), before)
        self.assertEqual(self.s.pinned(sid), pin)

    def prepare_review(self, models, version=section_synthesis.SHARED_PROSE_VERSION):
        self.ws = self.ws.resolve()
        self.root = self.root.resolve()
        self.meta['workspace'] = str(self.ws)
        self.prompt = 'Review the project architecture and suggest next steps. ' + self.prompt
        (self.ws / 'README.md').write_text('# Cedar\n\nThis workspace contains example programs.\n')
        (self.ws / 'VALIDATION.md').write_text('# Validation\n\nThe return value examples are unchecked.\n')
        waiting = self.until('SYNTHESIS')
        self.provider = lambda _: None
        for _ in range(10):
            waiting = self.step(waiting, models=models)
            _, saved = flow.info(self.s, waiting['flow_id'])
            if saved.get('section_plan'):
                self.assertEqual(saved['section_plan']['version'], version)
                return waiting, saved
        self.fail('The shared plan was never committed')

    def test_shared_review_resumes_on_other_providers_without_refetch_or_refund(self):
        self.check_resume_across_providers(section_synthesis.SHARED_PROSE_VERSION)

    def test_legacy_shared_review_resumes_with_its_original_reviewer_reserve(self):
        self.check_resume_across_providers(4)

    def check_resume_across_providers(self, version):
        models = dict(fixture.MODELS, high='sutra:remote/fixture')
        calls = []
        blocked = False

        def provider_for(_, model, **options):
            if blocked and model == models['low']:
                raise ResourceWait({'available': False, 'admitted': False, 'blocker': 'measurement_unavailable',
                                    'reason': 'Fixture resource wait', 'runtime': {'model': model}})
            provider = ReviewProvider(model, calls)
            if model == models['high']:
                provider.context_metadata['thinking'] = {'values': ['low', 'medium']}
            return provider

        def legacy_plan(store, session, source, model, *, draft_mode):
            return section_synthesis._prose_plan(
                store, session, source, model, draft_mode=draft_mode, version=version)[0]

        check_stage = section_synthesis.request_check
        checked_models = []

        def check_selected(store, session, role, packet, model=None):
            if model == models['high']:
                with self.assertRaisesRegex(InvalidUpdate, 'model'):
                    check_stage(store, session, role, packet, model=models['low'])
                checked_models.append(model)
            return check_stage(store, session, role, packet, model=model)

        planner = (patch.object(section_synthesis, 'make_prose_plan', side_effect=legacy_plan)
                   if version == 4 else nullcontext())
        with planner, patch('api_runtime.provider_for', side_effect=provider_for), \
                patch('provider_resources.require_resources', return_value={}), \
                patch('provider_resources.recover', return_value=None), \
                patch('section_synthesis.request_check', side_effect=check_selected):
            waiting, saved = self.prepare_review(models, version)
            sid = waiting['binding']['session']
            fid = waiting['flow_id']
            execution = waiting['execution_id']
            task = self.s.pinned(sid)
            permissions = machine.node(self.s, execution)['permissions']
            plan = copy.deepcopy(saved['section_plan'])
            source = copy.deepcopy(saved['section_source'])
            self.assertNotIn('model', plan)
            evidence = review_discovery.checkpoint_receipts(self.s, sid, source)
            self.assertTrue(evidence)

            drafted = self.step(waiting, models=models)
            [(model, packet)] = calls
            self.assertEqual((model, packet['section_role']), (models['low'], 'section_writer'))
            charged = review_budget.usage(self.s, sid)
            blocked = True
            paused = self.step(drafted, models=models)
            self.assertEqual(paused['state'], 'PAUSED')
            self.assertEqual(paused['stop']['status'], 'resource_wait')
            self.assertEqual(review_budget.usage(self.s, sid), charged)
            self.s.close()
            self.s = InvocationLedger(self.root / 'state.sqlite')

            with self.assertRaisesRegex(InvalidUpdate, 'user-armed'):
                self.step(paused, event='resume', tier='high', models=models, armed=False)
            with self.assertRaisesRegex(InvalidUpdate, 'permissions changed'):
                self.step(paused, event='resume', tier='high', models=models,
                          binding=dict(paused['binding'], contract_hash='0' * 64))
            resumed = self.step(paused, event='resume', tier='high', models=models)
            self.assertEqual((resumed['flow_id'], resumed['execution_id']), (fid, execution))
            self.assertEqual(resumed['binding'], paused['binding'])
            self.assertEqual(review_budget.usage(self.s, sid), charged)

            current = self.step(resumed, tier='high', models=models)
            self.assertEqual(len(current['report_sections']), 1)
            [_, (model, verification)] = calls
            self.assertEqual((model, verification['section_role']), (models['high'], 'section_reviewer'))
            self.assertEqual(verification['reasoning']['requested'], 'low')
            self.assertEqual(verification['memory_budget']['response_reserve'],
                             256 if version == 4 else 1024)
            self.assertEqual(verification['evidence'],
                             {eid: packet['evidence'][eid] for eid in verification['evidence']})
            self.assertEqual(verification['contract'], packet['contract'])
            self.assertEqual(verification['pinned'], packet['pinned'])
            self.assertEqual(verification['claim']['sentences'][0]['text'],
                             'The checked file is part of the project.')

            for _ in range(10):
                current = self.step(current, tier='mid', models=models)
                if current['state'] == 'DONE':
                    break

        self.assertEqual(current['state'], 'DONE', current.get('stop'))
        self.assertEqual((current['flow_id'], current['execution_id']), (fid, execution))
        self.assertEqual(self.s.pinned(sid), task)
        self.assertEqual(machine.node(self.s, execution)['permissions'], permissions)
        self.assertEqual(len(current['report_sections']), 3)
        _, final = flow.info(self.s, fid)
        self.assertEqual(final['section_plan'], plan)
        self.assertEqual(final['section_source'], source)
        self.assertEqual(review_discovery.checkpoint_receipts(self.s, sid, source), evidence)
        self.assertEqual(checked_models, [models['high']])
        routes = [event['data']['section_route'] for event in machine.events(self.s, fid)
                  if event['kind'] == 'REPORT_SECTION_ROUTE']
        self.assertEqual([route['model'] for route in routes], [models[k] for k in ('low', 'high', 'mid')])
        with self.assertRaisesRegex(InvalidUpdate, 'committed route'):
            flow.checked_section_route(source, dict(routes[-1], model=models['low']))
        end = review_budget.usage(self.s, sid)
        self.assertEqual(end['limits'], charged['limits'])
        self.assertEqual(end['used']['operations'], charged['used']['operations'])
        self.assertEqual(end['used']['source_bytes'], charged['used']['source_bytes'])
        self.assertEqual(end['used']['calls'] - charged['used']['calls'], 5)
        from budget_accounting import used
        rows = [(json.loads(env), json.loads(record)) for env, record in self.s.db.execute(
            'SELECT i.record_json,r.record_json FROM invocations i JOIN invocation_receipts r ON i.id=r.invocation_id '
            'WHERE i.session=? ORDER BY i.rowid DESC LIMIT 5', (sid,))]
        self.assertEqual(end['used']['tokens'] - charged['used']['tokens'],
                         sum(used(env, record) for env, record in rows))
        self.assertTrue(self.s.verify_invocations(sid))
        self.assertTrue(self.s.audit_valid())

    def test_shared_auto_fallback_reuses_draft_after_capacity_refusal(self):
        calls = []
        resource_fixture = fallback_fixture.ResourceRoutingTests

        def provider_for(_, model, **options):
            if model == fixture.MODELS['high']:
                raise resource_fixture.stop(self, 'high')
            return ReviewProvider(model, calls)

        with patch('api_runtime.provider_for', side_effect=provider_for), \
                patch('provider_resources.require_resources', return_value={}), \
                patch('runtime_memory.api', return_value=resource_fixture.tags(self)):
            waiting, saved = self.prepare_review(fixture.MODELS)
            draft = self.step(waiting)
            sid = draft['binding']['session']
            charged = review_budget.usage(self.s, sid)
            fallback = self.step(draft, tier='auto')
            self.assertEqual(fallback['state'], 'SYNTHESIS')
            self.assertEqual(fallback['resource_fallback']['model'], fixture.MODELS['mid'])
            self.assertEqual(fallback['resource_fallback']['bounded_switch']['decision'], 'ALLOW')
            self.assertEqual(review_budget.usage(self.s, sid), charged)
            self.assertEqual(len(calls), 1)
            advanced = self.step(fallback, tier='auto')
        self.assertEqual(advanced['execution_id'], draft['execution_id'])
        self.assertEqual(advanced['binding'], draft['binding'])
        _, final = flow.info(self.s, draft['flow_id'])
        self.assertEqual(final['section_source'], saved['section_source'])
        self.assertEqual(final['section_route']['mode'], 'resource-mid')
        self.assertEqual(calls[-1][0], fixture.MODELS['mid'])
        self.assertEqual(calls[-1][1]['section_role'], 'section_reviewer')
        self.assertEqual(review_budget.usage(self.s, sid)['used']['calls'], charged['used']['calls'] + 1)

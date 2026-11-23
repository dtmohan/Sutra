import copy,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from context_manager import InvalidUpdate,ContextBudgetExceeded,canonical
from invocation_gateway import InvocationLedger
from test_chat import Fake as Review
from test_conversation import Fake as Conversation,MODELS
from paging import PageFault
import orchestration as m
import orchestration_flow as flow
import review_budget

class Child(Review):
    def call(self,cid,system,p,schema):
        result,record=super().call(cid,system,p,schema)
        result['branch_id']=p['branch']['id'];return result,record

class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name);self.ws=self.base/'repo';self.ws.mkdir();self.root=self.base/'store';self.root.mkdir()
        for i in range(4):(self.ws/f'part{i}.py').write_text(f'def part{i}():\n    return {i}\n')
        self.meta={'workspace':str(self.ws)};self.s=InvocationLedger(self.root/'state.sqlite');self.models=[];self.tier='low'
        self.prompt='Review the project. Remember project Cedar, ticket CED724. Keep this read only. No network or new dependencies.'
    def tearDown(self):self.s.close();self.tmp.cleanup()
    def provider(self,stage):
        model=MODELS[self.tier]
        if stage=='conversation':
            op=getattr(self,'operation','read')
            work={'operation':'read','scope':'workspace','paths':[]} if op=='read' else {'operation':op,'paths':['part0.py']}
            p=Conversation(model,lambda r,p:r.update(intent='workspace',facts=['Remember project Cedar, ticket CED724.'],workspace_request=work))
        else:p=Child(model) if stage=='subagent' else Review(model)
        self.models.append((stage,p));return p
    def step(self,out=None,**kw):
        request={'models':MODELS,'tier':self.tier,'armed':True}
        if out:request.update(event='step',flow_id=out['flow_id'],cursor=out['cursor'],binding=out.get('binding') or out.get('bound_task'))
        else:request.update(event='start',prompt=self.prompt)
        request.update(kw)
        return flow.handle(self.s,self.meta,self.ws,self.root,request,self.provider)
    def until(self,phase,out=None):
        for _ in range(40):
            out=self.step(out)
            if out['state']==phase:return out
        self.fail('Flow never reached '+phase+': '+str(out))
    def children(self):return self.until('CHILDREN')
    def test_explicit_codebase_review_needs_no_provider_before_workspace_access(self):
        self.ws = self.ws.resolve()  # The VS Code bridge supplies a canonical workspace path.
        prompt = 'can you review the codebase please'
        chat_key = 'a' * 64
        with patch.object(self, 'provider', side_effect=AssertionError('No routing model needed')), \
                patch('repository_tools.execute', side_effect=AssertionError('Workspace access is required')):
            waiting = self.step(prompt=prompt, chat_key=chat_key, tier='auto', armed=False)
            self.assertEqual(waiting['state'], 'NEED_ARM')
            self.assertIsNone(waiting['conversation_id'])
            self.assertNotIn('conversation', waiting)
            node, data = flow.info(self.s, waiting['flow_id'])
            self.assertEqual(node['prompt'], prompt)
            self.assertEqual(data['permission'], {'operation': 'read', 'scope': 'workspace', 'paths': []})
            self.assertEqual(data['source_roots'], ['.'])
            session = data['conversation_session']
            self.assertTrue(all(value == 0 for value in review_budget.usage(self.s, session)['used'].values()))
            classified = m.events(self.s, waiting['flow_id'])[-1]
            self.assertEqual(classified['kind'], 'EXPLICIT_WORKSPACE_REVIEW')
            self.assertEqual(classified['kernel']['decision'], 'ALLOW')
            self.s.close()
            self.s = InvocationLedger(self.root / 'state.sqlite')
            with self.assertRaisesRegex(InvalidUpdate, 'user-armed'):
                self.step(waiting, chat_key=chat_key, armed=False)
        with patch.object(self, 'provider', side_effect=AssertionError('No routing model needed')):
            bound = self.step(waiting, chat_key=chat_key, tier='high', armed=True)
        self.assertEqual(bound['state'], 'WORKSPACE')
        self.assertEqual(bound['flow_id'], waiting['flow_id'])
        task = self.s.snapshot(bound['binding']['session'])
        self.assertEqual(task['goal'], prompt)
        self.assertEqual(task['constraints']['allowed_paths'], ['.'])
        for permission in ('edits_allowed', 'tests_allowed', 'network_allowed'):
            self.assertFalse(task['constraints'][permission])
        self.assertTrue(all(value == 0 for value in review_budget.usage(self.s, session)['used'].values()))

    def test_standalone_html_codebase_review_publishes_original_source_citations(self):
        import os
        from shutil import which

        node = os.environ.get('KARMA_NODE') or which('node')
        if not node:
            self.skipTest('Node is required for inline JavaScript mapping')
        compiler = Path(__file__).resolve().parent.parent / 'vendor' / 'typescript' / 'typescript.js'
        self.ws = self.ws.resolve().parent / 'single-page'
        self.ws.mkdir()
        self.prompt = 'can you review the codebase please'
        code = 'function start() { return leaf(); }'
        source = ('<!DOCTYPE html>\n<title>Demo</title>\n<script>\n'
                  'function leaf() { return 7; }\n' + code + '\nstart();\n</script>\n')
        (self.ws / 'index.html').write_text(source)
        chat_key = 'a' * 64

        def cite_inline_script(result, packet):
            self.assertIn(code, packet['excerpts']['index.html'])
            result['citations'] = [{'path': 'index.html', 'quote': code}]

        def provider(role):
            self.assertNotEqual(role, 'conversation')
            return Review(MODELS['low'], cite_inline_script)
        self.provider = provider

        waiting = self.step(chat_key=chat_key, armed=False)
        self.assertEqual(waiting['state'], 'NEED_ARM')
        with self.assertRaisesRegex(InvalidUpdate, 'user-armed'):
            self.step(waiting, chat_key=chat_key, armed=False)
        out = waiting
        with patch.dict(os.environ, KARMA_NODE=node, KARMA_TYPESCRIPT=str(compiler)):
            for _ in range(25):
                out = self.step(out, chat_key=chat_key)
                if out['state'] in ('PAUSED', 'DONE'):
                    break
        self.assertEqual(out['state'], 'DONE', out.get('stop'))
        self.assertEqual(out['flow_id'], waiting['flow_id'])
        result = out['data']['result']
        self.assertTrue(result['admission']['admitted'], result['admission'])
        self.assertIn({'path': 'index.html', 'quote': code}, result['accepted_output']['citations'])
        self.assertEqual((self.ws / 'index.html').read_text(), source)
        sid = out['binding']['session']
        self.assertTrue(self.s.verify_invocations(sid))
        self.assertTrue(self.s.audit_valid())

    def test_prebinding_catalog_pause_recovers_the_same_task_at_a_higher_ceiling(self):
        from vscode_bridge import handle

        self.ws = self.ws.resolve()
        ask = 'can you review the codebase please'
        common = {'op': 'orchestrate', 'tier': 'auto', 'chat_key': 'a' * 64}

        def request(event, previous, **fields):
            return handle(self.root, self.ws, dict(common, event=event, armed=True,
                flow_id=previous['flow_id'], cursor=previous['cursor'], binding=previous.get('binding'), **fields))

        with patch.dict(os.environ, SUTRA_WORKSPACE_ALLOCATION='{"seconds":10}'):
            waiting = handle(self.root, self.ws, dict(common, event='start', prompt=ask, armed=False))
            paused = request('step', waiting)
            self.assertEqual(paused['state'], 'PAUSED')
            self.assertEqual(paused['stop']['resume_phase'], 'BINDING')
            self.assertEqual(paused['stop']['automatic_allocation']['reason'], 'workspace_limit')
            self.assertIsNone(paused['execution_id'])
            sid = paused['binding']['session']
            snapshot = self.s.pinned(sid)
            budget = review_budget.usage(self.s, sid)
            self.assertEqual(budget['used']['operations'], 0)
            self.assertEqual(budget['used']['calls'], 0)
            proposal = request('allocation_preview', paused)['allocation_proposal']
            self.assertEqual(proposal['additions'], {'seconds': 11})

        self.s.close()
        self.s = InvocationLedger(self.root / 'state.sqlite')
        with patch.dict(os.environ, SUTRA_WORKSPACE_ALLOCATION='{"seconds":120}'):
            resumed = request('resume', paused)
            self.assertEqual(resumed['state'], 'BINDING')
            amended = request('step', resumed)
            self.assertEqual(amended['state'], 'BINDING')
            self.assertEqual(amended['allocation_amendment']['used'], budget['used'])
            bound = request('step', amended)
        self.assertEqual(bound['state'], 'WORKSPACE')
        self.assertEqual(bound['flow_id'], waiting['flow_id'])
        self.assertEqual(bound['binding'], paused['binding'])
        self.assertEqual(self.s.pinned(sid), snapshot)
        self.assertEqual(review_budget.usage(self.s, sid)['used']['operations'], 1)
        self.assertEqual(review_budget.usage(self.s, sid)['used']['calls'], 0)
        self.assertTrue(self.s.audit_valid())

    def test_prebind_arming_interruption_resumes_without_assuming_task_authority(self):
        from lifecycle import Interrupted

        self.ws = self.ws.resolve()
        chat = {'chat_key': 'a' * 64}
        waiting = self.step(prompt='can you review the codebase please', armed=False, **chat)
        with patch('orchestration_flow.review_provenance', side_effect=Interrupted('Pause before initial task identity')):
            paused = self.step(waiting, **chat)
        self.assertEqual(paused['stop']['resume_phase'], 'NEED_ARM')
        self.assertIsNone(paused['binding'])
        with self.assertRaisesRegex(InvalidUpdate, 'user-armed'):
            self.step(paused, event='resume', armed=False, **chat)
        with self.assertRaisesRegex(InvalidUpdate, 'permissions changed'):
            self.step(paused, event='resume', binding={'session': 'foreign'}, **chat)
        resumed = self.step(paused, event='resume', **chat)
        self.assertEqual(resumed['state'], 'NEED_ARM')
        self.assertEqual(resumed['flow_id'], waiting['flow_id'])
        self.assertEqual(self.step(resumed, **chat)['state'], 'WORKSPACE')
        self.assertEqual(self.models, [])

    def test_interrupted_catalog_charges_stay_on_the_bound_task(self):
        from lifecycle import Interrupted
        import repository_tools as repo

        self.ws = self.ws.resolve()
        chat = {'chat_key': 'a' * 64}
        waiting = self.step(prompt='can you review the codebase please', armed=False, **chat)
        with patch('repository_tools.inventory', side_effect=Interrupted('Stop during catalog scan')):
            paused = self.step(waiting, **chat)
        self.assertEqual(paused['stop']['resume_phase'], 'BINDING')
        sid = paused['binding']['session']
        records = repo.records(self.s, sid)
        self.assertEqual([row['result']['status'] for row in records], ['interrupted'])
        charged = review_budget.usage(self.s, sid)['used']
        self.assertEqual(charged['operations'], 1)
        resumed = self.step(paused, event='resume', **chat)
        bound = self.step(resumed, **chat)
        self.assertEqual(bound['state'], 'WORKSPACE')
        self.assertEqual(bound['binding'], paused['binding'])
        rows = repo.records(self.s, sid)
        self.assertEqual(rows[0], records[0])
        self.assertEqual([row['result']['status'] for row in rows], ['interrupted', 'ok'])
        self.assertEqual(review_budget.usage(self.s, sid)['used']['operations'], charged['operations'] + 1)
        self.assertTrue(self.s.audit_valid())

    def test_legacy_prebind_pause_cannot_recreate_an_unidentified_task(self):
        self.ws = self.ws.resolve()
        chat = {'chat_key': 'a' * 64}
        waiting = self.step(prompt='can you review the codebase please', armed=False, **chat)
        fid = waiting['flow_id']
        flow.move(self.s, fid, 'BINDING', 'ARM_CONFIRMED')
        flow.move(self.s, fid, 'PAUSED', 'BRIDGE_INTERRUPTED',
                  workflow_stop={'status': 'interrupted', 'resume_phase': 'BINDING'})
        paused = flow.response(self.s, fid)
        with self.assertRaisesRegex(InvalidUpdate, 'no verified task identity'):
            self.step(paused, event='resume', **chat)
        self.assertEqual(flow.response(self.s, fid)['cursor'], paused['cursor'])

    def test_interrupted_permission_decision_waits_for_confirmation_or_recovers_exact_commit(self):
        from lifecycle import Interrupted
        from vscode_bridge import save_contract

        self.ws = self.ws.resolve()
        self.operation = 'edit'
        grant = {'operation': 'edit', 'paths': ['part0.py']}

        def saved_pause(key):
            chat = {'chat_key': key * 64}
            waiting = self.step(armed=False, **chat)
            need = self.step(waiting, **chat)
            self.assertEqual(need['state'], 'NEED_PERMISSION')
            return chat, need

        chat, need = saved_pause('1')
        sid = need['bound_task']['session']
        old_contract = self.s.pinned(sid)
        old_usage = review_budget.usage(self.s, sid)['used']
        with patch('native_access.grant', side_effect=Interrupted('Stop before permission grant')):
            paused = self.step(need, event='grant', grant=grant, **chat)
        self.assertEqual(paused['stop']['resume_phase'], 'NEED_PERMISSION')
        waiting = self.step(paused, event='resume', **chat)
        self.assertEqual(waiting['state'], 'NEED_PERMISSION')
        self.assertEqual(waiting['bound_task'], need['bound_task'])
        self.assertEqual(waiting['permission'], need['permission'])
        self.assertEqual(self.s.pinned(sid), old_contract)
        self.assertEqual(review_budget.usage(self.s, sid)['used'], old_usage)
        with self.assertRaisesRegex(InvalidUpdate, 'Explicit permission grant'):
            self.step(waiting, **chat)

        chat, need = saved_pause('2')
        sid = need['bound_task']['session']
        old_usage = review_budget.usage(self.s, sid)['used']

        def commit_then_interrupt(*args, **kwargs):
            save_contract(*args, **kwargs)
            raise Interrupted('Stop after scope contract commit')

        with patch('vscode_bridge.save_contract', side_effect=commit_then_interrupt):
            paused = self.step(need, event='grant', grant=grant, **chat)
        authorized = self.s.pinned(sid)
        self.assertTrue(authorized['constraints']['edits_allowed'])
        self.assertFalse(authorized['constraints']['tests_allowed'])
        self.assertEqual(review_budget.usage(self.s, sid)['used'], old_usage)
        with self.assertRaisesRegex(InvalidUpdate, 'permissions changed'):
            self.step(paused, event='resume', binding={'session': 'foreign'}, **chat)
        resumed = self.step(paused, event='resume', **chat)
        self.assertEqual(resumed['state'], 'BINDING')
        bound = self.step(resumed, **chat)
        self.assertEqual(bound['state'], 'WORKSPACE')
        self.assertEqual(bound['binding']['session'], sid)
        self.assertEqual(self.s.pinned(sid), authorized)
        self.assertEqual(review_budget.usage(self.s, sid)['used'], old_usage)
        self.assertTrue(self.s.audit_valid())

    def test_interrupted_legacy_permission_recovers_matching_completion_receipt(self):
        from lifecycle import Interrupted

        self.ws = self.ws.resolve()
        self.operation = 'edit'
        chat = {'chat_key': 'a' * 64}
        need = self.step(self.step(armed=False, **chat), **chat)
        sid = need['bound_task']['session']
        old_usage = review_budget.usage(self.s, sid)['used']
        real_audit = self.s._audit

        def legacy_audit(event, payload):
            if event == 'NATIVE_PERMISSION_INTENT':
                payload = {key: value for key, value in payload.items() if key not in ('operation', 'flow_id')}
            real_audit(event, payload)

        with patch.object(self.s, '_audit', side_effect=legacy_audit), \
                patch('native_access.bind', side_effect=Interrupted('Stop after grant receipt')):
            paused = self.step(need, event='grant', grant={'operation': 'edit', 'paths': ['part0.py']}, **chat)
        self.assertEqual(paused['stop']['resume_phase'], 'NEED_PERMISSION')
        committed = self.s.pinned(sid)
        resumed = self.step(paused, event='resume', **chat)
        self.assertEqual(resumed['state'], 'BINDING')
        self.assertEqual(self.step(resumed, **chat)['state'], 'WORKSPACE')
        self.assertEqual(review_budget.usage(self.s, sid)['used'], old_usage)
        self.assertEqual(self.s.pinned(sid), committed)
        self.assertTrue(self.s.audit_valid())

    def test_successive_test_grants_keep_revalidated_exact_paths_and_spending(self):
        from native_access import grant

        self.ws = self.ws.resolve()
        old_test, new_test = 'test_zeta.py', 'test_alpha.py'
        for name in (old_test, new_test):
            (self.ws / name).write_text('import unittest\n')
        chat = {'chat_key': 'a' * 64}
        waiting = self.step(prompt='Review the project. Remember project Cedar, ticket CED724. No network or new dependencies.',
                            armed=False, **chat)
        self.assertEqual(waiting['state'], 'NEED_ARM')
        bound = self.step(waiting, **chat)
        self.assertEqual(bound['state'], 'WORKSPACE')
        sid = bound['binding']['session']
        meta = json.loads(flow.metadata_path(self.root, bound['flow_id']).read_text())
        first = dict(bound['binding'], conversation_id=bound['conversation_id'], operation='test',
                     paths=[old_test], test_paths=[old_test])
        grant(self.s, meta, self.ws, first)
        previous = flow.identity(self.s, sid)
        before = review_budget.usage(self.s, sid)
        self.assertGreater(before['used']['operations'], 0)

        def receipts(kind):
            return [json.loads(raw) for raw, in self.s.db.execute(
                'SELECT payload_json FROM page_audit WHERE event=? ORDER BY seq', (kind,))]

        request = dict(previous, conversation_id=bound['conversation_id'], operation='test',
                       paths=[new_test, 'part0.py'], test_paths=[new_test])
        initial_intents = receipts('NATIVE_PERMISSION_INTENT')
        edit = dict(request, operation='edit', paths=['part0.py'], test_paths=[])
        with self.assertRaisesRegex(InvalidUpdate, 'confirmed edit scope'):
            grant(self.s, meta, self.ws, edit)
        overflow = dict(request, paths=[new_test] + [f'extra{i}.py' for i in range(7)])
        with self.assertRaisesRegex(InvalidUpdate, 'at most eight exact Python paths'):
            grant(self.s, meta, self.ws, overflow)
        old_file = self.ws / old_test
        parked = old_file.rename(self.ws / 'temporarily_missing.py')
        with self.assertRaisesRegex(InvalidUpdate, f'Source is missing: {old_test}'):
            grant(self.s, meta, self.ws, request)
        parked.rename(old_file)
        self.assertEqual(receipts('NATIVE_PERMISSION_INTENT'), initial_intents)
        self.assertEqual(flow.identity(self.s, sid), previous)
        self.assertEqual(review_budget.usage(self.s, sid), before)

        grant(self.s, meta, self.ws, request)
        contract = self.s.snapshot(sid)['constraints']
        expected_paths = ['part0.py', new_test, old_test]
        self.assertEqual(contract['allowed_paths'], expected_paths)
        self.assertEqual(contract['test_paths'], [new_test, old_test])
        self.assertTrue(contract['tests_allowed'])
        for name in ('edits_allowed', 'network_allowed', 'dependencies_allowed'):
            self.assertFalse(contract[name])
        self.assertEqual(review_budget.usage(self.s, sid), before)
        self.assertEqual(receipts('NATIVE_PERMISSION_INTENT')[-1]['constraints'], contract)
        completed = receipts('NATIVE_PERMISSION_GRANT')[-1]
        self.assertEqual(completed['paths'], expected_paths)
        self.assertEqual(completed['tests'], [new_test, old_test])
        self.assertEqual(completed['carry_usage'], before['used'])
        self.assertTrue(self.s.audit_valid())

    def test_parent_output_recall_retires_the_paid_retry_checkpoint(self):
        out = self.until('SYNTHESIS')
        fid = out['flow_id']
        checkpoint = {'failed_invocation': 'a' * 32}
        recall = {'invocation_id': 'b' * 32, 'refs': ['c' * 64]}
        flow.move(self.s, fid, 'SYNTHESIS', 'FIXTURE_RETRY', parent_output_retry=checkpoint)
        out = flow.response(self.s, fid)
        with patch('chat_runtime.resume_parent_output', return_value={'retained_context_pending': recall}):
            next_step = self.step(out)
        self.assertEqual(next_step['state'], 'SYNTHESIS')
        _, data = flow.info(self.s, fid)
        self.assertIsNone(data['parent_output_retry'])
        self.assertEqual(data['context_recall'], recall)

        def assert_recalled(_s, _meta, _workspace, _root, request, **_options):
            self.assertEqual(request['context_recall'], recall)
            return {'result': {'invocation_id': 'd' * 32, 'accepted_output': {'answer': 'Review completed.'},
                               'admission': {'admitted': True}}}

        # The old coexistence of recall and retry fields can also be encountered
        # when resuming a previously saved workflow.
        flow.move(self.s, fid, 'SYNTHESIS', 'FIXTURE_OLDER_RECALL', parent_output_retry=checkpoint)
        saved = flow.response(self.s, fid)
        with patch('chat_runtime.resume_parent_output', side_effect=AssertionError('Paid retry was already accepted')), \
                patch('chat_runtime.answer', side_effect=assert_recalled):
            self.assertEqual(self.step(saved)['state'], 'DONE')

    def test_lost_parent_output_recall_acknowledgement_recovers_before_retry(self):
        out = self.until('SYNTHESIS')
        fid = out['flow_id']
        failed = 'a' * 32
        recalled = {'invocation_id': 'b' * 32, 'refs': ['c' * 64]}
        flow.move(self.s, fid, 'SYNTHESIS', 'FIXTURE_RETRY', parent_output_retry={'failed_invocation': failed})
        with patch('retained_context.recover_requests', return_value=recalled) as recovered, \
                patch('chat_runtime.resume_parent_output', side_effect=AssertionError('Paid retry was already accepted')):
            next_step = self.step(flow.response(self.s, fid))
        self.assertEqual(next_step['state'], 'SYNTHESIS')
        self.assertEqual(recovered.call_args.kwargs, {'parent_output_failure': failed})
        _, data = flow.info(self.s, fid)
        self.assertIsNone(data['parent_output_retry'])
        self.assertEqual(data['context_recall'], recalled)

    def sources_for_slices(self):
        # Continuation tests need more source than one complete delivered page.
        for index in range(4):
            (self.ws / f'part{index}.py').write_text(
                f'def part{index}():\n    return {index}\n' +
                ''.join(f'    # source segment {line:03}: unique continuation evidence for part {index}.\n'
                        for line in range(100)))

    def test_named_test_stays_a_test_when_classifier_reads_support_files(self):
        from test_native_runtime import NativeModel
        from host_actions import preview

        self.ws = self.ws.resolve()
        target = 'test_ledger_append.py'
        paths = [target, 'stp/ledger.py', 'stp/kernel.py']
        (self.ws / 'stp').mkdir()
        (self.ws / paths[1]).write_text('def safe():\n    return True\n')
        (self.ws / paths[2]).write_text('# large snapshot support\n' * 380)
        (self.ws / target).write_text('from stp.ledger import safe\n')
        classifier = Conversation(MODELS['low'], lambda result, _: result.update(
            intent='workspace', workspace_request={'operation': 'read', 'paths': paths[1:]}))
        coder = NativeModel(MODELS['low'], 'run_tests', lambda result: result['action'].update(target=target))

        def models(role):
            self.assertIn(role, ('conversation', 'coder'))
            return classifier if role == 'conversation' else coder

        self.provider = models
        chat = {'chat_key': 'a' * 64}
        prompt = 'Run test_ledger_append.py with stp/ledger.py and stp/kernel.py in its read-only snapshot. Do not edit.'
        with patch('repository_tools.execute', side_effect=AssertionError('No workspace access')):
            pending = self.step(prompt=prompt, armed=False, **chat)
            self.assertEqual(pending['state'], 'NEED_ARM')
            self.assertEqual(m.events(self.s, pending['flow_id'])[-1]['kind'], 'EXPLICIT_TEST_ROUTED')
            self.assertFalse(coder.calls)
            with self.assertRaisesRegex(InvalidUpdate, 'user-armed'):
                self.step(pending, armed=False, **chat)
        need = self.step(pending, **chat)
        self.assertEqual(need['state'], 'NEED_PERMISSION')
        self.assertEqual(need['permission']['operation'], 'test')
        self.assertEqual(need['permission']['paths'], paths)
        self.assertEqual(need['permission']['test_paths'], [target])
        with self.assertRaisesRegex(InvalidUpdate, 'must match the named test'):
            self.step(need, event='grant', grant={'operation': 'test', 'paths': paths,
                                                 'test_paths': paths}, **chat)
        self.assertFalse(coder.calls)
        scoped = self.step(need, event='grant', grant={'operation': 'test', 'paths': paths,
                                                       'test_paths': [target]}, **chat)
        self.assertEqual(scoped['state'], 'WORKSPACE')
        coding = self.step(scoped, **chat)
        self.assertEqual(coding['state'], 'CODING')
        proposed = self.step(coding, **chat)
        self.assertEqual(proposed['state'], 'AWAITING_ACTION')
        [payload] = coder.calls
        self.assertEqual(payload['mode'], 'test')
        self.assertEqual(payload['test_target'], target)
        self.assertEqual([item['path'] for item in payload['pages']], [target])
        c = payload['contract']['constraints']
        self.assertEqual(c['allowed_paths'], sorted(paths))
        self.assertEqual(c['test_paths'], [target])
        self.assertFalse(c['edits_allowed'])
        self.assertFalse(c['network_allowed'])
        result = proposed['data']['result']
        checked = preview(self.s, proposed['binding']['session'], result['invocation_id'], self.ws)
        self.assertEqual(checked['snapshot_files'], sorted(paths))
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM host_actions').fetchone()[0], 0)
        self.assertTrue(self.s.audit_valid())

    def test_conversation_parent_stays_in_its_chat_across_reload_and_model_switch(self):
        chat_a, chat_b = 'a' * 64, 'b' * 64
        fact = 'Remember that Silvercliff uses ticket SC-4581.'

        def provider(stage):
            self.assertEqual(stage, 'conversation')

            def reply(result, packet):
                if packet['request'] == fact:
                    result['facts'] = [fact]

            model = Conversation(MODELS[self.tier], reply)
            self.models.append((stage, model))
            return model

        self.provider = provider
        first = self.step(prompt=fact, chat_key=chat_a, armed=False)
        self.assertEqual(first['state'], 'DONE')
        sid = first['conversation']['session']
        parent = first['conversation_id']
        original = review_budget.usage(self.s, sid)
        contract = self.models[-1][1].payloads[0]['contract']
        self.assertEqual(original['used']['calls'], 1)

        separate = self.step(prompt='What do we have open?', chat_key=chat_b, armed=False)
        self.assertNotEqual(separate['conversation']['session'], sid)
        self.assertNotIn('SC-4581', canonical(self.models[-1][1].payloads[0]))

        before_attempt = len(self.models)
        with self.assertRaisesRegex(InvalidUpdate, 'another Sutra chat'):
            self.step(prompt='What is my ticket?', chat_key=chat_b, parent=parent, armed=False)
        self.assertEqual(len(self.models), before_attempt)
        self.assertEqual(review_budget.usage(self.s, sid), original)

        self.s.close()
        self.s = InvocationLedger(self.root / 'state.sqlite')
        self.tier = 'high'
        followed = self.step(prompt='What is my ticket?', chat_key=chat_a, parent=parent, armed=False)
        self.assertEqual(followed['state'], 'DONE')
        self.assertEqual(followed['conversation']['session'], sid)
        model = self.models[-1][1]
        self.assertEqual(model.model, MODELS['high'])
        packet = model.payloads[0]
        self.assertEqual(packet['memory']['invocation_id'], parent)
        self.assertIn(fact, [pin['quote'] for pin in packet['memory']['pinned_user_statements']])
        self.assertEqual(packet['contract'], contract)
        charged = review_budget.usage(self.s, sid)
        self.assertEqual(charged['limits'], original['limits'])
        self.assertEqual(charged['used']['calls'], original['used']['calls'] + 1)
        self.assertGreater(charged['used']['tokens'], original['used']['tokens'])
        self.assertEqual(charged['used']['source_bytes'], original['used']['source_bytes'])
        self.assertTrue(self.s.verify_invocations(sid))

    def test_legacy_receipt_with_foreign_ancestor_cannot_restore_memory(self):
        chat_a, chat_b = 'a' * 64, 'b' * 64

        def provider(stage):
            self.assertEqual(stage, 'conversation')
            model = Conversation(MODELS[self.tier])
            self.models.append((stage, model))
            return model

        self.provider = provider
        # Simulate receipts written before conversation axes were attached.
        with patch('conversation.chat_axis', return_value=None):
            first = self.step(prompt='Remember this chat concerns Project Ironlark.', chat_key=chat_a)
            mixed = self.step(prompt='Continue that discussion.', chat_key=chat_b,
                              parent=first['conversation_id'])
        self.assertEqual(first['state'], 'DONE')
        self.assertEqual(mixed['state'], 'DONE')
        sid = first['conversation']['session']
        self.assertEqual(mixed['conversation']['session'], sid)
        self.assertIn('Ironlark', canonical(self.models[-1][1].payloads[0]['memory']))
        before = review_budget.usage(self.s, sid)
        count = len(self.models)

        for chat in (chat_a, chat_b):
            with self.subTest(chat=chat), self.assertRaisesRegex(InvalidUpdate, 'another Sutra chat'):
                self.step(prompt='What was the earlier topic?', chat_key=chat, parent=mixed['conversation_id'])
        self.assertEqual(len(self.models), count)
        self.assertEqual(review_budget.usage(self.s, sid), before)

        valid = self.step(prompt='What was the earlier topic?', chat_key=chat_a, parent=first['conversation_id'])
        self.assertEqual(valid['state'], 'DONE')
        self.assertEqual(valid['conversation']['session'], sid)
        self.assertIn('Ironlark', canonical(self.models[-1][1].payloads[0]['memory']))

    def test_repaired_handoff_still_requires_host_arming_and_retains_limits(self):
        original=self.provider
        def provider(stage):
            if stage!='conversation':return original(stage)
            def reply(r,p):
                if 'repair' not in p:r['answer']='This conversation has no file-access tools.'
                else:r.update(intent='workspace',answer='Handing off in this chat.',workspace_request={'operation':'read','scope':'workspace','paths':[]})
            model=Conversation(MODELS[self.tier],reply);self.models.append((stage,model));return model
        self.provider=provider
        out=self.step(armed=False)
        self.assertEqual(out['state'],'NEED_ARM')
        self.assertNotIn('active',self.meta)
        self.assertEqual(len(self.models[0][1].payloads),2)
        with self.assertRaisesRegex(InvalidUpdate,'user-armed'):
            self.step(out,armed=False)
        out=self.until('CHILDREN',out)
        sid=self.meta['active'];contract=self.s.pinned(sid)
        self.assertFalse(contract['constraints']['edits_allowed'])
        self.assertFalse(contract['constraints']['network_allowed'])
        self.s.close();self.s=InvocationLedger(self.root/'state.sqlite');self.tier='high'
        out=self.until('DONE',out)
        self.assertTrue(out['data']['result']['admission']['admitted'])
        self.assertEqual(contract,self.s.pinned(sid))

    def test_auto_small_first_then_fresh_source_worker_switch_keeps_contract(self):
        self.tier='auto';self.prompt='Review the architecture. Remember Cedar, CED724. Keep this read only; no network or new dependencies.'
        def provider(stage):
            if stage=='conversation':
                p=Conversation('small',lambda r,p:r.update(intent='workspace',escalation_reason='reasoning_capacity',
                    workspace_request={'operation':'read','scope':'workspace','paths':[]}))
            else:
                p=Child('large') if stage=='subagent' else Review('large')
                p.budget=__import__('paging').Budget(32768,4096,256)
            self.models.append((stage,p));return p
        self.provider=provider
        first=self.step(armed=False)
        self.assertEqual(first['conversation']['route']['tier'],'low')
        self.assertEqual(len(self.models),1);self.assertNotIn('active',self.meta)
        with self.assertRaisesRegex(InvalidUpdate,'user-armed'):self.step(first,armed=False)
        children=self.until('CHILDREN',first);sid=self.meta['active'];contract=self.s.pinned(sid)
        used=review_budget.usage(self.s,sid)['used']
        done=self.until('DONE',children)
        self.assertTrue(done['data']['result']['admission']['admitted'])
        self.assertEqual(done['data']['route']['tier'],'high')
        self.assertEqual(self.s.pinned(sid),contract)
        self.assertGreater(review_budget.usage(self.s,sid)['used']['calls'],used['calls'])
        for stage,p in self.models:
            if stage not in ('subagent','synthesis'):continue
            packet=p.payloads[0]
            if packet.get('review_protocol') == 2:
                self.assertTrue(packet['source_evidence'])
                self.assertEqual(packet['contract'], contract)
                continue
            self.assertTrue(packet['excerpts']);self.assertTrue(packet.get('snapshot_receipts'))
            self.assertEqual(packet['contract'],contract)
            self.assertFalse(packet['contract']['constraints']['network_allowed'])
            self.assertFalse(packet['contract']['constraints']['edits_allowed'])
            self.assertIn('CED724',canonical(packet['memory']))

    def test_fresh_models_keep_context_permissions_budget_and_join(self):
        out=self.children();self.assertGreaterEqual(len(out['children']),2)
        contract=self.s.pinned(self.meta['active']);sid=self.meta['active']
        parent=m.node(self.s,out['children'][0]['id'])['parent_id'];before=m.spent(self.s,parent)
        out=self.step(out);after=m.spent(self.s,parent);self.assertGreater(after['calls'],before['calls'])
        self.s.close();self.s=InvocationLedger(self.root/'state.sqlite');self.tier='high'
        out=self.until('DONE',out);self.assertTrue(out['data']['result']['admission']['admitted'],out['data']['result']['admission'])
        self.assertEqual(contract,self.s.pinned(sid));self.assertGreater(m.spent(self.s,parent)['calls'],after['calls'])
        children=[p for stage,p in self.models if stage=='subagent'];self.assertGreaterEqual(len({p.model for p in children}),2)
        for p in children:
            packet=p.payloads[0];self.assertEqual(packet['contract'],contract)
            self.assertIn('CED724',canonical(packet['memory']));self.assertEqual(packet['memory']['recent_turns'][-1]['user'],self.prompt)
            self.assertFalse(packet['branch']['capabilities']['edit']);self.assertFalse(packet['branch']['capabilities']['network'])
        main=[p for stage,p in self.models if stage=='synthesis'][0];self.assertEqual(len(main.payloads[0]['child_results']),len(children))
        for result in main.payloads[0]['child_results']:
            self.assertIn('Static calls',result['answer_unverified'])
            self.assertTrue(result['evidence_coverage']['excerpt_files'])
        self.assertEqual(main.payloads[0]['workspace_map']['source_files'],4)
        self.assertTrue(self.s.verify_invocations(sid));self.assertTrue(self.s.audit_valid())
    def test_replayed_ui_event_and_unarmed_step_are_rejected(self):
        out=self.step()
        with self.assertRaises(InvalidUpdate):self.step(out,armed=False)
        self.step(out)
        with self.assertRaisesRegex(InvalidUpdate,'stale'):self.step(out)
    def test_child_cannot_escape_permissions_or_role(self):
        out=self.children();n=m.node(self.s,out['children'][0]['id'])
        for kind,paths,role in [('edit',n['files'],None),('run_tests',n['files'],None),('model',n['files'],'coder'),('repository',['outside.py'],None)]:
            with self.subTest(kind=kind),self.assertRaises(InvalidUpdate):m.scope_check(n,kind,paths,role)
        wider=copy.deepcopy(n['permissions']);wider['network_allowed']=True
        with self.assertRaises(InvalidUpdate):m.subset(wider,n['permissions'])
    def test_budget_reservations_prevent_sibling_overspend_and_no_refund_of_calls(self):
        out=self.children();cid=out['children'][0]['id'];n=m.node(self.s,cid);parent=n['parent_id']
        frames=m.gates(self.s,parent,{'tokens':m.node(self.s,parent)['limits']['tokens']})
        self.assertNotEqual(frames[0]['decision'],'ALLOW')
        with self.assertRaises(m.OrchestrationStop):m.allowed(m.gates(self.s,cid,{'calls':n['limits']['calls']*3}))
        out=self.step(out);used=m.spent(self.s,parent)
        self.step(out,event='cancel');self.assertEqual(m.spent(self.s,parent),used)
        self.assertEqual(m.state(self.s,parent),'CANCELLED')
        task=m.node(self.s,parent)['parent_id']
        self.assertTrue(all(v==0 for v in m.gates(self.s,task,{})[0]['reserved_for_other_children'].values()))
    def test_source_change_after_child_completion_blocks_join(self):
        out=self.children();out=self.step(out);first=m.node(self.s,out['children'][0]['id'])
        (self.ws/first['files'][0]).write_text('def changed(): return 99\n')
        with self.assertRaises((InvalidUpdate,PageFault)):
            self.until('DONE',out)
    def test_contract_change_invalidates_children(self):
        out=self.children();cid=out['children'][0]['id'];sid=self.meta['active']
        from vscode_bridge import save_contract
        snap=self.s.snapshot(sid);save_contract(self.s,sid,{'goal':'Changed task','constraints':snap['constraints']},self.ws)
        with self.assertRaises(InvalidUpdate):m.checked(self.s,cid)
    def test_kernel_compound_fin_precedes_other_decisions(self):
        from karma_adapter import KarmaAdapter
        a=KarmaAdapter()
        self.assertEqual(a.allocations({'calls':0},{'calls':10},{'calls':1})['decision'],'ALLOW')
        self.assertEqual(a.allocations({'calls':0},{'calls':10},{'calls':15})['decision'],'BRIDGE')
        self.assertEqual(a.allocations({'calls':0},{'calls':10},{'calls':30})['decision'],'FIN')
        self.assertEqual(a.allocations({'calls':10},{'calls':10},{'calls':0})['decision'],'COMPOUND_FIN')

    def test_cancelled_call_recovery_keeps_allocation_and_context(self):
        out=self.children();cid=out['children'][0]['id'];parent=m.node(self.s,cid)['parent_id']
        from lifecycle import reconcile
        with patch('subagent_runtime.InvocationGateway.invoke',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):self.step(out)
        used=m.spent(self.s,parent)
        self.s.close();self.s=InvocationLedger(self.root/'state.sqlite');reconcile(self.s,self.ws)
        self.assertEqual(m.spent(self.s,parent),used)
        self.tier='high';res=self.until('DONE',out)
        self.assertTrue(res['data']['result']['admission']['admitted'])
        self.assertIn('CED724',canonical([p.payloads for stage,p in self.models if stage=='subagent']))

    def test_transport_interruption_reconciles_child_and_charges_failed_call(self):
        out=self.children();cid=out['children'][0]['id'];parent=m.node(self.s,cid)['parent_id']
        before=m.spent(self.s,parent)
        with patch.object(Child,'call',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):self.step(out)
        self.assertEqual(m.state(self.s,cid),'RUNNING')
        from lifecycle import reconcile
        self.s.close();self.s=InvocationLedger(self.root/'state.sqlite');reconcile(self.s,self.ws)
        self.assertEqual(m.state(self.s,cid),'READY')
        charged=m.spent(self.s,parent);self.assertEqual(charged['calls'],before['calls']+1)
        self.assertGreaterEqual(charged['seconds'],before['seconds']+150)
        self.tier='high';res=self.until('DONE',out);self.assertTrue(res['data']['result']['admission']['admitted'])
        self.assertGreater(m.spent(self.s,parent)['calls'],charged['calls'])

    def test_child_timeout_resumes_same_slice_with_new_model_and_retained_spend(self):
        from invocation_gateway import consume_dispatch

        self.sources_for_slices()
        out = self.children()
        child = out['children'][0]['id']
        node = m.node(self.s, child)
        parent = node['parent_id']
        sid = node['session']
        contract = self.s.pinned(sid)
        original_call = Child.call

        def checkpoint(worker, *args):
            proposal, record = original_call(worker, *args)
            proposal['slice_status'] = 'continue'
            return proposal, record

        with patch.object(Child, 'call', checkpoint):
            sliced = self.step(out)
        self.assertEqual(sliced['state'], 'CHILDREN')
        self.assertEqual(sliced['child']['slice_status'], 'checkpoint')
        partial_id = sliced['child']['result']['invocation_id']
        first_packet = self.models[-1][1].payloads[0]
        self.assertEqual(m.state(self.s, child), 'READY')
        self.assertFalse(any(event['kind'] == 'RESULT' for event in m.events(self.s, child)))
        with self.assertRaisesRegex(InvalidUpdate, 'not ready'):
            m.join(self.s, parent)
        with self.assertRaisesRegex(InvalidUpdate, 'stale'):
            m.complete(self.s, child, partial_id)
        before_timeout = m.spent(self.s, child)
        task_before_timeout = review_budget.usage(self.s, sid)

        def timeout(worker, call_id, system, payload, schema):
            consume_dispatch(call_id, worker.model, system, payload, schema)
            worker.payloads.append(copy.deepcopy(payload))
            raise TimeoutError('Model transport deadline reached')

        with patch.object(Child, 'call', timeout):
            paused = self.step(sliced)
        timeout_packet = self.models[-1][1].payloads[0]
        self.assertEqual(paused['state'], 'PAUSED')
        self.assertEqual(paused['stop']['status'], 'child_slice_timeout')
        self.assertFalse(paused['stop']['automatic_retry'])
        self.assertEqual(paused['stop']['branch_id'], child)
        timed_out_id = paused['child']['result']['invocation_id']
        self.assertNotEqual(timed_out_id, partial_id)
        with self.assertRaises(PageFault):
            self.s.load_result(sid, timed_out_id)
        forged = dict(timeout_packet['child_slice'], predecessor=timed_out_id)
        with self.assertRaisesRegex(InvalidUpdate, 'predecessor'):
            m.check_slice(self.s, child, forged, current=True)
        self.assertEqual(m.state(self.s, child), 'READY')
        charged = m.spent(self.s, child)
        self.assertEqual(charged['calls'], before_timeout['calls'] + 1)
        self.assertGreater(charged['tokens'], before_timeout['tokens'])
        self.assertGreater(charged['seconds'], before_timeout['seconds'])
        self.assertEqual(charged['operations'], before_timeout['operations'])
        self.assertEqual(charged['source_bytes'], before_timeout['source_bytes'])
        task_spending = review_budget.usage(self.s, sid)
        self.assertEqual(task_spending['used']['calls'], task_before_timeout['used']['calls'] + 1)

        self.s.close()
        self.s = InvocationLedger(self.root / 'state.sqlite')
        self.tier = 'high'
        with self.assertRaisesRegex(InvalidUpdate, 'user-armed'):
            self.step(paused, event='resume', armed=False)
        resumed = self.step(paused, event='resume')
        self.assertEqual(resumed['state'], 'CHILDREN')
        self.assertEqual(m.spent(self.s, child), charged)
        self.assertEqual(review_budget.usage(self.s, sid), task_spending)
        finished = self.step(resumed)
        worker = self.models[-1][1]
        packet = worker.payloads[0]
        self.assertEqual(worker.model, MODELS['high'])
        self.assertEqual(packet['contract'], first_packet['contract'])
        self.assertEqual(packet['branch'], first_packet['branch'])
        self.assertEqual(packet['snapshot_receipts'], first_packet['snapshot_receipts'])
        self.assertEqual(packet['child_slice'], timeout_packet['child_slice'])
        self.assertEqual(packet['child_slice']['predecessor'], partial_id)
        self.assertEqual(packet['child_checkpoint'], timeout_packet['child_checkpoint'])
        self.assertIn('Inspected the entry and leaf source.', canonical(packet['child_checkpoint']))
        self.assertIn('Review runtime behavior.', canonical(packet['child_checkpoint']))
        self.assertIn('CED724', canonical(packet['memory']))
        self.assertEqual(m.node(self.s, child)['permissions'], node['permissions'])
        self.assertEqual(self.s.pinned(sid), contract)
        self.assertEqual(m.spent(self.s, child)['calls'], charged['calls'] + 1)
        self.assertEqual(m.spent(self.s, child)['source_bytes'], charged['source_bytes'])
        self.assertEqual(m.state(self.s, child), 'DONE')
        final_id = finished['child']['result']['invocation_id']
        self.assertNotIn(final_id, (partial_id, timed_out_id))
        result = self.until('DONE', finished)
        self.assertTrue(result['data']['result']['admission']['admitted'])
        join = next(event for event in m.events(self.s, parent) if event['kind'] == 'JOIN')
        self.assertIn(final_id, join['data']['invocations'])
        self.assertNotIn(partial_id, join['data']['invocations'])
        self.assertNotIn(timed_out_id, join['data']['invocations'])
        self.assertTrue(self.s.verify_invocations(sid))
        self.assertTrue(self.s.audit_valid())

    def test_child_resume_reuses_context_in_one_attempt_when_repair_share_cannot_fit(self):
        from budget_accounting import reserved
        from invocation_gateway import consume_dispatch

        self.sources_for_slices()
        fork = m.fork

        def tight_child_allocation(s, nid, proposals, signals, context):
            proposals[0]['limits']['tokens'] = 35000
            return fork(s, nid, proposals, signals, context)

        with patch.object(m, 'fork', tight_child_allocation):
            out = self.children()
        child = out['children'][0]['id']
        node = m.node(self.s, child)
        sid = node['session']
        call = Child.call

        def checkpoint(worker, *args):
            proposal, record = call(worker, *args)
            proposal['slice_status'] = 'continue'
            return proposal, record

        def timeout(worker, call_id, system, packet, schema):
            consume_dispatch(call_id, worker.model, system, packet, schema)
            worker.payloads.append(copy.deepcopy(packet))
            raise TimeoutError('Model transport deadline reached')

        with patch.object(Child, 'call', checkpoint):
            out = self.step(out)
        with patch.object(Child, 'call', timeout):
            paused = self.step(out)
        self.assertEqual(paused['stop']['status'], 'child_slice_timeout')
        stopped_packet = self.models[-1][1].payloads[0]
        charged = m.spent(self.s, child)
        task_budget = review_budget.usage(self.s, sid)
        remaining = node['limits']['tokens'] - charged['tokens']

        self.s.close()
        self.s = InvocationLedger(self.root / 'state.sqlite')
        self.tier = 'high'
        resumed = self.step(paused, event='resume')
        self.assertEqual(m.spent(self.s, child), charged)
        self.assertEqual(review_budget.usage(self.s, sid), task_budget)

        provider = self.provider
        worker = Child(MODELS['high'])
        worker.context_metadata = {'model_info': {'fixture.context_length': 32768}}
        self.provider = lambda stage: None if stage == 'subagent' else provider(stage)
        with patch('api_runtime.provider_for', return_value=worker), patch('monitoring.require_resources', return_value={}):
            finished = self.step(resumed)
        self.provider = provider
        self.assertEqual(m.state(self.s, child), 'DONE')
        self.assertEqual(len(worker.payloads), 1)
        packet = worker.payloads[0]
        self.assertEqual(packet['child_slice'], stopped_packet['child_slice'])
        self.assertEqual(packet['child_checkpoint'], stopped_packet['child_checkpoint'])
        self.assertEqual(packet['contract'], stopped_packet['contract'])
        self.assertEqual(packet['branch'], stopped_packet['branch'])
        self.assertEqual(packet['snapshot_receipts'], stopped_packet['snapshot_receipts'])
        self.assertEqual(packet['memory']['recent_turns'], stopped_packet['memory']['recent_turns'])
        self.assertEqual(packet['memory']['pinned_user_statements'], stopped_packet['memory']['pinned_user_statements'])
        self.assertEqual(packet['memory_budget']['context_limit'], remaining)

        invocation = finished['child']['result']['invocation_id']
        raw, run_id = self.s.db.execute('SELECT record_json,run_id FROM invocations WHERE id=?', (invocation,)).fetchone()
        envelope = json.loads(raw)
        self.assertEqual(envelope['model'], MODELS['high'])
        self.assertEqual(self.s.run(run_id)['config']['max_repairs'], 0)
        self.assertLess(remaining // 2, reserved(envelope))
        self.assertLessEqual(reserved(envelope), remaining)
        self.assertEqual(m.spent(self.s, child)['tokens'], charged['tokens'] + reserved(envelope))
        self.assertEqual(m.spent(self.s, child)['calls'], charged['calls'] + 1)
        self.assertEqual(m.spent(self.s, child)['source_bytes'], charged['source_bytes'])
        self.assertEqual(review_budget.usage(self.s, sid)['limits'], task_budget['limits'])
        completed = self.until('DONE', finished)
        self.assertTrue(completed['data']['result']['admission']['admitted'])
        self.assertTrue(self.s.verify_invocations(sid))
        self.assertTrue(self.s.audit_valid())

    def test_accepted_child_slice_recovers_before_checkpoint_commit_without_repeating_call(self):
        self.sources_for_slices()
        out = self.children()
        child = out['children'][0]['id']
        sid = m.node(self.s, child)['session']
        original_call = Child.call

        def checkpoint(worker, *args):
            proposal, record = original_call(worker, *args)
            proposal['slice_status'] = 'continue'
            return proposal, record

        with patch.object(Child, 'call', checkpoint), patch(
                'subagent_runtime.machine.complete', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.step(out)
        self.assertEqual(m.state(self.s, child), 'READY')
        self.assertFalse(any(event['kind'] == 'CHILD_SLICE_SAVED' for event in m.events(self.s, child)))
        used = m.spent(self.s, child)
        self.s.close()
        self.s = InvocationLedger(self.root / 'state.sqlite')

        with patch('subagent_runtime.InvocationGateway.invoke', side_effect=AssertionError('duplicate inference')):
            recovered = self.step(out)
        self.assertTrue(recovered['child']['recovered'])
        self.assertEqual(recovered['child']['slice_status'], 'checkpoint')
        self.assertEqual(m.state(self.s, child), 'READY')
        self.assertEqual(m.spent(self.s, child), used)
        saved = [event for event in m.events(self.s, child) if event['kind'] == 'CHILD_SLICE_SAVED']
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]['data']['invocation_id'], recovered['child']['result']['invocation_id'])

        self.tier = 'high'
        finished = self.step(recovered)
        self.assertTrue(finished['child']['result']['admission']['admitted'])
        worker = self.models[-1][1]
        self.assertEqual(worker.model, MODELS['high'])
        self.assertEqual(worker.payloads[0]['child_slice']['predecessor'], saved[0]['data']['invocation_id'])
        self.assertEqual(m.state(self.s, child), 'DONE')
        self.assertEqual(m.spent(self.s, child)['calls'], used['calls'] + 1)
        self.assertTrue(self.s.verify_invocations(sid))

    def test_socket_timeout_receipt_recovers_before_stop_commit_without_repeating_call(self):
        from invocation_gateway import consume_dispatch
        from socket import timeout

        out = self.children()
        child = out['children'][0]['id']
        cursor = m.slice_cursor(self.s, child)
        before = m.spent(self.s, child)

        def transport_timeout(worker, call_id, system, payload, schema):
            consume_dispatch(call_id, worker.model, system, payload, schema)
            raise timeout('timed out')

        with patch.object(Child, 'call', transport_timeout), patch(
                'subagent_runtime.machine.timeout_child', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.step(out)
        charged = m.spent(self.s, child)
        self.assertEqual(charged['calls'], before['calls'] + 1)
        self.assertEqual(m.slice_cursor(self.s, child), cursor)
        self.assertEqual(m.state(self.s, child), 'READY')
        self.assertFalse(any(event['kind'] == 'CHILD_SLICE_TIMED_OUT' for event in m.events(self.s, child)))
        self.s.close()
        self.s = InvocationLedger(self.root / 'state.sqlite')

        with patch('subagent_runtime.InvocationGateway.invoke', side_effect=AssertionError('duplicate timeout')):
            recovered = self.step(out)
        self.assertEqual(recovered['state'], 'PAUSED')
        self.assertEqual(recovered['stop']['slice_ordinal'], cursor['ordinal'])
        self.assertEqual(recovered['stop']['slice_reason'], 'timeout')
        self.assertTrue(recovered['child']['recovered'])
        self.assertEqual(m.spent(self.s, child), charged)
        self.assertEqual(m.slice_cursor(self.s, child), cursor)
        event, = [event for event in m.events(self.s, child) if event['kind'] == 'CHILD_SLICE_TIMED_OUT']
        self.assertEqual(event['data']['invocation_id'], recovered['child']['result']['invocation_id'])

    def test_successive_child_checkpoints_advance_source_and_reach_parent_synthesis(self):
        self.sources_for_slices()
        out = self.children()
        child = out['children'][0]['id']
        original_call = Child.call

        def checkpoint(worker, call_id, system, payload, schema):
            proposal, record = original_call(worker, call_id, system, payload, schema)
            proposal['summary'] = f"Inspected source segment {payload['child_slice']['ordinal']}."
            proposal['slice_status'] = 'continue'
            return proposal, record

        with patch.object(Child, 'call', checkpoint):
            first = self.step(out)
            second = self.step(first)
        self.assertEqual(first['child']['slice_status'], 'checkpoint')
        self.assertEqual(second['child']['slice_status'], 'checkpoint')
        first_result = first['child']['result']['accepted_output']
        second_result = second['child']['result']['accepted_output']
        self.assertNotEqual(first_result['citations'], second_result['citations'])
        first_packet, second_packet = [worker.payloads[0] for stage, worker in self.models
                                       if stage == 'subagent'][:2]
        path = second_result['citations'][0]['path']
        self.assertGreaterEqual(second_packet['excerpt_spans'][path]['start_line'],
                                first_packet['excerpt_spans'][path]['next_line'])
        self.assertEqual(m.slice_cursor(self.s, child)['ordinal'], 3)
        final = self.until('DONE', second)
        self.assertTrue(final['data']['result']['admission']['admitted'])

        synthesizer = next(worker for stage, worker in self.models if stage == 'synthesis')
        joined = next(result for result in synthesizer.payloads[0]['child_results'] if result['branch_id'] == child)
        for prior in (first_result, second_result):
            self.assertIn(prior['summary'], joined['summary_unverified'])
            for citation in prior['citations']:
                self.assertIn(citation, joined['citations'])

    def test_output_limit_repair_only_pauses_for_the_last_attempt(self):
        from invocation_gateway import consume_dispatch
        from local_model import OutputLimitExceeded

        out = self.children()
        child = out['children'][0]['id']
        before = m.spent(self.s, child)

        def exhausted(worker, call_id, system, payload, schema):
            consume_dispatch(call_id, worker.model, system, payload, schema)
            raise OutputLimitExceeded('The output allowance ended before the checkpoint')

        metadata = {'thinking': {'values': [True], 'default': True}}
        with patch.dict('os.environ', SUTRA_THINKING='adaptive'), patch.object(
                Child, 'context_metadata', metadata, create=True), patch.object(Child, 'call', exhausted):
            paused = self.step(out)
        self.assertEqual(paused['state'], 'PAUSED')
        self.assertEqual(paused['stop']['slice_reason'], 'output_limit')
        self.assertEqual(m.spent(self.s, child)['calls'], before['calls'] + 2)
        self.assertIsNone(m.recover_child(self.s, child))

        self.tier = 'high'
        resumed = self.step(paused, event='resume')
        finished = self.step(resumed)
        self.assertEqual(m.state(self.s, child), 'DONE')
        self.assertTrue(finished['child']['result']['admission']['admitted'])
        self.assertEqual(m.spent(self.s, child)['calls'], before['calls'] + 3)
        self.assertEqual(len([e for e in m.events(self.s, child) if e['kind'] == 'CHILD_SLICE_OUTPUT_LIMIT']), 1)

    def test_committed_fork_and_join_recover_without_duplicate_children(self):
        out=self.until('BRANCHING');n,d=flow.info(self.s,out['flow_id'])
        parent,children,_=flow.plan(self.s,self.meta,self.ws,n['prompt'],d['conversation_id'],d['root_node'])
        out=self.step(out);self.assertEqual([i['id'] for i in out['children']],[c['id'] for c in children])
        while any(i['state']!='DONE' for i in out['children']):out=self.step(out)
        ids=m.join(self.s,parent['id']);out=self.step(out);self.assertEqual(out['state'],'SYNTHESIS')
        res=self.until('DONE',out);self.assertTrue(res['data']['result']['admission']['admitted'])
        self.assertEqual(len([e for e in m.events(self.s,parent['id']) if e['kind']=='JOIN']),1)

    def test_forged_child_context_and_foreign_completion_are_rejected(self):
        out=self.children();cid=out['children'][0]['id'];other=out['children'][1]['id'];out=self.step(out)
        p=[p for stage,p in self.models if stage=='subagent'][0].payloads[0]
        import subagent_runtime
        for mutation in ('goal','memory','scope'):
            bad=copy.deepcopy(p)
            if mutation=='goal':bad['contract']['goal']='Different goal'
            elif mutation=='memory':bad['memory']['pinned_user_statements']=[]
            else:bad['source_paths']=['outside.py']
            with self.subTest(mutation=mutation),self.assertRaises(InvalidUpdate):subagent_runtime.request_check(self.s,self.meta['active'],bad)
        iid=m.events(self.s,cid)[-1]['data']['invocation_id']
        with self.assertRaises(InvalidUpdate):m.complete(self.s,other,iid)
        self.s.quarantine_invocation(self.meta['active'],iid,'test quarantine')
        with self.assertRaises((InvalidUpdate,PageFault)):self.until('DONE',out)

    def test_explicit_permission_grant_changes_scope_once_without_refunding_budget(self):
        self.operation='edit';out=self.until('NEED_PERMISSION');sid=self.meta['active'];old=review_budget.usage(self.s,sid)['used']
        self.assertFalse(self.s.snapshot(sid)['constraints']['edits_allowed'])
        with self.assertRaises(InvalidUpdate):self.step(out)
        out=self.step(out,event='grant',grant={'operation':'edit','paths':['part0.py'],'test_paths':[]})
        self.assertEqual(out['state'],'WORKSPACE');contract=self.s.pinned(sid)
        self.assertTrue(contract['constraints']['edits_allowed']);self.assertEqual(contract['constraints']['allowed_paths'],['part0.py'])
        now=review_budget.usage(self.s,sid)['used'];self.assertTrue(all(now[k]>=v for k,v in old.items()))
        self.tier='high';out=self.step(out);self.assertEqual(out['state'],'CODING');self.assertEqual(self.s.pinned(sid),contract)

    def test_unchanged_canonical_kernel(self):
        import hashlib
        self.assertEqual(hashlib.sha256((Path(__file__).parent/'stp_canonical/kernel.py').read_bytes()).hexdigest(),'9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac')

    def test_nonempty_calibration_binding_inherited_and_revocation_stops_children(self):
        import calibration
        from test_calibration import profile
        from vscode_bridge import handle
        handle(self.root,self.ws,{'op':'auto_new','goal':self.prompt,'roots':['.']})
        self.meta=json.loads((self.root/'workspace.json').read_text());sid=self.meta['active']
        ref=calibration.register(self.s,profile());bound=calibration.bind(self.s,sid,[ref],calibration.EMPTY_HASH)
        out=self.children();cid=out['children'][0]['id'];self.assertEqual(m.node(self.s,cid)['binding']['calibration_hash'],bound['hash'])
        out=self.step(out);worker=[p for stage,p in self.models if stage=='subagent'][0]
        self.assertEqual(worker.payloads[0]['contract']['calibration_binding'],bound)
        calibration.revoke(self.s,ref,'test revocation')
        before=len(self.models)
        blocked=self.step(out)
        self.assertFalse(blocked['recovery']['restart_allowed'])
        self.assertEqual(len(self.models),before)

    def test_oversized_fork_is_atomic_and_records_no_children(self):
        out=self.until('BRANCHING');_,data=flow.info(self.s,out['flow_id']);n=m.checked(self.s,data['root_node'])
        proposal={'objective':'Too large','files':['part0.py'],'roles':['subagent'],'permissions':n['permissions'],
                  'limits':{k:v*3 for k,v in n['limits'].items()}}
        before=self.s.db.execute('SELECT COUNT(*) FROM orchestration_nodes').fetchone()[0]
        with self.assertRaises(m.OrchestrationStop):m.fork(self.s,n['id'],[proposal],{}, {'request':self.prompt})
        self.assertEqual(m.state(self.s,n['id']),'READY');self.assertEqual(before,self.s.db.execute('SELECT COUNT(*) FROM orchestration_nodes').fetchone()[0])

    def test_explicit_read_only_review_uses_same_state_machine_without_classifier(self):
        from vscode_bridge import handle
        handle(self.root,self.ws,{'op':'auto_new','goal':self.prompt,'roots':['.']})
        self.meta=json.loads((self.root/'workspace.json').read_text());sid=self.meta['active']
        out=self.step(event='start_review',binding=flow.identity(self.s,sid))
        self.assertEqual(out['state'],'NEED_ARM');self.assertFalse(self.models)
        result=self.until('DONE',out);self.assertTrue(result['data']['result']['admission']['admitted'])
        self.assertFalse(any(stage=='conversation' for stage,p in self.models))

    def test_paused_mapping_explicit_resume_retains_binding_and_spent_budget(self):
        out=self.until('MAPPING');sid=self.meta['active'];contract=self.s.pinned(sid)
        with patch('auto_review.step',return_value={'auto':{'status':'resource_wait'}}):out=self.step(out)
        self.assertEqual(out['state'],'PAUSED');used=review_budget.usage(self.s,sid)['used']
        with self.assertRaisesRegex(InvalidUpdate,'Choose Resume'):self.step(out)
        with self.assertRaisesRegex(InvalidUpdate,'user-armed'):self.step(out,event='resume',armed=False)
        self.s.close();self.s=InvocationLedger(self.root/'state.sqlite');self.tier='high'
        out=self.step(out,event='resume');self.assertEqual(out['state'],'MAPPING')
        self.assertEqual(used,review_budget.usage(self.s,sid)['used']);self.assertEqual(contract,self.s.pinned(sid))
        self.assertTrue(self.until('DONE',out)['data']['result']['admission']['admitted'])

    def test_context_shortage_pauses_and_resumes_synthesis_without_reclassification(self):
        out=self.until('SYNTHESIS');sid=self.meta['active'];contract=self.s.pinned(sid)
        used=review_budget.usage(self.s,sid)['used'];conversation_id=out['conversation_id']
        with patch('chat_runtime.answer',side_effect=ContextBudgetExceeded('Review allocation is too small.')):
            out=self.step(out)
        self.assertEqual(out['state'],'PAUSED')
        self.assertEqual(out['stop']['status'],'context_budget')
        self.assertEqual(out['stop']['stage'],'SYNTHESIS')
        self.assertEqual(review_budget.usage(self.s,sid)['used'],used)
        with self.assertRaisesRegex(InvalidUpdate,'Choose Resume'):self.step(out)
        self.s.close();self.s=InvocationLedger(self.root/'state.sqlite');self.tier='high'
        pending=flow.handle(self.s,self.meta,self.ws,self.root,{'event':'pending'})
        self.assertEqual(pending['stop'],out['stop'])
        resumed=self.step(pending,event='resume')
        self.assertEqual(resumed['state'],'SYNTHESIS');self.assertIsNone(resumed['stop'])
        self.assertEqual(resumed['conversation_id'],conversation_id)
        self.assertEqual(review_budget.usage(self.s,sid)['used'],used)
        with patch('auto_review.step',side_effect=AssertionError('unnecessary remapping')):
            done=self.until('DONE',resumed)
        self.assertEqual(done['state'],'DONE');self.assertTrue(done['data']['result']['admission']['admitted'])
        self.assertEqual(self.s.pinned(sid),contract)

    def test_resource_wait_during_classification_needs_no_workspace_arm_and_rechecks_on_resume(self):
        from monitoring import ResourceWait
        blocked={'admitted':False,'available':True,'reason':'thermal pressure','thermal_label':'serious',
                 'decision':'BRIDGE','sample':{'memory_pressure_raw':1},'reclaimable_estimate_gib':20}
        with patch('conversation.answer',side_effect=ResourceWait(blocked)):
            out=self.step(armed=False)
        self.assertEqual(out['state'],'PAUSED');self.assertNotIn('active',self.meta)
        self.assertEqual(out['stop']['measurement'],blocked)
        self.assertEqual(out['stop']['resume_phase'],'CLASSIFYING')
        self.assertFalse(out['stop']['automatic_retry']);self.assertFalse(any(p.payloads for _,p in self.models))
        self.s.close();self.s=InvocationLedger(self.root/'state.sqlite')
        with patch('monitoring.resource_state',return_value=blocked):
            still=self.step(out,event='resume',armed=False)
        self.assertEqual(still['state'],'PAUSED');self.assertFalse(any(p.payloads for _,p in self.models))
        with patch('monitoring.resource_state',return_value={'admitted':True}):
            ready=self.step(still,event='resume',armed=False)
        self.assertEqual(ready['state'],'CLASSIFYING')
        classified=self.step(ready,armed=False)
        self.assertEqual(classified['state'],'NEED_ARM');self.assertNotIn('active',self.meta)

    def test_child_resource_pause_keeps_completed_sibling_and_model_switch_context(self):
        from monitoring import ResourceWait
        out=self.children();out=self.step(out);sid=self.meta['active']
        first=out['children'][0]['id'];parent=m.node(self.s,first)['parent_id']
        self.assertEqual(m.state(self.s,first),'DONE')
        used=m.spent(self.s,parent);contract=self.s.pinned(sid)
        blocked={'admitted':False,'available':True,'reason':'memory pressure','thermal_label':'nominal',
                 'decision':'FIN','sample':{'memory_pressure_raw':4},'reclaimable_estimate_gib':3}
        with patch('subagent_runtime.runtime.build_partition',side_effect=ResourceWait(blocked)):
            stopped=self.step(out)
        self.assertEqual(stopped['state'],'PAUSED');self.assertEqual(stopped['stop']['stage'],'CHILDREN')
        self.assertEqual(m.spent(self.s,parent),used)
        self.s.close();self.s=InvocationLedger(self.root/'state.sqlite');self.tier='high'
        with self.assertRaisesRegex(InvalidUpdate,'user-armed'):self.step(stopped,event='resume',armed=False)
        with patch('monitoring.resource_state',return_value=blocked):
            still=self.step(stopped,event='resume')
        self.assertEqual(still['state'],'PAUSED');self.assertEqual(m.spent(self.s,parent),used)
        with patch('monitoring.resource_state',return_value={'admitted':True}):ready=self.step(still,event='resume')
        self.assertEqual(ready['state'],'CHILDREN')
        done=self.until('DONE',ready)
        self.assertTrue(done['data']['result']['admission']['admitted'])
        self.assertEqual(contract,self.s.pinned(sid))
        self.assertEqual(len([e for e in m.events(self.s,first) if e['kind']=='RESULT']),1)

    def test_resource_wait_in_mapping_and_synthesis_keeps_correct_resume_stage(self):
        from monitoring import ResourceWait
        blocked={'admitted':False,'available':False,'reason':'Hardware sample is stale or future-dated','decision':None}
        out=self.until('MAPPING');sid=self.meta['active'];used=review_budget.usage(self.s,sid)['used']
        # Exercise the real auto-review handler's typed exception path.
        with patch('static_runtime.build',side_effect=ResourceWait(blocked)):
            out=self.step(out)
        self.assertEqual(out['state'],'PAUSED');self.assertEqual(out['stop']['stage'],'MAPPING')
        after=review_budget.usage(self.s,sid)['used']
        self.assertEqual(after['calls'],used['calls']);self.assertEqual(after['tokens'],used['tokens'])
        self.assertGreaterEqual(after['operations'],used['operations'])
        with patch('monitoring.resource_state',return_value={'admitted':True}):out=self.step(out,event='resume')
        out=self.until('SYNTHESIS',out);used=review_budget.usage(self.s,sid)['used']
        with patch('chat_runtime.answer',side_effect=ResourceWait(blocked)):out=self.step(out)
        self.assertEqual(out['state'],'PAUSED');self.assertEqual(out['stop']['stage'],'SYNTHESIS')
        self.assertEqual(review_budget.usage(self.s,sid)['used'],used)
        with patch('monitoring.resource_state',return_value={'admitted':True}):out=self.step(out,event='resume')
        self.assertEqual(out['state'],'SYNTHESIS')
        self.assertTrue(self.until('DONE',out)['data']['result']['admission']['admitted'])

    def test_memory_release_then_model_switch_preserves_child_context_grants_and_spend(self):
        import os
        import runtime_memory as rm
        from monitoring import ResourceWait
        out=self.children();out=self.step(out);sid=self.meta['active']
        first=out['children'][0]['id'];parent=m.node(self.s,first)['parent_id']
        contract=self.s.pinned(sid);used=m.spent(self.s,parent)
        row={'name':'large','digest':'identity','size':10*rm.GIB,'context_length':65536}
        blocked={'available':True,'admitted':False,'reason':'memory pressure','decision':'BRIDGE',
                 'blocker':'hardware_pressure','sample':{'wall_time':100}}
        healthy={'available':True,'admitted':True,'decision':'ALLOW','sample':{'wall_time':100}}
        with patch.dict(os.environ,{'SUTRA_RUNTIME_DIR':str(self.root/'runtime'),'SUTRA_MEMORY_OWNERSHIP':'exclusive'}),patch('monitoring.resource_state',return_value=blocked),patch.object(rm,'stable_resources',return_value=healthy),patch.object(rm,'running',side_effect=[[row],[],[]]),patch.object(rm,'api') as api:
            rm.write_state({'active':None,'owned':{'name':'large','digest':'identity','completed_at':1}})
            with patch('subagent_runtime.runtime.build_partition',side_effect=ResourceWait(blocked)):paused=self.step(out)
            self.assertEqual(paused['state'],'PAUSED');self.assertTrue(paused['stop']['recovery']['admitted'])
            self.assertEqual(paused['stop']['recovery']['action'],'released_owned_model')
            self.assertEqual(m.spent(self.s,parent),used)
            self.s.close();self.s=InvocationLedger(self.root/'state.sqlite');self.tier='high'
            with self.assertRaisesRegex(InvalidUpdate,'user-armed'):self.step(paused,event='resume',armed=False)
            with patch('monitoring.require_resources',return_value=healthy):resumed=self.step(paused,event='resume')
            self.assertEqual(m.spent(self.s,parent),used)
            api.assert_called_once()
        done=self.until('DONE',resumed)
        self.assertTrue(done['data']['result']['admission']['admitted'])
        self.assertEqual(contract,self.s.pinned(sid))
        self.assertEqual(len([e for e in m.events(self.s,first) if e['kind']=='RESULT']),1)
        self.assertGreater(m.spent(self.s,parent)['calls'],used['calls'])
        children=[p for stage,p in self.models if stage=='subagent']
        self.assertGreaterEqual(len({p.model for p in children}),2)
        self.assertIn('CED724',canonical(children[-1].payloads))
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM page_audit WHERE event='MEMORY_RELEASE_PLANNED'").fetchone()[0],1)

    def test_mapping_failure_details_survive_reload_and_do_not_dispatch_children(self):
        out=self.until('MAPPING');sid=self.meta['active'];used=review_budget.usage(self.s,sid)['used']
        failure={'auto':{'status':'needs_attention','mapped_files':2,'source_files':9,
                        'failures':{f'part{i}.py':'Static graph work-unit allocation exceeded' for i in range(7)}}}
        with patch('auto_review.step',return_value=failure):out=self.step(out)
        self.assertEqual(out['state'],'PAUSED');self.assertEqual(out['stop']['failure_count'],7)
        self.assertEqual(len(out['stop']['failures']),5);self.assertEqual(out['stop']['mapped_files'],2)
        self.s.close();self.s=InvocationLedger(self.root/'state.sqlite')
        saved=flow.handle(self.s,self.meta,self.ws,self.root,{'event':'pending'})
        self.assertEqual(saved['stop'],out['stop']);self.assertFalse(saved['children'])
        self.assertEqual(review_budget.usage(self.s,sid)['used'],used)
        self.tier='high';resumed=self.step(saved,event='resume');self.assertIsNone(resumed['stop'])

    def test_calibrated_allocation_pause_then_model_switch_preserves_parent(self):
        import runtime_memory as rm
        from test_memory_profiles import profile
        from monitoring import ResourceWait
        out=self.children();out=self.step(out);sid=self.meta['active']
        first=out['children'][0]['id'];parent=m.node(self.s,first)['parent_id']
        spent=m.spent(self.s,parent);contract=self.s.pinned(sid)
        hardware={'sample':{'free_bytes':20*rm.GIB,'inactive_bytes':0}}
        plan=rm.estimate('large',{},8*rm.GIB,[],32768,hardware,profile())
        self.assertEqual(plan['decision'],'BRIDGE')
        def stopped(*args,**kwargs):rm.denied('Calibrated memory allocation does not fit',plan=plan)
        with patch('subagent_runtime.runtime.build_partition',side_effect=stopped):paused=self.step(out)
        self.assertEqual(paused['state'],'PAUSED');self.assertEqual(m.spent(self.s,parent),spent)
        self.assertEqual(paused['stop']['measurement']['calibration']['profile_sha256'],plan['calibration']['profile_sha256'])
        self.s.close();self.s=InvocationLedger(self.root/'state.sqlite');self.tier='high'
        with self.assertRaisesRegex(InvalidUpdate,'user-armed'):self.step(paused,event='resume',armed=False)
        with patch('monitoring.resource_state',return_value={'admitted':True}):ready=self.step(paused,event='resume')
        self.assertEqual(m.spent(self.s,parent),spent)
        done=self.until('DONE',ready)
        self.assertTrue(done['data']['result']['admission']['admitted']);self.assertEqual(self.s.pinned(sid),contract)
        self.assertEqual(len([e for e in m.events(self.s,first) if e['kind']=='RESULT']),1)
        self.assertGreater(m.spent(self.s,parent)['calls'],spent['calls'])
        children=[p for stage,p in self.models if stage=='subagent']
        self.assertGreaterEqual(len({p.model for p in children}),2)
        self.assertIn('CED724',canonical(children[-1].payloads))

    def test_committed_child_receipt_recovers_without_duplicate_inference(self):
        out=self.children();cid=out['children'][0]['id']
        with patch('subagent_runtime.machine.complete',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):self.step(out)
        self.assertEqual(m.state(self.s,cid),'READY');used=m.spent(self.s,cid)
        self.s.close();self.s=InvocationLedger(self.root/'state.sqlite')
        with patch('subagent_runtime.InvocationGateway.invoke',side_effect=AssertionError('duplicate inference')):
            recovered=self.step(out)
        self.assertTrue(recovered['child']['recovered']);self.assertEqual(m.state(self.s,cid),'DONE');self.assertEqual(used,m.spent(self.s,cid))

    def test_pending_discovery_blocks_second_workflow_and_cancel_keeps_history(self):
        out=self.children();sid=self.meta['active'];used=review_budget.usage(self.s,sid)['used']
        found=flow.handle(self.s,self.meta,self.ws,self.root,{'event':'pending'})
        self.assertEqual(found['flow_id'],out['flow_id'])
        deferred=self.step()
        self.assertEqual(deferred['state'],'DONE')
        self.assertEqual(deferred['deferred_flow_id'],out['flow_id'])
        self.assertEqual(used,review_budget.usage(self.s,sid)['used'])
        with self.assertRaisesRegex(InvalidUpdate,'saved workflow'):
            self.step(event='start_existing',conversation_id=deferred['conversation_id'])
        inspected=self.step(out,event='inspect');self.assertEqual(inspected['prompt'],self.prompt)
        self.step(out,event='cancel');self.assertEqual(used,review_budget.usage(self.s,sid)['used'])
        self.assertIsNone(flow.handle(self.s,self.meta,self.ws,self.root,{'event':'pending'})['flow_id'])

    def test_greeting_beside_saved_review_preserves_context_budget_and_permissions(self):
        out=self.children();out=self.step(out);sid=self.meta['active']
        before=flow.response(self.s,out['flow_id']);contract=self.s.pinned(sid)
        used=review_budget.usage(self.s,sid)['used'];original=self.provider
        greeting=Conversation(MODELS['low'])
        self.provider=lambda stage:greeting if stage=='conversation' else original(stage)
        with patch('static_analysis.snapshot',side_effect=AssertionError('Greeting read source')):
            chat=self.step(prompt='hello there',armed=False)
        self.assertEqual(chat['state'],'DONE');self.assertNotIn('deferred_flow_id',chat)
        self.assertTrue(chat['conversation']['result']['admission']['admitted'])
        self.assertEqual(len(greeting.payloads),1)
        self.assertEqual(flow.response(self.s,out['flow_id']),before)
        self.assertEqual(self.s.pinned(sid),contract)
        self.assertEqual(review_budget.usage(self.s,sid)['used'],used)
        self.assertEqual(flow.handle(self.s,self.meta,self.ws,self.root,{'event':'pending'})['flow_id'],out['flow_id'])
        self.provider=original;self.s.close();self.s=InvocationLedger(self.root/'state.sqlite');self.tier='high'
        done=self.until('DONE',out)
        self.assertTrue(done['data']['result']['admission']['admitted'])
        self.assertEqual(self.s.pinned(sid),contract)
        self.assertGreater(review_budget.usage(self.s,sid)['used']['calls'],used['calls'])
        writer=next(p for stage,p in reversed(self.models) if stage=='synthesis' and 'claims' not in p.payloads[0])
        self.assertIn('CED724',canonical(writer.payloads[0]['memory']))

    def test_new_classification_resource_stop_does_not_pause_saved_same_prompt(self):
        from monitoring import ResourceWait
        out=self.children();before=flow.response(self.s,out['flow_id'])
        with patch('conversation.answer',side_effect=ResourceWait({'admitted':False,'reason':'memory pressure'})):
            paused=self.step(armed=False)
        self.assertNotEqual(paused['flow_id'],out['flow_id'])
        self.assertEqual(paused['state'],'PAUSED')
        self.assertEqual(paused['stop']['resume_phase'],'CLASSIFYING')
        self.assertEqual(flow.response(self.s,out['flow_id']),before)

    def test_document_review_does_not_reuse_saved_architecture_task(self):
        saved=self.children()
        sid=self.meta['active']
        before=flow.response(self.s,saved['flow_id'])
        contract=self.s.pinned(sid)
        permissions=m.permissions(self.s,sid)
        usage=review_budget.usage(self.s,sid)
        prompts=[
            ("Can you review this resume and comment? I am applying for post-silicon validation roles. The file is /Users/example/Downloads/Candidate Resume (1).docx",'DOCX'),
            ("i was requesting for the resume review but you gave me worksapce review. can you look at the resume now please?",'Document text:'),
        ]
        self.tier='high'
        with patch.object(self,'provider',side_effect=AssertionError('Document request dispatched a model')), \
                patch('repository_tools.execute',side_effect=AssertionError('Document request read the repository')):
            for prompt,expected in prompts:
                for event in ('start','start_existing','start_review'):
                    with self.subTest(event=event,prompt=prompt):
                        scope={'chat_key':'7'*64} if event=='start' else {}
                        result=self.step(event=event,prompt=prompt,armed=False,**scope)
                        self.assertIsNone(result['conversation_id'])
                        if event=='start':
                            self.assertEqual(result['state'],'PAUSED')
                            self.assertIn(expected,result['stop']['error'])
                            self.assertEqual(m.events(self.s,result['flow_id'])[-1]['kind'],'EVIDENCE_BRIDGE')
                        else:
                            self.assertEqual(result['state'],'DONE')
                            self.assertIn(expected,result['request_notice'])
        self.assertEqual(self.meta['active'],sid)
        self.assertEqual(flow.response(self.s,saved['flow_id']),before)
        self.assertEqual(self.s.pinned(sid),contract)
        self.assertEqual(m.permissions(self.s,sid),permissions)
        self.assertEqual(review_budget.usage(self.s,sid),usage)
        self.assertEqual(self.step(event='pending')['flow_id'],saved['flow_id'])

    def test_document_reference_from_memory_uses_non_workspace_route(self):
        first=Conversation()
        self.provider=lambda stage:first
        origin=self.step(prompt='The CV I mentioned is saved in /tmp/Example Applicant (1).docx.',armed=False)
        router=Conversation(fault=lambda out,_:out.update(intent='document'))
        self.provider=lambda stage:router
        with patch('repository_tools.execute',side_effect=AssertionError('Document request read the repository')):
            result=self.step(prompt='Can you look at it now?',parent=origin['conversation_id'],armed=False)
        self.assertEqual(result['state'],'PAUSED')
        self.assertIn('Document text:',result['stop']['error'])
        self.assertTrue(result['conversation']['memory_used'])
        self.assertIn('Example Applicant',canonical(router.payloads[0]['memory']))
        self.assertTrue(result['conversation']['result']['admission']['admitted'])
        self.assertNotIn('active',self.meta)
        self.assertEqual(m.events(self.s,result['flow_id'])[-1]['kind'],'EVIDENCE_BRIDGE')

    def test_pronoun_review_after_document_route_is_guidance_without_a_model(self):
        router=Conversation(fault=lambda out,_:out.update(intent='document'))
        self.provider=lambda stage:router
        first=self.step(prompt='For later discussion my résumé is in /tmp/Applicant.docx. Please acknowledge this.',armed=False)
        with patch.object(self,'provider',side_effect=AssertionError('Document follow-up dispatched an inference call')), \
                patch('repository_tools.execute',side_effect=AssertionError('Document follow-up read the repository')):
            ref=self.step(prompt='Can you look at it now?',parent=first['conversation_id'],armed=True)
        self.assertEqual(ref['state'],'PAUSED')
        self.assertIn('Document text:',ref['stop']['error'])
        self.assertIsNone(ref['conversation_id'])
        self.assertNotIn('active',self.meta)

    def test_latest_document_notice_guides_reference_without_receipt(self):
        chat_key='7'*64
        with patch.object(self,'provider',side_effect=AssertionError('Document follow-up dispatched an inference call')), \
                patch('repository_tools.execute',side_effect=AssertionError('Document follow-up read the repository')):
            first=self.step(prompt='Please review my CV.',chat_key=chat_key,armed=True)
            ref=self.step(prompt='Can you look at it now?',chat_key=chat_key,armed=True)
            without_chat_id=self.step(prompt='Can you look at it now?',chat_key='8'*64,armed=True,
                                      prior_prompt='Please review my CV.')
        self.assertIsNone(first['conversation_id'])
        self.assertEqual(ref['flow_id'],first['flow_id'])
        self.assertEqual(ref['stop']['error'],first['stop']['error'])
        self.assertEqual(without_chat_id['stop']['error'],first['stop']['error'])
        self.assertTrue(ref['task_stack']['open_tasks'])
        workspace=Conversation(fault=lambda out,_:out.update(intent='workspace',
            workspace_request={'operation':'read','scope':'workspace','paths':[]}))
        self.provider=lambda stage:workspace
        fresh=self.step(prompt='Please review the architecture of this codebase.', chat_key=chat_key,armed=False)
        self.assertEqual(fresh['state'],'NEED_ARM')

    def test_supplied_document_text_cannot_create_workspace_review(self):
        def propose(out,packet):
            if 'repair' not in packet:
                out.update(intent='workspace')
            else:
                out.update(answer='Lead with the validation work.')
        router=Conversation(fault=propose)
        self.provider=lambda stage:router
        with patch('repository_tools.execute',side_effect=AssertionError('Document review read the repository')):
            result=self.step(prompt='Review this résumé.\n\nRésumé text:\nValidated a PCIe interface on first silicon.',armed=False)
        self.assertEqual(result['state'],'DONE')
        self.assertNotIn('request_notice',result)
        self.assertEqual(result['conversation']['result']['accepted_output']['answer'],'Lead with the validation work.')
        self.assertEqual(len(router.payloads),2)
        self.assertNotIn('active',self.meta)
        router.payloads.clear()
        with patch('repository_tools.execute',side_effect=AssertionError('Document review read the repository')):
            followup=self.step(prompt='Could you review it again?',parent=result['conversation_id'],armed=False)
        self.assertEqual(followup['state'],'DONE')
        self.assertEqual(followup['conversation']['result']['accepted_output']['answer'],'Lead with the validation work.')
        self.assertEqual(len(router.payloads),2)

    def test_old_misrouted_document_flow_cannot_reenter_workspace(self):
        prompt='Can you look at the resume now please?'
        with self.s.db:
            saved=m.create(self.s,'flow',workspace=str(self.ws),objective=prompt,prompt=prompt)
            m.transition(self.s,saved['id'],'CLASSIFYING','REQUEST')
            m.transition(self.s,saved['id'],'NEED_ARM','CLASSIFIED',{'permission':{'operation':'read','paths':[]}})
        before=flow.response(self.s,saved['id'])
        with patch('repository_tools.execute',side_effect=AssertionError('Saved document request read the repository')):
            self.assertIn('Document text:',self.step(before,event='inspect')['request_notice'])
            self.assertIn('Document text:',self.step(before)['request_notice'])
        self.assertEqual(flow.response(self.s,saved['id']),before)
        self.assertNotIn('active',self.meta)

    def test_paused_pasted_document_can_resume_conversation(self):
        with patch('conversation.answer',side_effect=ContextBudgetExceeded('Small model context')):
            paused=self.step(prompt='Review this paragraph.\n\nDocument text:\nValidated PCIe on first silicon.',
                             armed=False)
        self.assertEqual(paused['stop']['resume_phase'],'CLASSIFYING')
        resumed=self.step(paused,event='resume',armed=False)
        self.assertEqual(resumed['state'],'CLASSIFYING')
        self.assertNotIn('request_notice',resumed)
        self.provider=lambda stage:Conversation()
        self.assertEqual(self.step(resumed,armed=False)['state'],'DONE')

    def test_replacing_review_keeps_edit_intent_and_requires_permission(self):
        out=self.children();sid=self.meta['active'];self.operation='edit'
        edit_prompt='Remember project Cedar, ticket CED724. Edit part0.py'
        deferred=self.step(prompt=edit_prompt,armed=False)
        self.assertEqual(deferred['deferred_flow_id'],out['flow_id'])
        self.assertFalse(self.s.snapshot(sid)['constraints']['edits_allowed'])
        self.step(out,event='cancel')
        start=self.step(event='start_existing',prompt=edit_prompt,conversation_id=deferred['conversation_id'])
        self.assertEqual(start['state'],'NEED_ARM')
        with self.assertRaisesRegex(InvalidUpdate,'user-armed'):self.step(start,armed=False)
        needed=self.step(start)
        self.assertEqual(needed['state'],'NEED_PERMISSION')
        self.assertEqual(needed['permission']['operation'],'edit')
        self.assertFalse(self.s.snapshot(sid)['constraints']['edits_allowed'])

    def test_settlement_is_idempotent_and_illegal_transition_is_rejected(self):
        out=self.children();cid=out['children'][0]['id'];self.step(out);used=m.spent(self.s,cid)
        row=self.s.db.execute('SELECT id FROM orchestration_charges WHERE node_id=? LIMIT 1',(cid,)).fetchone()
        with self.s.db:m.settle(self.s,{'node_id':cid,'charge_id':row[0]},'repeat',99999)
        self.assertEqual(used,m.spent(self.s,cid))
        with self.s.db,self.assertRaisesRegex(InvalidUpdate,'forbidden'):m.transition(self.s,cid,'CLASSIFYING','BAD')

    def test_rejected_child_is_quarantined_and_cannot_join(self):
        out=self.children();cid=out['children'][0]['id'];original=Child.call
        def corrupt(child,*args):
            proposal,record=original(child,*args);proposal['constraints']=[];return proposal,record
        with patch.object(Child,'call',corrupt):out=self.step(out)
        self.assertEqual(m.state(self.s,cid),'QUARANTINED')
        self.assertEqual(m.spent(self.s,cid)['calls'],3)
        self.assertEqual(out['state'], 'PAUSED')
        self.assertEqual(out['stop']['status'], 'child_review_rejected')
        self.assertTrue(out['stop']['receipt'])
        with self.assertRaisesRegex(InvalidUpdate, 'not ready to join'):
            m.join(self.s, m.node(self.s, cid)['parent_id'])

    def test_corrupt_transition_chain_is_detected_before_resume(self):
        out=self.children();fid=out['flow_id']
        # Simulate corruption outside the append-only API, not an allowed update.
        self.s.db.execute('DROP TRIGGER immutable_orchestration_events_UPDATE')
        self.s.db.execute("UPDATE orchestration_events SET digest='tampered' WHERE node_id=? AND seq=1",(fid,));self.s.db.commit()
        with self.assertRaisesRegex(InvalidUpdate,'corrupted'):self.step(out,event='inspect')

    def test_child_larger_than_page_allocation_pages_fresh_sources_across_model_switch(self):
        from vscode_bridge import handle
        import repository_tools as repo
        for i in range(6):
            text=f'def part{i}():\n    return {i}\n'+('# padding\n'*2200)
            (self.ws/f'part{i}.py').write_text(text)
        handle(self.root,self.ws,{'op':'auto_new','goal':self.prompt,'roots':['.'],'review_page_kib':32})
        self.meta=json.loads((self.root/'workspace.json').read_text())
        out=self.children();sid=self.meta['active'];contract=self.s.pinned(sid)
        parent=m.node(self.s,out['children'][0]['id'])['parent_id']
        before=m.spent(self.s,parent)
        out=self.step(out);after=m.spent(self.s,parent)
        self.assertGreaterEqual(after['operations']-before['operations'],2)
        self.s.close();self.s=InvocationLedger(self.root/'state.sqlite');self.tier='high'
        result=self.until('DONE',out)
        self.assertTrue(result['data']['result']['admission']['admitted'])
        self.assertEqual(contract,self.s.pinned(sid))
        for stage,model in self.models:
            if stage!='subagent':continue
            packet=model.payloads[0];self.assertGreaterEqual(len(packet['snapshot_receipts']),2)
            files=set()
            for rid in packet['snapshot_receipts']:
                row=repo.record_by_id(self.s,sid,rid)
                self.assertLessEqual(row['result']['read_limit'],32*1024)
                self.assertLessEqual(sum(len(t.encode()) for t in row['result']['sources'].values()),32*1024)
                self.assertEqual(row['operation']['orchestration']['node_id'],packet['branch']['id'])
                files.update(row['result']['sources'])
            self.assertEqual(files,set(m.node(self.s,packet['branch']['id'])['files']))
        self.assertTrue(self.s.verify_invocations(sid))

    def test_policy_upgrade_offers_explicit_restart_without_refunding_or_reusing_children(self):
        out=self.children();out=self.step(out);sid=self.meta['active'];old_flow=out['flow_id']
        contract=self.s.pinned(sid);used=review_budget.usage(self.s,sid)['used'];calls=len(self.models)
        import conversation_memory as cm
        with patch('governance.policy_hash',return_value='f'*64), patch.object(cm,'LEGACY_POLICIES',cm.LEGACY_POLICIES|{self.s.logic_hash}):
            self.s.close();self.s=InvocationLedger(self.root/'state.sqlite')
            blocked=self.step(out)
            self.assertEqual(blocked['recovery']['kind'],'policy_changed')
            self.assertTrue(blocked['recovery']['restart_allowed'])
            self.assertEqual(len(self.models),calls)
            before=flow.response(self.s,old_flow)
            greeting=flow.handle(self.s,self.meta,self.ws,self.root,
                {'event':'start','prompt':'hello there','models':MODELS,'tier':'low','armed':False},
                lambda stage:Conversation())
            self.assertEqual(greeting['state'],'DONE')
            self.assertTrue(greeting['conversation']['result']['admission']['admitted'])
            self.assertEqual(flow.response(self.s,old_flow),before)
            self.assertEqual(review_budget.usage(self.s,sid)['used'],used)
            self.assertEqual(self.s.pinned(sid),contract)
            with self.assertRaisesRegex(InvalidUpdate,'user-armed'):self.step(blocked,event='restart_review',armed=False)
            restarted=self.step(blocked,event='restart_review')
            self.assertNotEqual(restarted['flow_id'],old_flow)
            self.assertEqual(m.state(self.s,old_flow),'CANCELLED')
            self.assertEqual(flow.info(self.s,restarted['flow_id'])[0]['prompt'],self.prompt)
            self.assertEqual(restarted['conversation_id'],out['conversation_id'])
            self.assertEqual(review_budget.usage(self.s,sid)['used'],used)
            self.assertEqual(self.s.pinned(sid),contract)
            self.tier='high';done=self.until('DONE',restarted)
            self.assertTrue(done['data']['result']['admission']['admitted'])
            self.assertEqual(self.s.pinned(sid),contract)
            self.assertGreater(review_budget.usage(self.s,sid)['used']['calls'],used['calls'])
            packet=next(p for stage,p in reversed(self.models) if stage=='synthesis' and 'claims' not in p.payloads[0]).payloads[0]
            self.assertIn('CED724',canonical(packet['memory']))

    def test_changed_contract_does_not_allow_policy_only_restart(self):
        from vscode_bridge import save_contract
        out=self.children();sid=self.meta['active'];constraints=self.s.snapshot(sid)['constraints']
        save_contract(self.s,sid,{'goal':'Different user goal','constraints':constraints},self.ws)
        blocked=self.step(out)
        self.assertEqual(blocked['recovery']['kind'],'binding_changed')
        self.assertFalse(blocked['recovery']['restart_allowed'])
        with self.assertRaisesRegex(InvalidUpdate,'policy-only'):
            self.step(blocked,event='restart_review',binding=flow.identity(self.s,sid))

    def test_exact_full_page_allows_a_following_empty_source_file(self):
        import static_analysis
        text='def source(): return 1\n'
        (self.ws/'full.py').write_text(text);(self.ws/'empty.py').write_text('')
        page=static_analysis.snapshot(self.ws,['full.py','empty.py'],max_total=len(text.encode()))
        self.assertEqual(set(page['sources']),{'full.py','empty.py'})

    def test_compiler_config_is_not_a_separate_review_partition(self):
        from test_support import ENV,HAS_TYPESCRIPT
        if not HAS_TYPESCRIPT:self.skipTest('Node/TypeScript required')
        for path in self.ws.glob('*.py'):path.unlink()
        (self.ws/'entry.ts').write_text('export function main(){return 1;}\n')
        (self.ws/'tsconfig.json').write_text('{"compilerOptions":{"strict":true}}')
        with patch.dict('os.environ',ENV):out=self.until('SYNTHESIS')
        self.assertFalse(out['children'])

    def test_ranked_child_excerpts_keep_scope_after_canonical_storage(self):
        for i,name in enumerate(('main.py','a.py','b.py','c.py')):
            (self.ws/f'part{i}.py').rename(self.ws/name)
        out=self.children();out=self.step(out)
        worker=[p for stage,p in self.models if stage=='subagent'][0]
        packet=worker.payloads[0]
        self.assertNotEqual(list(packet['excerpts']),sorted(packet['excerpts']))
        self.assertEqual(packet['source_paths'],sorted(packet['excerpts']))
        from subagent_runtime import request_check
        for paths in (packet['source_paths']+packet['source_paths'][:1],['outside.py']):
            bad=copy.deepcopy(packet);bad['source_paths']=paths
            with self.assertRaisesRegex(InvalidUpdate,'scope changed'):request_check(self.s,self.meta['active'],bad)
        self.s.close();self.s=InvocationLedger(self.root/'state.sqlite');self.tier='high'
        self.assertTrue(self.until('DONE',out)['data']['result']['admission']['admitted'])

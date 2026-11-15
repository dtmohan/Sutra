from test_support import requires_sandbox
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from context_manager import InvalidUpdate
from invocation_gateway import InvocationLedger, consume_dispatch
from paging import Budget, PageFault
from review_role import constraint_rows
from host_actions import execute, preview, files_at, run_tests_snapshot, execution_context, source_path, run_provenance
from monitoring import resource_state, dashboard, require_resources, ResourceWait
from vscode_bridge import handle, dispatch

SOURCE='''MAX_ATTEMPTS = 3
def retry(operation):
    for attempt in range(MAX_ATTEMPTS - 1):
        try:
            return operation()
        except TimeoutError:
            if attempt == MAX_ATTEMPTS - 1:
                raise
'''
TEST='''import unittest
from src.retry import retry
class RetryTests(unittest.TestCase):
    def test_third_attempt(self):
        calls = []
        def operation():
            calls.append(1)
            if len(calls) < 3:
                raise TimeoutError()
            return "done"
        self.assertEqual(retry(operation), "done")
        self.assertEqual(len(calls), 3)
    def test_success(self):
        self.assertEqual(retry(lambda: "ok"), "ok")
'''


class Coder:
    model='fixture-model-A';budget=Budget()
    def __init__(self,fault=None):self.calls=[];self.fault=fault
    def call(self,cid,system,payload,schema):
        consume_dispatch(cid,self.model,system,payload,schema);self.calls.append(copy.deepcopy(payload))
        mode=payload['mode']
        action={'kind':'report','target':'','old':'','new':''}
        if mode=='edit':action.update(kind='edit',target='src/retry.py',old='range(MAX_ATTEMPTS - 1)',new='range(MAX_ATTEMPTS)')
        if mode=='test':action.update(kind='run_tests',target='tests/test_retry.py')
        answer={'task_id':payload['contract']['task_id'],'constraints':constraint_rows(payload['contract']),
            'summary':'Use the host receipts to continue the retry task.','findings':[],
            'completed':[],'remaining':['Check current tests.'],'action':action,
            'complete':mode=='review' and payload['execution']['tests']=='passed_current'}
        if self.fault:self.fault(answer)
        return answer,{'call_id':cid,'ok':True,'answer':answer}


@unittest.skipIf(os.name=='nt','Atomic action fixtures require POSIX; Windows adapter is read-only')
class ActionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve();self.workspace=self.root/'project';self.workspace.mkdir()
        for d in ('src','tests'):(self.workspace/d).mkdir()
        (self.workspace/'src/retry.py').write_text(SOURCE);(self.workspace/'tests/test_retry.py').write_text(TEST)
        self.storage=self.root/'store'
        self.request={'op':'new','goal':'Fix retries without changing permissions.',
            'constraints':{'allowed_paths':['src/retry.py','tests/test_retry.py'],'network_allowed':False,'dependencies_allowed':False,
                'edits_allowed':True,'tests_allowed':True,'test_paths':['tests/test_retry.py'],'test_timeout_ms':5000,'max_actions':4}}
        handle(self.storage,self.workspace,self.request)
        self.meta=json.loads((self.storage/'workspace.json').read_text());self.session=self.meta['active'];self.s=InvocationLedger(self.storage/'state.sqlite')
    def tearDown(self):self.s.close();self.tmp.cleanup()
    def propose(self,mode='edit',model=None):
        return dispatch(self.s,self.meta,self.workspace,self.storage,{'op':'work','mode':mode,'model':'fixture-model-A',
            'prompt':'Continue this bounded task.','pages':['src/retry.py#1','tests/test_retry.py#1']},model or Coder())
    def apply(self,r):return execute(self.s,self.session,r['result']['invocation_id'],self.workspace,self.storage,resource_check=False)
    def action_flow(self,iid):
        import orchestration as m
        with self.s.db:
            f=m.create(self.s,'flow',objective='Fix retries')
            for state in ('CLASSIFYING','NEED_ARM','BINDING','WORKSPACE','CODING'):
                m.transition(self.s,f['id'],state,'TEST_TRANSITION')
            m.transition(self.s,f['id'],'AWAITING_ACTION','ACTION_PROPOSED',{'invocation_id':iid})
        return f['id']
    def test_workflow_action_tracks_confirmation_execution_and_receipt(self):
        import orchestration as m
        r=self.propose();fid=self.action_flow(r['result']['invocation_id'])
        self.assertEqual(m.state(self.s,fid),'AWAITING_ACTION')
        receipt=self.apply(r);self.assertEqual(receipt['result']['status'],'applied')
        self.assertEqual(m.state(self.s,fid),'DONE')
        self.assertEqual(m.events(self.s,fid)[-1]['data']['receipt'],receipt['id'])
    def test_cancel_executing_action_waits_for_reconciliation_and_does_not_replay(self):
        import orchestration as m
        from lifecycle import reconcile
        r=self.propose();fid=self.action_flow(r['result']['invocation_id'])
        with patch('host_actions.replace_file',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):self.apply(r)
        self.assertEqual(m.state(self.s,fid),'EXECUTING_ACTION')
        root=self.s.db.execute("SELECT id FROM orchestration_nodes WHERE json_extract(record_json,'$.kind')='task' ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        charged=m.spent(self.s,root);m.cancel(self.s,fid)
        self.assertEqual(m.state(self.s,fid),'CANCELLING');self.assertEqual(charged,m.spent(self.s,root))
        with patch('host_actions.replace_file',side_effect=AssertionError('replayed')):reconcile(self.s,self.workspace)
        self.assertEqual(m.state(self.s,fid),'DONE');self.assertEqual(charged,m.spent(self.s,root))
        self.assertEqual(m.events(self.s,fid)[-1]['data']['status'],'reconciled_before')

    def test_cancelled_workflow_cannot_execute_old_proposal(self):
        import orchestration as m
        r=self.propose();fid=self.action_flow(r['result']['invocation_id']);m.cancel(self.s,fid)
        with self.assertRaisesRegex(InvalidUpdate,'no longer pending'):self.apply(r)
        self.assertIn('range(MAX_ATTEMPTS - 1)',(self.workspace/'src/retry.py').read_text())
    def test_edit_is_bound_to_exact_preimage_and_receipted(self):
        before=self.s.current_contract(self.session);r=self.propose();p=preview(self.s,self.session,r['result']['invocation_id'],self.workspace)
        self.assertIn('-    for attempt in range(MAX_ATTEMPTS - 1):',p['diff'])
        receipt=self.apply(r);self.assertEqual(receipt['result']['status'],'applied')
        self.assertIn('range(MAX_ATTEMPTS):',(self.workspace/'src/retry.py').read_text())
        self.assertEqual(before,self.s.current_contract(self.session));self.assertTrue(self.s.audit_valid())
    def test_interrupted_tests_recover_with_inconclusive_provenance(self):
        r=self.propose('test')
        with patch('host_actions.execution_identity',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):self.apply(r)
        from lifecycle import reconcile
        reconcile(self.s,self.workspace)
        raw=self.s.db.execute('SELECT record_json FROM host_action_receipts ORDER BY rowid DESC LIMIT 1').fetchone()[0]
        result=json.loads(raw)['result']
        self.assertEqual(result['status'],'interrupted')
        self.assertEqual(result['provenance']['transition'],'inconclusive')
        self.assertEqual(result['provenance']['test_paths'],['tests/test_retry.py'])
        self.assertFalse(result['assertions_verified'])

    def test_failed_runtime_observation_gets_inconclusive_provenance(self):
        r=self.propose('test')
        with patch('host_actions.execution_identity',side_effect=RuntimeError('unavailable')):
            result=self.apply(r)['result']
        self.assertEqual(result['status'],'failed_or_uncertain')
        self.assertEqual(result['provenance']['transition'],'inconclusive')
        self.assertIsNone(result['provenance']['execution'])

    def test_test_confirmation_lists_the_exact_snapshot_grant(self):
        result = self.propose('test')
        shown = preview(self.s, self.session, result['result']['invocation_id'], self.workspace)
        actual = files_at(self.s, self.session, self.workspace)
        self.assertEqual(shown['snapshot_files'], sorted(actual))
        self.assertIn('required project modules', shown['snapshot_note'])
    def test_replay_returns_original_receipt_without_second_write(self):
        r=self.propose();first=self.apply(r);second=self.apply(r)
        self.assertEqual(first['id'],second['id']);self.assertTrue(second['replayed_receipt'])
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM host_actions').fetchone()[0],1)
    def test_external_edit_blocks_stale_proposal(self):
        r=self.propose();(self.workspace/'src/retry.py').write_text(SOURCE+'# newer user edit\n')
        with self.assertRaises((InvalidUpdate,PageFault)):self.apply(r)
        self.assertTrue((self.workspace/'src/retry.py').read_text().endswith('# newer user edit\n'))
    def test_revoked_permission_blocks_existing_proposal(self):
        r=self.propose();update=copy.deepcopy(self.request);update['op']='save';update['constraints']['edits_allowed']=False
        handle(self.storage,self.workspace,update)
        with self.assertRaises((InvalidUpdate,PageFault)):self.apply(r)
    def test_invalid_syntax_and_new_external_import_rejected(self):
        for text in ('range(MAX_ATTEMPTS))','range(MAX_ATTEMPTS):\n        import not_an_installed_dependency\n        for x in []'):
            r=self.propose(model=Coder(lambda a:a['action'].update(new=text)))
            self.assertFalse(r['result']['admission']['admitted'])
        self.assertEqual((self.workspace/'src/retry.py').read_text(),SOURCE)
    def test_wrong_preimage_rejected(self):
        r=self.propose(model=Coder(lambda a:a['action'].update(old='absent code')))
        self.assertFalse(r['result']['admission']['admitted'])
    def test_constraint_omission_rejected(self):
        r=self.propose(model=Coder(lambda a:a['constraints'].pop()))
        self.assertFalse(r['result']['admission']['admitted'])
    def test_false_completion_rejected_before_tests(self):
        r=self.propose('review',Coder(lambda a:a.update(complete=True)))
        self.assertFalse(r['result']['admission']['admitted'])
    def test_source_receipt_preserves_progress_when_model_changes(self):
        first=self.propose();self.apply(first);other=Coder();other.model='fixture-model-B';second=self.propose('test',other)
        p=other.calls[0];self.assertIsNone(p['handoff']);self.assertEqual(p['execution']['recent'][0]['status'],'applied')
        self.assertEqual(p['execution']['recent'][0]['model'],'fixture-model-A')
        self.assertTrue(second['result']['admission']['admitted'])
    def test_action_budget_survives_switch_and_stops_before_inference(self):
        update=copy.deepcopy(self.request);update['op']='save';update['constraints']['max_actions']=1;handle(self.storage,self.workspace,update)
        self.apply(self.propose());other=Coder();other.model='fixture-model-B'
        with self.assertRaisesRegex(InvalidUpdate,'COMPOUND_FIN'):self.propose('test',other)
        self.assertEqual(other.calls,[])
        self.assertEqual(dashboard(self.storage)['action_budget']['decision'],'COMPOUND_FIN')
    def test_interrupted_action_is_not_replayed(self):
        r=self.propose()
        with patch('host_actions.receipt',side_effect=RuntimeError('simulated host interruption')):
            with self.assertRaises(RuntimeError):self.apply(r)
        with self.assertRaisesRegex(InvalidUpdate,'pending or interrupted'):self.apply(r)
        with self.assertRaisesRegex(InvalidUpdate,'earlier action is pending'):self.propose('test')
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM host_actions').fetchone()[0],1)
    def test_symlink_and_hardlink_targets_are_rejected(self):
        original=self.workspace/'src/retry.py';target=self.root/'outside.py';target.write_text(SOURCE);original.unlink();original.symlink_to(target)
        with self.assertRaises(InvalidUpdate):source_path(self.workspace,'src/retry.py')
        original.unlink();os.link(target,original)
        with self.assertRaises(InvalidUpdate):source_path(self.workspace,'src/retry.py')
    def test_action_receipt_tampering_rejected_by_database(self):
        self.apply(self.propose())
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):self.s.db.execute("UPDATE host_action_receipts SET record_json='{}'")
    def observed(self,r):
        # Stand-in for the macOS sandbox so the receipt logic runs on every platform.
        def run(directory,files,target,timeout_ms,root=None):
            ok='range(MAX_ATTEMPTS)' in files['src/retry.py']['text'] or 'len(calls) < 1' in files['tests/test_retry.py']['text']
            return {'status':'process_exited_zero' if ok else 'process_failed','returncode':0 if ok else 1,
                    'assertions_verified':False,'elapsed_seconds':.01,'output_tail':''}
        with patch('host_actions.run_tests_snapshot',run),patch('sys.platform','darwin'):return self.apply(r)
    def test_red_to_green_is_recorded_only_when_test_files_are_unchanged(self):
        failed=self.observed(self.propose('test'))['result']
        self.assertEqual(failed['provenance']['transition'],'first_run');self.assertIsNone(failed['provenance']['prior_run'])
        self.apply(self.propose('edit'));passed=self.observed(self.propose('test'))['result']
        self.assertEqual(passed['provenance']['transition'],'red_to_green')
        self.assertEqual(passed['provenance']['test_paths_edited_in_task'],[])
        self.assertEqual({k:passed['provenance']['prior_run'][k] for k in ('status','tests_unchanged','sources_changed')},
                         {'status':'process_failed','tests_unchanged':True,'sources_changed':True})
        self.assertFalse(passed['assertions_verified'])
        runs=[r for r in execution_context(self.s,self.session,self.workspace)['recent'] if r['kind']=='run_tests']
        self.assertEqual([r['transition'] for r in runs],['first_run','red_to_green'])
        self.assertTrue(all(r['assertions_verified'] is False for r in runs))
    def test_passing_after_the_model_weakens_a_test_is_not_red_to_green(self):
        self.observed(self.propose('test'))
        weaken=lambda answer:answer['action'].update(target='tests/test_retry.py',old='if len(calls) < 3:',new='if len(calls) < 1:')
        self.assertEqual(self.apply(self.propose('edit',Coder(weaken)))['result']['status'],'applied')
        passed=self.observed(self.propose('test'))['result']
        self.assertEqual(passed['status'],'process_exited_zero')
        self.assertEqual(passed['provenance']['transition'],'tests_changed')
        self.assertEqual(passed['provenance']['test_paths_edited_in_task'],['tests/test_retry.py'])
        self.assertFalse(passed['provenance']['prior_run']['tests_unchanged'])
    @requires_sandbox
    def test_failed_then_passing_tests_bound_to_versions(self):
        failed=self.apply(self.propose('test'));self.assertEqual(failed['result']['status'],'process_failed')
        self.apply(self.propose('edit'));passed=self.apply(self.propose('test'))
        self.assertEqual(passed['result']['status'],'process_exited_zero');self.assertEqual(passed['result']['reported_result']['tests'],2)
        self.assertEqual(execution_context(self.s,self.session,self.workspace)['tests'],'process_exited_zero_current')
        report=self.propose('review');self.assertFalse(report['result']['accepted_output']['complete'])
        (self.workspace/'src/retry.py').write_text(SOURCE)
        self.assertNotEqual(execution_context(self.s,self.session,self.workspace)['tests'],'process_exited_zero_current')


class TestProvenanceTests(unittest.TestCase):
    EXECUTION={'contract_hash':'contract','epoch':1,'runtime_sha256':'runtime','runner_sha256':'runner','test_timeout_ms':1000}
    PATHS=['tests/test_a.py','tests/helper.py']
    def run_at(self,status,**versions):
        base={'src/a.py':'s1','tests/test_a.py':'t1','tests/helper.py':'h1'};base.update(versions)
        return ({'id':'run-'+status,'kind':'run_tests','target':'tests/test_a.py'},{'status':status,'artifact_versions':base,'provenance':{'version':2,'execution':self.EXECUTION,'test_paths':sorted(self.PATHS)}})
    def now(self,history,status,**versions):
        base={'src/a.py':'s2','tests/test_a.py':'t1','tests/helper.py':'h1'};base.update(versions)
        return run_provenance(history,'tests/test_a.py',self.PATHS,base,status,self.EXECUTION)
    def test_transitions(self):
        red=self.run_at('process_failed');green=self.run_at('process_exited_zero')
        self.assertEqual(self.now([red],'process_exited_zero')['transition'],'red_to_green')
        self.assertEqual(self.now([green],'process_exited_zero')['transition'],'green_to_green')
        self.assertEqual(self.now([green],'process_failed')['transition'],'green_to_red')
        self.assertEqual(self.now([red],'process_failed')['transition'],'red_to_red')
        self.assertEqual(self.now([],'process_exited_zero')['transition'],'first_run')
        # Same sources and tests, different outcome: a flaky or environment-dependent test.
        self.assertEqual(self.now([red],'process_exited_zero',**{'src/a.py':'s1'})['transition'],'red_to_green_without_source_change')
    def test_any_changed_or_newly_listed_test_path_withholds_red_to_green(self):
        red=self.run_at('process_failed')
        self.assertEqual(self.now([red],'process_exited_zero',**{'tests/helper.py':'h2'})['transition'],'tests_changed')
        self.assertEqual(self.now([red],'process_exited_zero',**{'tests/test_a.py':'t2'})['transition'],'tests_changed')
        absent=({'id':'x','kind':'run_tests','target':'tests/test_a.py'},{'status':'process_failed','artifact_versions':{'src/a.py':'s1','tests/test_a.py':'t1'}})
        self.assertEqual(self.now([absent],'process_exited_zero')['transition'],'inconclusive')
    def test_timeouts_stale_and_legacy_results_are_inconclusive(self):
        for status in ('timeout','resource_wait','output_limit','stale_sources','failed_or_uncertain','passed',None):
            self.assertEqual(self.now([self.run_at('process_failed')],status)['transition'],'inconclusive')
            self.assertEqual(self.now([],status)['transition'],'inconclusive')
            prior=({'id':'p','kind':'run_tests','target':'tests/test_a.py'},{'status':status})
            self.assertEqual(self.now([prior],'process_exited_zero')['transition'],'inconclusive')
    def test_changed_test_inventory_cannot_reclassify_a_helper_as_source(self):
        prior=self.run_at('process_failed')
        now={'src/a.py':'s1','tests/test_a.py':'t1','tests/helper.py':'altered'}
        result=run_provenance([prior],'tests/test_a.py',['tests/test_a.py'],now,'process_exited_zero',self.EXECUTION)
        self.assertEqual(result['transition'],'inconclusive')
        self.assertFalse(result['prior_run']['comparable'])
    def test_changed_or_missing_execution_identity_is_inconclusive(self):
        prior=self.run_at('process_failed')
        for key in self.EXECUTION:
            changed=dict(self.EXECUTION);changed[key]='changed'
            with self.subTest(key=key):
                result=run_provenance([prior],'tests/test_a.py',self.PATHS,prior[1]['artifact_versions'],'process_exited_zero',changed)
                self.assertEqual(result['transition'],'inconclusive')
        prior[1].pop('provenance')
        self.assertEqual(self.now([prior],'process_exited_zero')['transition'],'inconclusive')

    def test_history_selects_the_latest_run_of_the_same_target_and_applied_test_edits(self):
        other=({'id':'o','kind':'run_tests','target':'tests/other.py'},{'status':'process_failed','artifact_versions':{}})
        applied=({'id':'e1','kind':'edit','target':'tests/helper.py'},{'status':'applied'})
        failed_edit=({'id':'e2','kind':'edit','target':'tests/test_a.py'},{'status':'failed_or_uncertain'})
        source_edit=({'id':'e3','kind':'edit','target':'src/a.py'},{'status':'applied'})
        result=self.now([other,source_edit,failed_edit,self.run_at('process_exited_zero'),applied,self.run_at('process_failed')],'process_exited_zero')
        self.assertEqual(result['prior_run']['action_id'],'run-process_exited_zero')
        self.assertEqual(result['test_paths_edited_in_task'],['tests/helper.py'])
        self.assertEqual(result['target_sha256'],'t1')


class HardwareTests(unittest.TestCase):
    def test_resource_stop_retains_measurement_and_fresh_recheck(self):
        with tempfile.TemporaryDirectory() as tmp, patch('monitoring.time.time', return_value=1001):
            root = Path(tmp)
            sample = {'wall_time':1000, 'thermal_state':2, 'memory_pressure_raw':1,
                      'free_bytes':8*1024**3, 'inactive_bytes':0}
            (root/'telemetry.json').write_text(json.dumps(sample))
            with self.assertRaises(ResourceWait) as caught:
                require_resources(root)
            self.assertEqual(caught.exception.state['sample'], sample)
            self.assertEqual(caught.exception.state['decision'], 'BRIDGE')
            self.assertIn('OS thermal pressure:', str(caught.exception))
            self.assertIn('memory pressure: normal', str(caught.exception))
            self.assertIn('8.0 GiB available estimate', str(caught.exception))
            sample.update(thermal_state=0, memory_pressure_raw=4)
            (root/'telemetry.json').write_text(json.dumps(sample))
            with self.assertRaises(ResourceWait) as caught:
                require_resources(root)
            self.assertIn('memory pressure: critical', str(caught.exception))
            sample.update(memory_pressure_raw=1, wall_time=900)
            (root/'telemetry.json').write_text(json.dumps(sample))
            with self.assertRaises(ResourceWait) as caught:
                require_resources(root)
            self.assertFalse(caught.exception.state['available'])
            self.assertIn('stale', str(caught.exception))
            sample.update(wall_time=1000)
            (root/'telemetry.json').write_text(json.dumps(sample))
            self.assertTrue(require_resources(root)['admitted'])

    def test_missing_nan_and_stale_signals_do_not_mean_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.assertFalse(resource_state(root)['admitted'])
            sample={'wall_time':1000,'thermal_state':0,'memory_pressure_raw':1,'free_bytes':8*1024**3,'inactive_bytes':0}
            (root/'telemetry.json').write_text(json.dumps(sample));self.assertTrue(resource_state(root,1001)['admitted'])
            self.assertFalse(resource_state(root,1010)['admitted'])
            for free,inactive in ((float('nan'),0),(1e308,1e308)):
                sample.update(free_bytes=free,inactive_bytes=inactive);(root/'telemetry.json').write_text(json.dumps(sample));self.assertFalse(resource_state(root,1001)['admitted'])
    def test_thermal_and_memory_profiles_use_unchanged_kernel(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for thermal,pressure,expected in [(0,1,'ALLOW'),(1,1,'ALLOW'),(2,1,'BRIDGE'),(3,1,'FIN'),(0,2,'BRIDGE'),(0,4,'FIN')]:
                sample={'wall_time':1000,'thermal_state':thermal,'memory_pressure_raw':pressure,'free_bytes':8*1024**3,'inactive_bytes':0}
                (root/'telemetry.json').write_text(json.dumps(sample));self.assertEqual(resource_state(root,1001)['decision'],expected)


@requires_sandbox
class SandboxTests(unittest.TestCase):
    def test_project_dependency_requires_explicit_snapshot_inclusion(self):
        text = ('import unittest\nimport helper\n'
                'class Check(unittest.TestCase):\n'
                '    def test_value(self): self.assertEqual(helper.VALUE, 7)\n')
        for include, expected in ((False, 'process_failed'), (True, 'process_exited_zero')):
            with self.subTest(include=include), tempfile.TemporaryDirectory() as tmp:
                files = {'test_scope.py': {'text': text, 'sha256': sha256(text)}}
                if include:
                    files['helper.py'] = {'text': 'VALUE = 7\n', 'sha256': sha256('VALUE = 7\n')}
                result = run_tests_snapshot(Path(tmp).resolve(), files, 'test_scope.py', 3000)
                self.assertEqual(result['status'], expected, result['output_tail'])
                if not include:
                    self.assertIn('ModuleNotFoundError', result['output_tail'])

    def test_interrupt_kills_the_running_test_process_before_returning(self):
        from lifecycle import deadline, Interrupted
        import subprocess
        real=subprocess.Popen;processes=[]
        def track(*args,**kwargs):
            p=real(*args,**kwargs);processes.append(p);return p
        start=time.monotonic()
        with patch('host_actions.subprocess.Popen',side_effect=track),deadline(.4):
            with self.assertRaises(Interrupted):self.run_code('import time\ntime.sleep(60)\n',120000)
        self.assertTrue(processes);self.assertTrue(all(p.poll() is not None for p in processes));self.assertLess(time.monotonic()-start,4)

    def run_code(self,text,timeout=3000):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();canary=root/'outside.txt';canary.write_text('OUTSIDE_CANARY')
            text=text.replace('CANARY_PATH',repr(str(canary)))
            directory=root/'run';directory.mkdir();files={'tests/test_boundary.py':{'text':text,'sha256':sha256(text)}}
            result=run_tests_snapshot(directory,files,'tests/test_boundary.py',timeout)
            self.assertEqual(canary.read_text(),'OUTSIDE_CANARY');return result
    def test_network_private_read_write_fork_and_snapshot_write_are_denied(self):
        text='''import os, socket, subprocess, tempfile, unittest
from pathlib import Path
class BoundaryTests(unittest.TestCase):
    def test_network(self):
        with self.assertRaises(OSError):
            with socket.create_connection(("127.0.0.1",11434),timeout=1): pass
    def test_private_read(self):
        with self.assertRaises(PermissionError): Path(CANARY_PATH).read_text()
    def test_private_write(self):
        with self.assertRaises(PermissionError): Path(CANARY_PATH).write_text("changed")
    def test_snapshot_write(self):
        with self.assertRaises(PermissionError): Path(__file__).write_text("changed")
    def test_subprocess(self):
        with self.assertRaises(OSError): subprocess.run(["/usr/bin/true"],check=True)
    def test_scratch_allowed(self):
        with tempfile.TemporaryFile() as f: f.write(b"temporary")
'''
        r=self.run_code(text);self.assertEqual(r['status'],'process_exited_zero',r['output_tail']);self.assertEqual(r['reported_result']['tests'],6)
    def test_wall_timeout_terminates_test(self):
        r=self.run_code('import time\ntime.sleep(10)\n',200);self.assertEqual(r['status'],'timeout');self.assertLess(r['elapsed_seconds'],2)
    def test_no_tests_never_counts_as_passed(self):
        r=self.run_code('x=1\n');self.assertEqual(r['status'],'process_failed');self.assertEqual(r['reported_result']['tests'],0)
    def test_output_is_bounded(self):
        r=self.run_code('while True: print("x"*4096,flush=True)\n');self.assertEqual(r['status'],'output_limit')


def sha256(text):return hashlib.sha256(text.encode()).hexdigest()


if __name__=='__main__':unittest.main()

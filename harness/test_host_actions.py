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
from host_actions import execute, preview, files_at, run_tests_snapshot, execution_context, source_path
from monitoring import resource_state, dashboard
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
    def test_edit_is_bound_to_exact_preimage_and_receipted(self):
        before=self.s.current_contract(self.session);r=self.propose();p=preview(self.s,self.session,r['result']['invocation_id'],self.workspace)
        self.assertIn('-    for attempt in range(MAX_ATTEMPTS - 1):',p['diff'])
        receipt=self.apply(r);self.assertEqual(receipt['result']['status'],'applied')
        self.assertIn('range(MAX_ATTEMPTS):',(self.workspace/'src/retry.py').read_text())
        self.assertEqual(before,self.s.current_contract(self.session));self.assertTrue(self.s.audit_valid())
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
    @requires_sandbox
    def test_failed_then_passing_tests_bound_to_versions(self):
        failed=self.apply(self.propose('test'));self.assertEqual(failed['result']['status'],'process_failed')
        self.apply(self.propose('edit'));passed=self.apply(self.propose('test'))
        self.assertEqual(passed['result']['status'],'process_exited_zero');self.assertEqual(passed['result']['reported_result']['tests'],2)
        self.assertEqual(execution_context(self.s,self.session,self.workspace)['tests'],'process_exited_zero_current')
        report=self.propose('review');self.assertFalse(report['result']['accepted_output']['complete'])
        (self.workspace/'src/retry.py').write_text(SOURCE)
        self.assertNotEqual(execution_context(self.s,self.session,self.workspace)['tests'],'process_exited_zero_current')


class HardwareTests(unittest.TestCase):
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

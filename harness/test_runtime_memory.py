import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from context_manager import canonical, ContextBudgetExceeded
from karma_adapter import KarmaAdapter
from local_model import LocalModel
from model_context import allocation
from monitoring import ResourceWait
from paging import Budget
import runtime_memory as rm


class RuntimeMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.env=patch.dict(os.environ,{'SUTRA_RUNTIME_DIR':str(self.root/'shared'), 'SUTRA_MEMORY_OWNERSHIP':'shared'})
        self.env.start()
        self.healthy={'available':True,'admitted':True,'decision':'ALLOW','sample':{'wall_time':1,'free_bytes':20*rm.GIB,'inactive_bytes':0}}
        self.blocked=dict(self.healthy,admitted=False,decision='BRIDGE',reason='memory pressure')
        self.row={'name':'large','digest':'weights1','size':10*rm.GIB,'context_length':65536,'runner':'ggml','expires_at':'9999-01-01T00:00:00Z'}
        self.metadata={'details':{'format':'gguf','runner':'ggml'},'model_info':{'general.architecture':'llama','llama.context_length':65536,'llama.block_count':32,'llama.attention.head_count_kv':8,'llama.embedding_length':4096,'llama.attention.head_count':32}}
    def tearDown(self):
        self.env.stop();self.tmp.cleanup()
    def provider(self):
        p=LocalModel(self.root/'calls',port=11434,model='large',budget=Budget(65536,4096))
        p.context_metadata=self.metadata;p.memory_weights=10*rm.GIB
        return p
    def owned(self,**extra):
        data={'owned':{'name':'large','digest':'weights1','completed_at':1},'active':None,**extra}
        rm.write_state(data)

    def test_context_fits_packet_without_mutating_prompt_or_budget(self):
        p=self.provider();payload={'contract':{'goal':'review','constraints':{'network_allowed':False}},'recent_turns':['Keep X unchanged'],'pages':[{'value':'code'}]}
        original=canonical(payload);budget=p.budget
        context=rm.dispatch_context(p,'system',payload,{})
        self.assertGreaterEqual(context,budget.charge('system',payload,{})+4096)
        self.assertLess(context,65536);self.assertEqual(canonical(payload),original);self.assertEqual(p.budget,budget)
        p.budget=Budget(4096,1024)
        with self.assertRaises(ContextBudgetExceeded):rm.dispatch_context(p,'system',{'required':'x'*10000},{})

    def test_memory_adapter_uses_kernel_with_nondefault_k(self):
        from kernel_boundary import KernelBoundary
        a=KarmaAdapter(KernelBoundary(.3))
        for cost,expected in ((6,'ALLOW'),(9,'BRIDGE'),(20,'FIN')):
            self.assertEqual(a.memory_allocation(cost,10,4)['decision'],expected)
        self.assertEqual(a.memory_allocation(0,4,4)['decision'],'BRIDGE')

    def test_owned_engine_monitoring_and_recovery_never_contact_foreign_runtimes(self):
        self.owned()
        worker = {'ok': True, 'running': True, 'profile': 'sutra:engine/small',
                  'context_tokens': 16384, 'instance_id': 'a' * 32}
        with patch.dict(os.environ, SUTRA_ENGINE_ONLY='1', SUTRA_STRATA_ONLY='0'), \
                patch('engine_client.status', return_value=worker), \
                patch.object(rm, 'api', side_effect=AssertionError('Ollama access')), \
                patch.object(rm, 'native_inventory', side_effect=AssertionError('Strata access')), \
                patch.object(rm, 'running', side_effect=AssertionError('Ollama inventory')), \
                patch('monitoring.resource_state', return_value=self.blocked), \
                patch.object(rm, 'stable_resources', return_value=self.healthy):
            snapshot = rm.snapshot()
            recovered = rm.recover(self.root)
        self.assertEqual(snapshot['backend'], 'sutra_engine')
        self.assertEqual(snapshot['ownership'], 'sutra_owned')
        self.assertIsNone(snapshot['resident_models'][0]['size'])
        self.assertEqual(snapshot['resident_models'][0]['name'], worker['profile'])
        self.assertTrue(recovered['admitted'])
        self.assertEqual(recovered['max_release_actions'], 0)
        self.assertFalse(recovered['automatic_inference'])
        self.assertEqual(rm.read_state()['owned']['digest'], 'weights1')

    def test_configure_reduces_optional_context_cap_before_paging(self):
        p=self.provider();hardware=dict(self.healthy,sample={'free_bytes':16*rm.GIB,'inactive_bytes':0})
        with patch('monitoring.require_resources',return_value=hardware),patch.object(rm,'running',return_value=[]):
            rm.configure(p,self.root,self.metadata,10*rm.GIB)
        budget,diagnostics=allocation(p.context_metadata)
        self.assertGreater(budget.context_limit,4096);self.assertLess(budget.context_limit,65536)
        self.assertEqual(budget.context_limit,diagnostics['memory_context_cap'])
        self.assertEqual(rm.read_state()['plan']['context_tokens'], budget.context_limit)
        self.assertEqual(rm.read_state()['events'][-1]['event'], 'context_planned')

    def test_required_context_preflight_expands_only_under_fresh_memory_admission(self):
        p = self.provider(); p.budget = Budget(8192, 1024, 256)
        p.context_metadata = dict(self.metadata, _sutra_runtime_cap=8192)
        original = p.budget
        observed = {'required_input': 13000, 'output_reserve': 1024,
                    'required_context': 14024, 'context_limit': 8192,
                    'pinned_statements': 0, 'stored_turns': 2}
        with patch.object(rm, 'fresh_resources', return_value=self.healthy), \
                patch.object(rm, 'running', return_value=[]), patch.object(rm, 'api') as api:
            info = rm.refit_required_context(p, self.root, observed)
        api.assert_not_called()
        self.assertGreaterEqual(p.budget.context_limit, observed['required_context'])
        self.assertEqual(p.budget.response_reserve, original.response_reserve)
        self.assertEqual(p.budget.template_reserve, original.template_reserve)
        self.assertEqual(info['effective_context_tokens'], p.budget.context_limit)
        self.assertEqual(rm.read_state()['plan']['context_observation'], observed)
        self.assertEqual(rm.read_state()['events'][-1]['event'], 'mandatory_context_planned')

    def test_required_context_shortage_stays_resource_wait_without_budget_mutation(self):
        p = self.provider(); p.budget = Budget(8192, 1024, 256)
        p.context_metadata = dict(self.metadata, _sutra_runtime_cap=8192)
        original = p.budget
        hardware = dict(self.healthy, sample={'free_bytes': 14 * rm.GIB, 'inactive_bytes': 0})
        observed = {'required_input': 13000, 'output_reserve': 1024,
                    'required_context': 14024, 'context_limit': 8192,
                    'pinned_statements': 0, 'stored_turns': 2}
        with patch.object(rm, 'fresh_resources', return_value=hardware), \
                patch.object(rm, 'running', return_value=[]), patch.object(rm, 'api') as api:
            with self.assertRaisesRegex(ResourceWait, 'mandatory conversation packet') as stopped:
                rm.refit_required_context(p, self.root, observed)
        api.assert_not_called()
        self.assertEqual(p.budget, original)
        self.assertEqual(stopped.exception.state['runtime']['stage'], 'context_preflight')
        self.assertFalse(stopped.exception.state['runtime']['inference_started'])
        self.assertEqual(rm.read_state()['events'][-1]['event'], 'mandatory_context_paused')

    def test_context_refit_never_exceeds_architecture_or_bypasses_os_pressure(self):
        p = self.provider(); p.budget = Budget(8192, 1024, 256)
        p.context_metadata = dict(self.metadata, _sutra_runtime_cap=8192)
        with patch.object(rm, 'fresh_resources', side_effect=ResourceWait(self.blocked)), \
                patch.object(rm, 'running') as inventory:
            self.assertIsNone(rm.refit_required_context(p, self.root, {'required_context': 70000}))
            with self.assertRaises(ResourceWait):
                rm.refit_required_context(p, self.root, {'required_context': 14000})
        inventory.assert_not_called()
        self.assertEqual(p.budget.context_limit, 8192)

    def test_unknown_mlx_kv_is_not_reported_as_a_verified_bound(self):
        meta=dict(self.metadata,details={'format':'safetensors'})
        p=rm.estimate('large',meta,10*rm.GIB,[],8192,self.healthy)
        self.assertIsNone(p['kv_bytes_per_token']);self.assertFalse(p['peak_memory_verified'])
        self.assertIn('unknown',p['estimate_scope'])

    def test_resident_same_model_is_not_charged_full_weights_twice(self):
        p=rm.estimate('large',self.metadata,10*rm.GIB,[self.row],8192,self.healthy,manifest_digest='weights1')
        self.assertEqual(p['resident_weight_credit_bytes'],10*rm.GIB)
        self.assertEqual(p['estimated_increment_bytes'],8192*rm.kv_bytes_per_token(self.metadata))
        p=rm.estimate('other-alias',self.metadata,10*rm.GIB,[self.row],8192,self.healthy)
        self.assertGreaterEqual(p['estimated_increment_bytes'],10*rm.GIB)

    def test_memory_shortage_blocks_before_model_dispatch(self):
        p=self.provider();hardware=dict(self.healthy,sample={'free_bytes':5*rm.GIB,'inactive_bytes':0})
        with patch('monitoring.require_resources',return_value=hardware),patch.object(rm,'running',return_value=[]):
            with self.assertRaises(ResourceWait):
                with rm.dispatch(p,self.root,'id','system',{},{}):self.fail('must not dispatch')
        self.assertEqual(rm.read_state()['events'][-1]['event'],'allocation_paused')

    def test_shared_recovery_never_unloads_even_with_old_exclusive_ownership(self):
        self.owned()
        with patch.object(rm,'running',return_value=[self.row]),patch.object(rm,'api') as api,patch('monitoring.resource_state',return_value=self.blocked):
            result=rm.recover(self.root)
        api.assert_not_called();self.assertFalse(result['admitted']);self.assertEqual(result['action'],'observe_only_shared_server')

    def test_exclusive_release_is_bounded_verified_and_audited(self):
        self.owned();audits=[]
        with patch.dict(os.environ,{'SUTRA_MEMORY_OWNERSHIP':'exclusive'}),patch.object(rm,'running',side_effect=[[self.row],[]]),patch.object(rm,'api') as api,patch('monitoring.resource_state',return_value=self.blocked),patch.object(rm,'stable_resources',return_value=self.healthy):
            result=rm.recover(self.root,lambda kind,value:audits.append((kind,value)))
        api.assert_called_once_with('/api/generate',{'model':'large','keep_alive':0,'stream':False})
        self.assertTrue(result['admitted']);self.assertEqual(result['action'],'released_owned_model');self.assertFalse(result['automatic_inference'])
        self.assertEqual([a[0] for a in audits],['MEMORY_RELEASE_PLANNED','MEMORY_RECOVERY_OBSERVED'])
        self.assertEqual(result['release_kernel']['decision'],'ALLOW');self.assertIsNone(rm.read_state()['owned'])
        with patch.dict(os.environ,{'SUTRA_MEMORY_OWNERSHIP':'exclusive'}),patch.object(rm,'running',return_value=[]),patch.object(rm,'api') as api,patch('monitoring.resource_state',return_value=self.blocked):rm.recover(self.root)
        api.assert_not_called()

    def test_release_refuses_replaced_model_or_interrupted_owner(self):
        for replacement,active in ((dict(self.row,digest='new'),None),(self.row,{'call_id':'interrupted'})):
            self.owned(active=active)
            with patch.dict(os.environ,{'SUTRA_MEMORY_OWNERSHIP':'exclusive'}),patch.object(rm,'running',return_value=[replacement]),patch.object(rm,'api') as api,patch('monitoring.resource_state',return_value=self.blocked):rm.recover(self.root)
            api.assert_not_called()

    def test_failed_release_is_not_replayed(self):
        self.owned()
        with patch.dict(os.environ,{'SUTRA_MEMORY_OWNERSHIP':'exclusive'}),patch.object(rm,'running',return_value=[self.row]),patch.object(rm,'api',side_effect=ResourceWait(self.blocked)),patch('monitoring.resource_state',return_value=self.blocked):
            with self.assertRaises(ResourceWait):rm.recover(self.root)
        self.assertIsNone(rm.read_state()['owned'])

    def test_stability_requires_distinct_fresh_healthy_measurements(self):
        second=dict(self.healthy,sample=dict(self.healthy['sample'],wall_time=2))
        with patch('monitoring.resource_state',side_effect=[self.healthy,second]),patch.object(rm.time,'sleep'):
            self.assertTrue(rm.stable_resources(self.root)['admitted'])
        with patch('monitoring.resource_state',return_value=self.healthy),patch.object(rm.time,'sleep'),patch.object(rm.time,'monotonic',side_effect=[0,0,4]):
            result=rm.stable_resources(self.root)
        self.assertFalse(result['admitted']);self.assertEqual(result['decision'],'BRIDGE')

    def test_completed_call_release_observation_and_interruption(self):
        p=self.provider()
        with patch('monitoring.require_resources',return_value=self.healthy),patch.object(rm,'running',side_effect=[[],[]]):
            with rm.dispatch(p,self.root,'id','system',{},{}):p.runtime_completed=True
        self.assertEqual(rm.read_state()['events'][-1]['status'],'unloaded');self.assertEqual(p.keep_alive,0)
        with patch.dict(os.environ,{'SUTRA_MEMORY_OWNERSHIP':'exclusive'}),patch.object(rm,'fresh_resources',return_value=self.healthy),patch.object(rm,'running',return_value=[self.row]):
            with self.assertRaises(KeyboardInterrupt):
                with rm.dispatch(p,self.root,'next','system',{},{}):raise KeyboardInterrupt()
        self.assertIsNone(rm.read_state()['owned']);self.assertTrue(rm.read_state()['active']['uncertain'])

    def test_cross_process_slot_and_crash_releases_os_lock(self):
        child_env=dict(os.environ,PYTHONPATH=str(Path(__file__).parent))
        script="import runtime_memory as m\nwith m.lease(): print('acquired',flush=True)\n"
        with rm.lease():
            result=subprocess.run([sys.executable,'-B','-c',script],env=child_env,capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0);self.assertIn('Another Sutra call',result.stderr)
        child=subprocess.Popen([sys.executable,'-B','-c',"import time,runtime_memory as m\nwith m.lease():\n print('held',flush=True)\n time.sleep(30)"],env=child_env,stdout=subprocess.PIPE,text=True)
        try:self.assertEqual(child.stdout.readline().strip(),'held')
        finally:child.kill();child.wait();child.stdout.close()
        with rm.lease():pass

    def test_orphaned_inference_requires_observed_release(self):
        self.owned(active={'model':'large','call_id':'old'})
        with patch('monitoring.require_resources',return_value=self.healthy),patch.object(rm,'running',return_value=[self.row]):
            with self.assertRaisesRegex(ResourceWait,'interrupted'):
                with rm.dispatch(self.provider(),self.root,'new','system',{},{}):self.fail('orphaned call still resident')

    def test_transport_and_inventory_failures_are_recoverable(self):
        with patch.object(rm.http.client.HTTPConnection,'request',side_effect=OSError('offline')):
            with self.assertRaisesRegex(ResourceWait,'unavailable'):rm.running()
        with patch.object(rm,'running',side_effect=ResourceWait(self.blocked)):
            self.assertFalse(rm.snapshot()['inventory_available'])

    def test_all_native_monitor_and_recovery_never_query_ollama(self):
        rows = [{'name': 'strata:profile/native', 'size': None,
                 'context_length': 8192, 'runner': 'strata'}]
        with patch.dict(os.environ, {'SUTRA_STRATA_ONLY': '1'}), \
                patch.object(rm, 'running', side_effect=AssertionError('Ollama dependency')), \
                patch.object(rm, 'native_inventory', return_value=rows), \
                patch('monitoring.resource_state', return_value=self.healthy), \
                patch.object(rm, 'stable_resources', return_value=self.healthy):
            observed = rm.snapshot()
            self.assertEqual(observed['backend'], 'strata')
            self.assertEqual(observed['ownership'], 'native_owner')
            self.assertEqual(observed['resident_models'], rows)
            recovery = rm.recover(self.root)
            self.assertTrue(recovery['admitted'])
            self.assertEqual(recovery['action'], 'observe_native_runtime')
            self.assertEqual(recovery['max_release_actions'], 0)

    def test_native_plan_retains_existing_runtime_evidence(self):
        rm.write_state({'owned': {'name': 'previous-ollama'}, 'events': []})
        rm.note_native_plan({'model': 'strata:profile/native', 'decision': 'ALLOW'})
        saved = rm.read_state()
        self.assertEqual(saved['plan']['provider'], 'strata')
        self.assertEqual(saved['owned']['name'], 'previous-ollama')
        with patch.dict(os.environ, {'SUTRA_STRATA_ONLY': '1'}), \
                patch.object(rm, 'native_inventory', side_effect=OSError('unavailable')):
            self.assertFalse(rm.snapshot()['inventory_available'])


if __name__=='__main__':unittest.main()

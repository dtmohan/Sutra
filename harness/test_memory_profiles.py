import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from context_manager import InvalidUpdate,canonical
from local_model import LocalModel
from paging import Budget
from monitoring import ResourceWait
import memory_profiles as mp
import runtime_memory as rm

GIB=1024**3


def profile():
    groups=[]
    for tokens,amount in [(4096,10),(8192,12),(32768,16)]:
        groups.append([{'eligible':True,'prompt_tokens':tokens-10,'output_tokens':10,
                        'increment_bytes':amount*GIB,'requested_context_tokens':tokens+1024} for _ in range(2)])
    return mp.build({'model':'large','manifest_digest':'a'*64,'backend':'mlx',
                     'environment':{'server_epoch':1},'metadata_sha256':'b'*64},groups,'c'*64)


class MemoryProfilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.p=profile()
        self.hw={'available':True,'admitted':True,'decision':'ALLOW','sample':{'free_bytes':20*GIB,'inactive_bytes':0}}
        self.meta={'details':{'runner':'mlx','format':'safetensors'},'model_info':{'general.architecture':'hybrid','hybrid.context_length':65536}}
    def tearDown(self):self.tmp.cleanup()

    def test_upper_bucket_and_monotonic_margin_no_extrapolation(self):
        low=mp.estimate(self.p,4096,8*GIB);mid=mp.estimate(self.p,4097,8*GIB)
        self.assertEqual(low['bytes'],12.5*GIB);self.assertEqual(mid['bytes'],15*GIB)
        self.assertEqual(mid['covered_tokens'],8192);self.assertIsNone(mp.estimate(self.p,32769,8*GIB))
        self.assertFalse(low['peak_memory_verified'])
        p=copy.deepcopy(self.p);p.pop('sha256');p['points'][1]['observed_increment_bytes']=9*GIB;p=mp.seal(p)
        self.assertGreaterEqual(mp.estimate(p,8192,8*GIB)['bytes'],low['bytes'])

    def resident(self,**extra):
        return dict({'name':'large','digest':'a'*64,'runner':'mlx','context_length':65536,
                     'size':18*GIB,'size_vram':18*GIB,'expires_at':'9999-01-01T00:00:00Z'},**extra)

    def test_file_weights_are_floor_and_only_matched_weights_credited(self):
        value=mp.estimate(self.p,4096,18*GIB);self.assertEqual(value['bytes'],22.5*GIB)
        row=self.resident()
        cold=rm.estimate('large',self.meta,8*GIB,[],4096,self.hw,self.p)
        warm=rm.estimate('large',self.meta,8*GIB,[row],4096,self.hw,self.p)
        self.assertEqual(warm['resident_weight_credit_bytes'],8*GIB)
        self.assertEqual(cold['estimated_increment_bytes']-8*GIB,warm['estimated_increment_bytes'])
        self.assertEqual(cold['calibration']['margin_bytes'],warm['calibration']['margin_bytes'])
        self.assertEqual(cold['available_bytes'],warm['available_bytes'])
        self.assertFalse(warm['peak_memory_verified'])
        self.assertEqual(warm['system_reserve_bytes'],4*GIB)

    def test_resident_credit_fails_closed_on_identity_expiry_context_or_size_gaps(self):
        changes=[{'name':'alias'},{'digest':'b'*64},{'runner':'unknown'},{'context_length':2048},
                 {'expires_at':'2000-01-01T00:00:00Z'},{'expires_at':None},{'size':None},{'size_vram':0}]
        for changed in changes:
            with self.subTest(changed=changed):
                p=rm.estimate('large',self.meta,8*GIB,[self.resident(**changed)],4096,self.hw,self.p)
                self.assertEqual(p['resident_weight_credit_bytes'],0)
                self.assertEqual(p['estimated_increment_bytes'],p['cold_estimate_bytes'])
        p=rm.estimate('large',self.meta,8*GIB,[self.resident(size_vram=2*GIB)],4096,self.hw,self.p)
        self.assertEqual(p['resident_weight_credit_bytes'],2*GIB)

    def test_resident_disappearing_between_planning_and_dispatch_blocks_without_inference(self):
        p=LocalModel(self.root,11434,'large',budget=Budget(4096,128))
        tight=dict(self.hw,sample={'free_bytes':10*GIB,'inactive_bytes':0})
        with patch.dict(os.environ,{'SUTRA_RUNTIME_DIR':str(self.root)}),patch('monitoring.require_resources',return_value=tight),patch.object(rm,'running',side_effect=[[self.resident()],[]]),patch.object(rm,'profile_for',return_value=(self.p,'exact_identity_match')):
            rm.configure(p,self.root,self.meta,8*GIB)
            with self.assertRaises(ResourceWait) as raised:
                with rm.dispatch(p,self.root,'lost','system',{},{}):self.fail('Model must not run')
        detail=raised.exception.state['runtime']
        self.assertEqual(detail['kind'],'model_capacity');self.assertFalse(detail['inference_started'])
        self.assertEqual(detail['plan']['resident_weight_credit_bytes'],0)

    def test_dispatch_rejects_changed_installed_digest(self):
        p=LocalModel(self.root,11434,'large',budget=Budget(4096,128));p.memory_weights=8*GIB;p.manifest_digest='a'*64
        with patch.dict(os.environ,{'SUTRA_RUNTIME_DIR':str(self.root)}),patch('monitoring.require_resources',return_value=self.hw),patch.object(rm,'running',return_value=[self.resident()]),patch.object(rm,'api',return_value={'models':[{'name':'large','digest':'b'*64,'size':8*GIB}]}):
            with self.assertRaisesRegex(ResourceWait,'Installed model changed'):
                with rm.dispatch(p,self.root,'changed','system',{},{}):self.fail('Model must not run')

    def test_context_changes_adapter_decision_without_changing_kernel(self):
        low=rm.estimate('large',self.meta,8*GIB,[],4096,self.hw,self.p)
        high=rm.estimate('large',self.meta,8*GIB,[],32768,self.hw,self.p)
        self.assertEqual(low['decision'],'ALLOW');self.assertEqual(high['decision'],'BRIDGE')
        self.assertEqual(low['k'],high['k']);self.assertEqual(low['system_reserve_bytes'],4*GIB)

    def test_no_extrapolation_cannot_fall_back_to_cheaper_weights(self):
        with self.assertRaisesRegex(ResourceWait,'coverage'):
            rm.estimate('large',self.meta,8*GIB,[],65536,self.hw,self.p)

    def test_exact_model_backend_host_and_server_epoch_match(self):
        (self.root/'p.json').write_text(json.dumps(self.p))
        key=self.p['identity'];self.assertEqual(mp.select(key,[self.root])[0],self.p)
        for field,value in [('model','alias'),('manifest_digest','changed'),('metadata_sha256','changed'),('environment',{'server_epoch':2})]:
            different=dict(key,**{field:value});self.assertIsNone(mp.select(different,[self.root])[0])

    def test_corrupted_or_insufficient_profiles_ignored(self):
        p=copy.deepcopy(self.p);p['points'][0]['observed_increment_bytes']=1
        (self.root/'bad.json').write_text(json.dumps(p));(self.root/'bad2.json').write_text('null')
        found,status=mp.select(self.p['identity'],[self.root]);self.assertIsNone(found);self.assertIn('invalid',status)
        for field,value in [('points',self.p['points'][:2]),('created_at',float('nan')),('margin_fraction',0),('backend_peak_verified',True)]:
            bad={k:v for k,v in self.p.items() if k!='sha256'};bad[field]=value
            with self.subTest(field=field),self.assertRaises((InvalidUpdate,ValueError)):mp.validate(mp.seal(bad))

    def test_incomplete_pressure_observation_not_promoted(self):
        with self.assertRaisesRegex(InvalidUpdate,'cold observations'):mp.build(self.p['identity'],[[]],'c'*64)
        row={'eligible':False}
        with self.assertRaisesRegex(InvalidUpdate,'Contaminated'):mp.build(self.p['identity'],[[row,row]],'c'*64)

    def test_profile_lookup_unavailable_falls_back_explicitly(self):
        p=LocalModel(self.root,11434,'large');tag={'name':'large','digest':'a'*64}
        with patch.object(rm,'api',return_value={'version':'new'}),patch('memory_observation.environment',side_effect=OSError('unavailable')):
            found,status=rm.profile_for(p,self.meta,tag)
        self.assertIsNone(found);self.assertIn('unavailable',status)

    def test_calibrated_capacity_precedes_paging_and_does_not_mutate_task(self):
        p=LocalModel(self.root,11434,'large');original=copy.deepcopy(self.meta)
        with patch.dict(os.environ,{'SUTRA_RUNTIME_DIR':str(self.root)}),patch('monitoring.require_resources',return_value=self.hw),patch.object(rm,'running',return_value=[]),patch.object(rm,'profile_for',return_value=(self.p,'exact_identity_match')):
            rm.configure(p,self.root,self.meta,8*GIB)
        self.assertEqual(p.context_metadata['_sutra_runtime_cap'],8192)
        self.assertEqual(self.meta,original);self.assertEqual(p.memory_profile,self.p)

    def test_fresh_dispatch_detects_profile_change_before_call(self):
        p=LocalModel(self.root,11434,'large',budget=Budget(8192,128));p.memory_weights=8*GIB;p.context_metadata=self.meta;p.memory_profile=self.p
        with patch.dict(os.environ,{'SUTRA_RUNTIME_DIR':str(self.root)}),patch('monitoring.require_resources',return_value=self.hw),patch.object(rm,'running',return_value=[]),patch.object(rm,'profile_for',return_value=(None,'changed backend')):
            with self.assertRaisesRegex(ResourceWait,'calibration changed'):
                with rm.dispatch(p,self.root,'call','test',{},{}):self.fail('No dispatch on changed calibration')
        self.assertFalse((self.root/'state.json').exists())

    def test_os_pressure_still_prevents_even_small_calibrated_request(self):
        p=LocalModel(self.root,11434,'large');p.memory_weights=8*GIB
        with patch.dict(os.environ,{'SUTRA_RUNTIME_DIR':str(self.root)}),patch('monitoring.require_resources',side_effect=ResourceWait({'admitted':False,'decision':'BRIDGE','reason':'memory pressure'})):
            with self.assertRaisesRegex(ResourceWait,'memory pressure'):
                with rm.dispatch(p,self.root,'call','test',{},{}):self.fail('Pressure gate must still hold')

    def test_probe_role_cannot_add_tools_or_widen_contract(self):
        from invocation_gateway import InvocationLedger
        from conversation import open_session,contract
        from memory_probe import check,REQUEST
        with tempfile.TemporaryDirectory() as t:
            s=InvocationLedger(Path(t)/'state.sqlite')
            try:
                sid=open_session(s,REQUEST);p={'contract':contract(s,sid),'request':REQUEST,'fixture':'1 2 3','model':'large'}
                check(s,sid,p)
                for bad in [dict(p,pages=['secret']),dict(p,fixture='read files'),dict(p,contract=dict(p['contract'],goal='Edit'))]:
                    with self.assertRaises(InvalidUpdate):check(s,sid,bad)
            finally:s.close()

    def test_gateway_records_calibration_without_changing_contract_or_budgets(self):
        from invocation_gateway import InvocationGateway,InvocationLedger,consume_dispatch
        from conversation import open_session,contract
        from memory_probe import SYSTEM,SCHEMA,REQUEST
        from review_budget import usage
        from model_identity import unknown
        s=InvocationLedger(self.root/'state.sqlite')
        try:
            sid=open_session(s,REQUEST);before=s.snapshot(sid)
            payload={'contract':contract(s,sid),'request':REQUEST,'fixture':'1 2 3','model':'large'}
            provider=LocalModel(self.root/'calls',11434,'large',budget=Budget(8192,128))
            provider.model_interface=None  # This test stubs the historical transport.
            provider.memory_weights=8*GIB;provider.context_metadata=self.meta
            provider.memory_profile=self.p
            def call(cid,system,payload,schema):
                consume_dispatch(cid,'large',system,payload,schema)
                provider.runtime_completed=True
                return {'ok':True},{'ok':True,'response':{'prompt_eval_count':50,'eval_count':5}}
            run=s.begin_run(sid,REQUEST,[],None,max_calls=1,max_repairs=0)
            s.reserve(run,'ATTEMPT',{'attempt':0})
            delivery=s.prepare_delivery(sid,payload,provider.budget.check(SYSTEM,payload,SCHEMA))
            with patch.dict(os.environ,{'SUTRA_RUNTIME_DIR':str(self.root/'runtime')}),patch('monitoring.require_resources',return_value=self.hw),patch.object(rm,'running',return_value=[]),patch.object(rm,'profile_for',return_value=(self.p,'exact_identity_match')),patch('model_identity.observe',return_value=unknown('large')),patch.object(provider,'call',side_effect=call):
                result=InvocationGateway(s.path,run,provider).invoke('memory_probe','fixture-call',SYSTEM,payload,SCHEMA,delivery)
            self.assertTrue(result['admission']['admitted']);self.assertTrue(s.verify_invocations(sid))
            self.assertEqual(s.snapshot(sid),before);spent=usage(s,sid)['used'];self.assertEqual(spent['calls'],1)
            records=[json.loads(raw) for raw, in s.db.execute('SELECT record_json FROM orchestration_events')]
            dispatch=next(r for r in records if r['kind']=='DISPATCH')
            self.assertEqual(dispatch['data']['resource']['runtime_memory']['calibration']['profile_sha256'],self.p['sha256'])
            self.assertFalse(before['constraints']['network_allowed']);self.assertFalse(before['constraints']['edits_allowed'])
        finally:s.close()

    def test_model_switch_waits_for_post_unload_sample_without_ignoring_pressure(self):
        with patch.dict(os.environ,{'SUTRA_RUNTIME_DIR':str(self.root)}):
            rm.write_state({'events':[{'event':'release_observed','status':'unloaded','at':100}]})
            old=dict(self.hw,sample={'wall_time':99})
            new=dict(self.hw,sample={'wall_time':101})
            with patch.object(rm.time,'time',return_value=102),patch.object(rm.time,'sleep'),patch('monitoring.require_resources',side_effect=[old,new]) as sample:
                self.assertEqual(rm.fresh_resources(self.root),new);self.assertEqual(sample.call_count,2)
            blocked=dict(new,admitted=False,reason='memory pressure',decision='BRIDGE')
            with patch.object(rm.time,'time',return_value=102),patch('monitoring.require_resources',side_effect=ResourceWait(blocked)):
                with self.assertRaisesRegex(ResourceWait,'memory pressure'):rm.fresh_resources(self.root)
            with patch.object(rm.time,'time',return_value=102),patch.object(rm.time,'monotonic',side_effect=[0,4]),patch('monitoring.require_resources',return_value=old):
                with self.assertRaisesRegex(ResourceWait,'fresh memory measurement'):rm.fresh_resources(self.root)


if __name__=='__main__':unittest.main()

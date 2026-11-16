import base64
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import capabilities as tools
import capability_packages as packages
import capability_registry as registry
import conversation
from context_manager import InvalidUpdate
from invocation_gateway import InvocationLedger
from review_budget import usage
from test_conversation import Fake, MODELS
from vscode_bridge import handle


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name)
        self.ws=self.base/'repo';self.ws.mkdir();(self.ws/'code.py').write_text('import os\nanswer: int = "wrong"\n')
        self.root=self.base/'store'
        state=handle(self.root,self.ws,{'op':'auto_new','goal':'Project Cedar CED724. Keep kernel unchanged; no edits or tests.','roots':['.']})
        self.sid=state['session'];self.s=InvocationLedger(self.root/'state.sqlite')
        self.meta=json.loads((self.root/'workspace.json').read_text())
        self.hw=patch('monitoring.require_resources',return_value={'admitted':True,'decision':'ALLOW'});self.hw.start()
        self.space=patch('storage_policy.require_space',return_value={});self.space.start()
    def tearDown(self):self.s.close();self.hw.stop();self.space.stop();self.tmp.cleanup()
    def preview(self,action='install',paths=None):return tools.preview(self.s,self.sid,self.ws,self.root,'ruff',action,paths or [])
    def approve(self,p,**updates):
        auth={'kind':'user','proposal_id':p['id']};auth.update(updates)
        return tools.execute(self.s,self.sid,self.ws,self.root,p['id'],auth)
    def install(self):
        def fixture(root,name):
            dest=packages.folder(root,name);dest.mkdir(parents=True);(dest/'binary').write_bytes(b'verified fixture')
            return {'status':'installed','files':packages.hashes(dest)}
        p=self.preview()
        with patch.object(packages,'install',side_effect=fixture):return self.approve(p)
    def test_auth_required_and_replay_rejected_without_effects(self):
        p=self.preview();before=usage(self.s,self.sid)
        for auth in (None,{}, {'kind':'model','proposal_id':p['id']},{'kind':'user','proposal_id':'forged'}):
            with self.assertRaises(InvalidUpdate),patch.object(packages,'install') as install:
                tools.execute(self.s,self.sid,self.ws,self.root,p['id'],auth)
            install.assert_not_called()
        self.assertEqual(before,usage(self.s,self.sid))
        with patch.object(packages,'install',return_value={'status':'failed'}) as install:
            self.approve(p)
            with self.assertRaisesRegex(InvalidUpdate,'already consumed'):self.approve(p)
            self.assertEqual(install.call_count,1)
    def test_snapshot_permissions_and_changed_source(self):
        self.install();p=self.preview('check',['code.py'])
        (self.ws/'code.py').write_text('x=2\n')
        with self.assertRaisesRegex(InvalidUpdate,'Source changed'):self.approve(p)
        for paths in (['../outside.py'],['.vscode/settings.json'],['code.py','code.py']):
            with self.assertRaises(InvalidUpdate):self.preview('check',paths)
    def test_tampered_installation_cannot_execute(self):
        self.install();self.assertTrue(tools.installed(self.s,self.root,'ruff'))
        (packages.folder(self.root,'ruff')/'binary').write_bytes(b'changed')
        self.assertFalse(tools.installed(self.s,self.root,'ruff'))
        with self.assertRaisesRegex(InvalidUpdate,'Install'):self.preview('check')
    def test_budget_rechecked_after_approval_and_failed_work_charged(self):
        p=self.preview()
        from karma_adapter import KarmaAdapter
        blocked=KarmaAdapter().allocations({'operations':2},{'operations':1},{'operations':1})
        with patch('orchestration.gates',return_value=[blocked]),patch.object(packages,'install') as install:
            with self.assertRaises(InvalidUpdate):self.approve(p)
        install.assert_not_called()
        before=usage(self.s,self.sid)['used']
        with patch.object(packages,'install',side_effect=RuntimeError('offline')):r=self.approve(p)
        after=usage(self.s,self.sid)['used'];self.assertEqual(r['result']['status'],'failed')
        self.assertEqual(after['operations'],before['operations']+1)
        self.assertGreaterEqual(after['seconds'],before['seconds'])
    def test_interruption_recovered_without_replay_or_refund(self):
        p=self.preview()
        with patch.object(packages,'install',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):self.approve(p)
        used=usage(self.s,self.sid)['used'];self.s.close();self.s=InvocationLedger(self.root/'state.sqlite')
        result=tools.recover(self.s)
        self.assertEqual(result[0]['result']['status'],'interrupted')
        self.assertEqual(used,usage(self.s,self.sid)['used'])
        with self.assertRaisesRegex(InvalidUpdate,'already consumed'):self.approve(p)
        self.assertEqual(tools.recover(self.s),[])
    def test_approval_and_budget_survive_model_switch_and_ledger_reopen(self):
        before=self.s.snapshot(self.sid);p=self.preview()
        first=conversation.answer(self.s,self.meta,self.root,{'prompt':'Remember Cedar CED724. No edits, tests, network or project dependencies.','models':MODELS,'tier':'low'},Fake('small'))
        self.s.close();self.s=InvocationLedger(self.root/'state.sqlite')
        second_provider=Fake('large')
        conversation.answer(self.s,self.meta,self.root,{'prompt':'Recall the task and constraints','parent':first['result']['invocation_id'],'models':MODELS,'tier':'high'},second_provider)
        self.assertIn('CED724',json.dumps(second_provider.payloads))
        self.assertEqual(before,self.s.snapshot(self.sid))
        with patch.object(packages,'install',return_value={'status':'failed'}):self.approve(p)
        spent=usage(self.s,self.sid)['used'];self.assertGreaterEqual(spent['operations'],2)
        self.s.close();self.s=InvocationLedger(self.root/'state.sqlite')
        self.assertEqual(spent,usage(self.s,self.sid)['used'])
        self.assertEqual(before,self.s.snapshot(self.sid))
    def test_registry_and_expiry_bound_to_proposal(self):
        p=self.preview()
        with patch('time.time',return_value=p['expires_at']+1):
            with self.assertRaisesRegex(InvalidUpdate,'expired'):self.approve(p)
        with patch.object(registry,'identity',return_value='changed'):
            with self.assertRaisesRegex(InvalidUpdate,'task changed'):self.approve(p)
    def test_package_hash_and_archive_paths(self):
        def archive(name):
            stream=io.BytesIO()
            with zipfile.ZipFile(stream,'w') as z:z.writestr(name,'x')
            return stream.getvalue()
        for name in ('../escape','/absolute','x\\y','C:drive'):
            raw=archive(name);spec={'format':'zip','integrity':'sha256-'+base64.b64encode(hashlib.sha256(raw).digest()).decode(),'max_download_bytes':10000,'max_expanded_bytes':100}
            target=self.base/'extract';target.mkdir(exist_ok=True)
            with self.assertRaises(InvalidUpdate):packages.unpack(raw,spec,target)
        with self.assertRaisesRegex(InvalidUpdate,'integrity'):packages.check_archive(spec,b'corrupted')
    def test_repair_orphan_install_requires_fresh_approval_and_quarantines_copy(self):
        old=packages.folder(self.root,'ruff');old.mkdir(parents=True);(old/'unreceipted').write_text('never execute')
        p=self.preview();self.assertTrue(p['repair_existing'])
        spec=registry.artifact('ruff');stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as z:z.writestr(spec['entry'],'fresh package')
        raw=stream.getvalue();spec=dict(spec,integrity='sha256-'+base64.b64encode(hashlib.sha256(raw).digest()).decode())
        with patch.object(packages,'download',return_value=raw) as download,patch.object(registry,'artifact',return_value=spec):
            with self.assertRaises(InvalidUpdate):tools.execute(self.s,self.sid,self.ws,self.root,p['id'],None)
            download.assert_not_called()
            r=self.approve(p)
        self.assertEqual(r['result']['status'],'installed')
        quarantine=old.parent/r['result']['quarantined_previous']
        self.assertEqual((quarantine/'unreceipted').read_text(),'never execute')
        self.assertTrue(tools.installed(self.s,self.root,'ruff'))
    def test_archive_links_expansion_and_redirects_rejected(self):
        import stat
        for mode,limit in ((stat.S_IFLNK|0o777,100),(stat.S_IFREG|0o644,1)):
            stream=io.BytesIO();entry=zipfile.ZipInfo('entry');entry.external_attr=mode<<16
            with zipfile.ZipFile(stream,'w') as z:z.writestr(entry,'target')
            raw=stream.getvalue();spec={'format':'zip','integrity':'sha256-'+base64.b64encode(hashlib.sha256(raw).digest()).decode(),'max_download_bytes':10000,'max_expanded_bytes':limit}
            with self.assertRaises(InvalidUpdate):packages.unpack(raw,spec,self.base/'unsafe')
        with self.assertRaisesRegex(InvalidUpdate,'redirects'):packages.NoRedirect().redirect_request(None,None,302,'',{},'https://example.com')
    def test_resources_and_disabled_policy_prevent_dispatch_without_charging(self):
        from monitoring import ResourceWait
        p=self.preview();before=usage(self.s,self.sid)['used']
        with patch.dict('os.environ',{'SUTRA_CAPABILITIES_MODE':'disabled'}),patch.object(packages,'install') as install:
            with self.assertRaisesRegex(InvalidUpdate,'disabled'):self.approve(p)
            install.assert_not_called()
        with patch('monitoring.require_resources',side_effect=ResourceWait({'decision':'BRIDGE'})),patch.object(packages,'install') as install:
            with self.assertRaises(ResourceWait):self.approve(p)
            install.assert_not_called()
        self.assertEqual(before,usage(self.s,self.sid)['used'])
    def test_native_model_proposal_has_no_install_authority(self):
        def ask(r,p):r.update(intent='workspace',workspace_request={'operation':'capability','capability':'ruff','capability_action':'check','paths':['code.py']})
        answer=conversation.answer(self.s,self.meta,self.root,{'prompt':'Lint code.py with Ruff','models':MODELS,'tier':'low'},Fake('small',ask))
        self.assertTrue(answer['result']['admission']['admitted'])
        self.assertFalse(tools.installed(self.s,self.root,'ruff'))
        self.assertFalse((self.root/'tools').exists())
    def test_native_flow_separates_install_and_run_approvals_across_models(self):
        import orchestration_flow as flow
        def provider(stage):
            return Fake('small',lambda r,p:r.update(intent='workspace',workspace_request={
                'operation':'capability','capability':'ruff','capability_action':'check','paths':['code.py']}))
        common={'models':MODELS,'tier':'low','armed':True}
        out=flow.handle(self.s,self.meta,self.ws,self.root,dict(common,event='start',prompt='Lint code.py with Ruff'),provider)
        def step(out,**kw):
            return flow.handle(self.s,self.meta,self.ws,self.root,dict(common,event='step',flow_id=out['flow_id'],
                cursor=out['cursor'],binding=out.get('binding'),**kw),provider)
        out=step(out);self.assertEqual(out['state'],'WORKSPACE')
        out=step(out);self.assertEqual(out['state'],'NEED_TOOL_APPROVAL')
        self.assertEqual(out['capability_proposal']['action'],'install')
        install_id=out['capability_proposal']['id'];before=self.s.snapshot(self.sid)
        def fixture(root,name):
            dest=packages.folder(root,name);dest.mkdir(parents=True);(dest/'binary').write_bytes(b'verified')
            return {'status':'installed','files':packages.hashes(dest)}
        with patch.object(packages,'install',side_effect=fixture):
            req=dict(common,event='capability_grant',flow_id=out['flow_id'],cursor=out['cursor'],binding=out['binding'],authorization={'kind':'user','proposal_id':install_id})
            out=flow.handle(self.s,self.meta,self.ws,self.root,req,provider)
        self.assertEqual(out['state'],'WORKSPACE')
        out=step(out);self.assertEqual(out['capability_proposal']['action'],'check')
        self.s.close();self.s=InvocationLedger(self.root/'state.sqlite');common['tier']='high'
        with patch.object(packages,'run',return_value={'status':'finished','returncode':1,'output':'[{"filename":"code.py","message":"unused import","location":{"row":1}}]'}) as run:
            req=dict(common,event='capability_grant',flow_id=out['flow_id'],cursor=out['cursor'],binding=out['binding'],authorization={'kind':'workspace_policy','policy_hash':'approved-settings','proposal_id':out['capability_proposal']['id']})
            original_move=flow.move
            def lost_reply(s,fid,state,*args,**kw):
                if state=='DONE':raise KeyboardInterrupt()
                return original_move(s,fid,state,*args,**kw)
            with patch.object(flow,'move',side_effect=lost_reply):
                with self.assertRaises(KeyboardInterrupt):flow.handle(self.s,self.meta,self.ws,self.root,req,provider)
            charged=usage(self.s,self.sid)['used']
            self.s.close();self.s=InvocationLedger(self.root/'state.sqlite')
            out=flow.handle(self.s,self.meta,self.ws,self.root,dict(common,event='inspect',flow_id=out['flow_id']),provider)
            self.assertTrue(out['capability_settled'])
            # Recovery consumes the existing receipt, not a second authorization.
            out=step(out)
            self.assertEqual(charged,usage(self.s,self.sid)['used'])
        self.assertEqual(out['state'],'DONE');run.assert_called_once()
        self.assertEqual(before,self.s.snapshot(self.sid))
        r=out['capability_result'];doc=self.s.document(self.sid,'capability/'+r['operation_id'])
        self.assertIn('unused import',doc['text']);self.assertEqual(doc['authority'],'tool')
        other=Fake('large')
        conversation.answer(self.s,self.meta,self.root,{'prompt':'Explain the diagnostics','models':MODELS,'tier':'high'},other)
        self.assertIn('unused import',other.payloads[0]['tool_evidence']['output_excerpt'])
        self.assertEqual(usage(self.s,self.sid)['used']['operations'],3) # catalog + install + check
    def test_kernel_unchanged(self):
        self.assertEqual(hashlib.sha256(Path(__file__).with_name('stp_canonical').joinpath('kernel.py').read_bytes()).hexdigest(),'9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac')


if __name__=='__main__':unittest.main()

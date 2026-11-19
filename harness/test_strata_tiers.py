"""All-native tier routing without contacting Ollama or replaying task history."""
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import conversation
from context_manager import InvalidUpdate
import model_identity
from model_catalog import catalog
from model_routing import choose
from review_budget import usage
import strata_model as strata
import test_strata_model as fixture


NAMES = {tier:'strata:profile/test-'+tier for tier in ('low','mid','high')}


class NativeTierTests(unittest.TestCase):
    setUp = fixture.StrataTests.setUp

    def native_fixture(self):
        profiles = {name:{'name':name,'model':'native-'+tier,'port':self.server.server_port,
                          'context_tokens':16384 if tier == 'low' else 24576 if tier == 'mid' else 32768,
                          'profile_revision':str(index)*16,'source_sha256':str(index)*64,
                          'eligible':True,'ready':False,'active':False}
                    for index,(tier,name) in enumerate(NAMES.items(), 1)}
        self.native_calls = []

        def command(action, name=None):
            self.native_calls.append((action,name))
            if action == 'catalog':
                return {'ok':True,'profiles':list(profiles.values())}
            value = copy.deepcopy(profiles[name])
            if action == 'activate':
                self.server.status.update(model=value['model'], loaded=True,
                                          started=self.server.status['started']+1,
                                          context={'max_positions':value['context_tokens']})
                return {'ok':True,'profile':value,'status':'ready','server':copy.deepcopy(self.server.status)}
            return {'ok':True,'profile':value}

        environment = patch.dict(os.environ, SUTRA_STRATA_ONLY='1',
                                 SUTRA_STRATA_MODELS=json.dumps(list(NAMES.values())),SUTRA_STRATA_CONTEXT='32768')
        environment.start(); self.addCleanup(environment.stop)
        native = patch.object(strata, 'native_command', side_effect=command)
        native.start(); self.addCleanup(native.stop)
        return profiles

    def test_cold_profile_metadata_does_not_load_or_call_status(self):
        self.native_fixture()
        self.server.status['loaded'] = False
        with patch.object(strata, 'status', side_effect=AssertionError('Cold metadata must not require a live server')):
            provider = strata.StrataModel(self.root/'calls', model=NAMES['low'])
            provider.fresh_context_metadata()
        self.assertEqual(provider.model,NAMES['low'])
        self.assertIsNone(provider.server_identity)
        self.assertFalse(any(action == 'activate' for action,_ in self.native_calls))
        self.assertEqual(self.server.posts,[])

    def test_cold_provider_lookup_does_not_require_a_second_cold_allocation(self):
        from api_runtime import provider_for
        self.native_fixture()
        with patch('runtime_memory.fresh_resources',side_effect=AssertionError('Admission belongs to native dispatch')):
            provider=provider_for(self.root,NAMES['low'])
        self.assertIsNone(provider.server_identity)
        self.assertFalse(any(action=='activate' for action,_ in self.native_calls))

    def test_all_native_tiers_keep_the_same_task_permissions_and_spending(self):
        self.native_fixture()
        parent = None
        contract = None
        with patch('vscode_bridge.ollama', side_effect=AssertionError('No Ollama dependency')):
            for index,tier in enumerate(('low','mid','high'), 1):
                result = conversation.answer(self.ledger,{},self.root,
                    {'prompt':'Hello' if parent is None else 'Continue this same conversation.',
                     'models':NAMES,'tier':tier,'parent':parent,'one_step':True})
                self.assertTrue(result['result']['admission']['admitted'],result)
                self.assertEqual(result['route']['model'],NAMES[tier])
                if contract is None:
                    contract = self.ledger.current_contract(result['session'])
                    session = result['session']
                else:
                    self.assertEqual(result['session'],session)
                    self.assertEqual(self.ledger.current_contract(session),contract)
                    self.assertTrue(result['memory_used'])
                budget = usage(self.ledger,session)
                self.assertEqual(budget['used']['calls'],index)
                self.assertEqual(budget['used']['tokens'],154*index)
                self.assertTrue(self.ledger.verify_invocations(session))
                parent = result['result']['invocation_id']
        self.assertEqual([name for action,name in self.native_calls if action == 'activate'],list(NAMES.values()))
        self.assertEqual([row[0]['model'] for row in self.server.posts],['native-low','native-mid','native-high'])

    def test_native_activation_happens_inside_the_runtime_lease(self):
        from contextlib import contextmanager
        self.native_fixture()
        inside = []
        original = strata.native_command
        @contextmanager
        def lease():
            inside.append(True)
            try: yield
            finally: inside.pop()
        def guarded(action,name=None):
            if action == 'activate': self.assertTrue(inside)
            return original(action,name)
        with patch.object(strata,'native_command',side_effect=guarded), \
             patch.dict(os.environ,SUTRA_RUNTIME_DIR=str(self.root/'runtime')), \
             patch('runtime_memory.enabled',return_value=True),patch('runtime_memory.lease',side_effect=lease), \
             patch('runtime_memory.fresh_resources',side_effect=lambda root:__import__('monitoring').require_resources(root)):
            result = conversation.answer(self.ledger,{},self.root,
                {'prompt':'Hello','models':NAMES,'tier':'low','one_step':True})
        self.assertTrue(result['result']['admission']['admitted'])
        self.assertFalse(inside)

    def test_profile_identity_survives_reload_but_not_profile_changes(self):
        profiles = self.native_fixture()
        first = model_identity.observe(NAMES['low'])
        self.server.status['started'] += 100
        self.assertEqual(first,model_identity.observe(NAMES['low']))
        self.assertIsNone(first['weights_sha256'])
        self.assertNotIn('server_started',first)
        profiles[NAMES['low']]['source_sha256'] = 'f'*64
        self.assertNotEqual(first,model_identity.observe(NAMES['low']))

    def test_activation_cannot_substitute_another_ready_model(self):
        self.native_fixture()
        original = strata.native_command
        def substitute(action,name=None):
            result = original(action,name)
            if action == 'activate':result['server']['model']='wrong-native-model'
            return result
        with patch.object(strata,'native_command',side_effect=substitute):
            with self.assertRaisesRegex(InvalidUpdate,'readiness does not match'):
                conversation.answer(self.ledger,{},self.root,
                    {'prompt':'Hello','models':NAMES,'tier':'low','one_step':True})
        self.assertEqual(self.server.posts,[])
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM invocations').fetchone()[0],0)

    def test_all_native_catalog_and_reviewer_selection_skip_ollama(self):
        self.native_fixture()
        with patch('vscode_bridge.ollama',side_effect=AssertionError('No Ollama dependency')) as ollama:
            rows = strata.append_catalog(catalog(ollama))
            self.assertEqual({row['name'] for row in rows['models']},set(NAMES.values()))
            self.assertTrue(all(row['loadable'] and row['provider']=='strata' for row in rows['models']))
            mapper = model_identity.observe(NAMES['high'])
            selected,separation = model_identity.choose(mapper)
        self.assertNotEqual(selected,NAMES['high'])
        self.assertEqual(separation['weight_relation'],'unknown')
        self.assertFalse(any(action=='activate' for action,_ in self.native_calls))
        ollama.assert_not_called()

    def test_automatic_native_reviewer_is_limited_to_assigned_profiles(self):
        self.native_fixture()
        with patch.dict(os.environ,SUTRA_STRATA_MODELS=json.dumps([NAMES['low'],NAMES['high']])), \
             patch('vscode_bridge.ollama',side_effect=AssertionError('No Ollama dependency')):
            self.assertEqual(model_identity.choose(model_identity.observe(NAMES['high']))[0],NAMES['low'])

    def test_managed_catalog_preserves_the_pinned_legacy_alias(self):
        self.native_fixture()
        original=strata.native_command
        def pinned(action,name=None):
            result=original(action,name)
            if action=='catalog':result['legacy_high_ref']=NAMES['high']
            return result
        with patch.dict(os.environ,SUTRA_STRATA_MODELS=json.dumps([NAMES['low'],strata.MODEL])), \
             patch.object(strata,'native_command',side_effect=pinned):
            rows=strata.append_catalog({'models':[],'errors':[]})['models']
        alias=next(row for row in rows if row['name']==strata.MODEL)
        self.assertTrue(alias['legacy_alias'])
        self.assertTrue(alias['eligible'])
        self.assertEqual(alias['parameter_size'],'native-high')

    def test_mixed_catalog_keeps_native_candidates_when_ollama_is_offline(self):
        self.native_fixture()
        with patch.dict(os.environ,SUTRA_STRATA_ONLY=''), \
             patch('vscode_bridge.ollama',side_effect=InvalidUpdate('Ollama is offline')) as offline:
            rows = strata.append_catalog(catalog(offline))
        self.assertEqual(len(rows['models']),3)
        self.assertTrue(any(error.get('provider')=='ollama' for error in rows['errors']))

    def test_native_profiles_are_supported_in_every_fixed_tier(self):
        for tier in ('low','mid','high'):
            route = choose('Hello',NAMES,tier,{'source_files':0,'selected_files':0,'unresolved_calls':0})
            self.assertEqual(route['model'],NAMES[tier])
        with self.assertRaises(InvalidUpdate):
            choose('Hello',{**NAMES,'low':'strata:profile/../escape'},'low',
                   {'source_files':0,'selected_files':0,'unresolved_calls':0})

    def test_native_coding_transport_uses_the_host_action_role_without_ollama(self):
        from vscode_bridge import handle, dispatch
        self.native_fixture()
        workspace=self.root/'workspace';workspace.mkdir()
        (workspace/'a.py').write_text('def answer():\n    return 42\n')
        before=(workspace/'a.py').read_bytes()
        created=handle(self.root,workspace,{'op':'new','goal':'Review this function without editing.',
            'constraints':{'allowed_paths':['a.py'],'network_allowed':False,'dependencies_allowed':False,
                           'edits_allowed':False,'tests_allowed':False}})
        metadata=json.loads((self.root/'workspace.json').read_text())
        self.server.frames=[fixture.frames({'summary':'The function returns a constant.', 'findings':[],
            'completed':[], 'remaining':[], 'action':{'kind':'report','target':'','old':'','new':''}})]
        with patch('vscode_bridge.ollama',side_effect=AssertionError('No Ollama dependency')):
            result=dispatch(self.ledger,metadata,workspace,self.root,
                {'op':'work','mode':'review','prompt':'Review this function.','model':NAMES['low'],'pages':['a.py#1']})
        self.assertTrue(result['result']['admission']['admitted'],result)
        self.assertEqual(result['result']['model'],NAMES['low'])
        self.assertEqual(self.ledger.current_contract(created['session']),created['contract'])
        self.assertEqual((workspace/'a.py').read_bytes(),before)


class CoordinatorBoundaryTests(unittest.TestCase):
    def test_unassigned_native_activation_fails_before_spawning(self):
        with patch.dict(os.environ,SUTRA_STRATA_MODELS=json.dumps([NAMES['low']])), \
             patch.object(strata.subprocess,'run') as run:
            with self.assertRaisesRegex(InvalidUpdate,'trusted model settings'):
                strata.native_command('activate',NAMES['high'])
        run.assert_not_called()

    def test_node_bridge_uses_fixed_argv_and_host_python(self):
        response = SimpleNamespace(returncode=0,stdout=json.dumps({'ok':True,'profiles':[]}).encode())
        with patch.dict(os.environ,KARMA_NODE='/trusted/node'), \
             patch.object(strata.subprocess,'run',return_value=response) as run:
            self.assertEqual(strata.native_command('catalog')['profiles'],[])
        arguments,options = run.call_args
        self.assertEqual(arguments[0][0],'/trusted/node')
        self.assertTrue(arguments[0][1].endswith('strata_runtime.js'))
        self.assertEqual(arguments[0][2:],['catalog'])
        self.assertEqual(options['env']['ELECTRON_RUN_AS_NODE'],'1')
        self.assertEqual(options['env']['SUTRA_PROVIDER_PYTHON'],__import__('sys').executable)
        self.assertNotIn('shell',options)

    def test_busy_native_load_returns_a_recoverable_resource_wait(self):
        from monitoring import ResourceWait
        response = SimpleNamespace(returncode=1,stdout=json.dumps({'ok':False,'status':'busy','error':'A native request is active'}).encode())
        with patch.dict(os.environ,KARMA_NODE='/trusted/node',SUTRA_STRATA_MODELS=json.dumps([NAMES['low']])), \
             patch.object(strata.subprocess,'run',return_value=response):
            with self.assertRaises(ResourceWait):strata.native_command('activate',NAMES['low'])

    def test_uncertain_native_activation_timeout_is_recoverable_without_replay(self):
        from monitoring import ResourceWait
        timeout = strata.subprocess.TimeoutExpired('native fixture',145)
        with patch.dict(os.environ,KARMA_NODE='/trusted/node',SUTRA_STRATA_MODELS=json.dumps([NAMES['low']])), \
             patch.object(strata.subprocess,'run',side_effect=timeout) as run:
            with self.assertRaisesRegex(ResourceWait,'owner may still be loading'):
                strata.native_command('activate',NAMES['low'])
        self.assertEqual(run.call_count,1)


if __name__ == '__main__': unittest.main()

"""Opt-in all-native tier continuity. Profile activation belongs to the provider.

Run low, mid, high and verify in separate processes with the same output/model
arguments. No User settings are changed. The operator must supply installed,
compatible named profiles and KARMA_NODE. Context profiles sharing one set of
weights validate switching, not differences in model capability or quality.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'harness'))
spec = importlib.util.spec_from_file_location('live_native_continuity_helpers',
                                            ROOT/'test/live-strata-management.py')
shared = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shared)
from context_manager import canonical, sha
from conversation import answer, chat_axis, session_for
from invocation_gateway import InvocationLedger
from lifecycle import deadline
from model_catalog import catalog
from model_routing import choose
from strata_model import append_catalog, is_strata, profile_metadata


PHASES = ('low','mid','high')
PROMPTS = {
    'low':('Remember project Cedar-724, ticket CED724. The target is 18 and the actual is 21. '
           'Never change the STP kernel. No network, edits, tests, or new dependencies. '
           'Keep this task conversation only. Reply with one sentence confirming the facts.'),
    'mid':('Recall the project and ticket from the previous turn, calculate actual minus target, '
           'and repeat what must stay unchanged and which operations are prohibited. '
           'Answer in one short paragraph without performing actions.'),
    'high':('Recall our saved project and ticket, the difference between actual and target, '
            'and all work restrictions. Give one short paragraph without performing actions.'),
}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--models',required=True,help='JSON object mapping low, mid and high to named native refs')
    parser.add_argument('--phase',choices=(*PHASES,'verify'),required=True)
    parser.add_argument('--thinking',choices=('off','adaptive'),default='off')
    args=parser.parse_args()
    models=json.loads(args.models)
    assert isinstance(models,dict) and set(models)==set(PHASES)
    assert all(is_strata(models[tier]) and models[tier].startswith('strata:profile/') for tier in PHASES)
    assert len(set(models.values()))==3,'Use three different native profile references'
    output=args.output.resolve()
    fixture_path=output/'fixture.json'
    if args.phase=='low' and not fixture_path.exists():
        output.mkdir(parents=True,exist_ok=False)
        (output/'workspace').mkdir();(output/'state').mkdir()
        fixture={'version':1,'models':models,'thinking':args.thinking,'kernel_sha256':shared.kernel_hash(),
                 'completed':[],'attempts':[],'session':None,'parent':None,
                 'axis':chat_axis(output/'workspace','all-native-tier-continuity')}
        shared.write_json(fixture_path,fixture)
    else:
        fixture=json.loads(fixture_path.read_text())
        assert fixture['models']==models and fixture['thinking']==args.thinking
        assert fixture['kernel_sha256']==shared.kernel_hash()
    os.environ.update(SUTRA_STRATA_ONLY='1',SUTRA_STRATA_MODELS=json.dumps(list(models.values())),
                      SUTRA_THINKING=args.thinking,SUTRA_MEMORY_OWNERSHIP='shared',
                      SUTRA_RUNTIME_DIR=str(output/'runtime-memory'))
    ledger=InvocationLedger(output/'state/state.sqlite')
    report={'passed':False,'phase':args.phase,'models':models,'kernel_sha256':fixture['kernel_sha256'],
            'user_settings_changed':False,'runtime_actions_from_this_script':[],
            'activation_owner':'StrataModel.dispatch_gate through the native coordinator',
            'scope':'Native profile switching and continuity; not a model-quality benchmark'}
    try:
        before=shared.check_saved(ledger,fixture) if fixture['session'] else None
        if args.phase=='verify':
            assert fixture['completed']==list(PHASES)
            shared.check_no_actions(ledger)
            report.update(passed=True,checkpoint=before,attempts=fixture['attempts'],checks={
                'canonical_kernel_unchanged':True,'saved_contract_permissions_spending':True,
                'receipt_chains':True,'native_low_mid_high_recall':True,
                'fresh_process_per_phase':True,'ollama_rpc_forbidden':True,
                'native_activation_owned_by_provider':True})
            return
        assert fixture['completed']==list(PHASES[:PHASES.index(args.phase)]),'Run low, mid, high in order'
        request={'prompt':PROMPTS[args.phase],'models':models,'tier':args.phase,
                 'parent':fixture['parent'],'one_step':True}
        if not fixture['session']:
            fixture['session']=session_for(ledger,request,axis=fixture['axis'])
            fixture['checkpoint']=shared.checkpoint(ledger,fixture['session'])
            shared.write_json(fixture_path,fixture)
        request['conversation_session']=fixture['session']
        with patch('vscode_bridge.ollama',side_effect=AssertionError('Ollama not allowed')) as ollama, \
             patch('runtime_memory.api',side_effect=AssertionError('Ollama not allowed')) as memory_api:
            catalog_result=append_catalog(catalog(ollama))
            eligible={row['name'] for row in catalog_result['models'] if row.get('eligible')}
            assert set(models.values())<=eligible,catalog_result
            for tier in PHASES:
                assert choose('Hello',models,tier,{'source_files':0,'selected_files':0,'unresolved_calls':0})['model']==models[tier]
            metadata=profile_metadata(models[args.phase])
            started=time.monotonic()
            with shared.telemetry(output/'state'),deadline(450):
                result=answer(ledger,{'workspace':str(output/'workspace')},output/'state',
                              request,axis=fixture['axis'])
            elapsed=round(time.monotonic()-started,3)
            ollama.assert_not_called();memory_api.assert_not_called()
        attempt={'phase':args.phase,'seconds':elapsed,'profile':metadata,'response':result,
                 'ollama_rpc_calls':ollama.call_count+memory_api.call_count}
        fixture['attempts'].append(attempt);report['attempt']=attempt
        assert result['session']==fixture['session']
        receipt=result['result'];assert receipt['admission']['admitted'],receipt['admission']
        assert result['route']['model']==models[args.phase]
        envelope=json.loads(ledger.db.execute('SELECT record_json FROM invocations WHERE id=?',
                                              (receipt['invocation_id'],)).fetchone()[0])
        identity=envelope['model_identity'];attempt['model_identity']=identity
        assert identity['native_profile_ref']==models[args.phase]
        assert identity['native_profile_revision']==metadata['profile_revision']
        assert identity['native_source_sha256']==metadata['source_sha256']
        assert result['model_capacity']['effective_context_tokens']<=metadata['context_tokens']
        current=shared.checkpoint(ledger,fixture['session'])
        constraints=current['snapshot']['constraints']
        assert all(constraints[key] is False for key in ('network_allowed','edits_allowed','tests_allowed','dependencies_allowed'))
        assert constraints['conversation_only'] is True
        if before:
            assert current['origin']==before['origin'] and current['snapshot']==before['snapshot']
            for key in ('limits','base_limits','allocation_version'):
                assert current['budget'][key]==before['budget'][key]
            previous_used,used=before['budget']['used'],current['budget']['used']
            assert all(used[key]>=previous_used[key] for key in previous_used)
            assert used['calls']>previous_used['calls'] and used['tokens']>previous_used['tokens']
            assert current['invocations'][:len(before['invocations'])]==before['invocations']
            assert current['receipt_fingerprints'][:len(before['receipt_fingerprints'])]==before['receipt_fingerprints']
        text=receipt['accepted_output']['answer']
        if fixture['parent']:
            assert result['memory_used']
            shared.check_recall(text)
            raw=ledger.db.execute('SELECT d.payload_json FROM deliveries d JOIN invocations i ON i.delivery_id=d.id WHERE i.id=?',
                                  (receipt['invocation_id'],)).fetchone()[0]
            pins=json.dumps(json.loads(raw)['memory']['pinned_user_statements'])
            assert all(value in pins for value in ('Cedar-724','CED724','STP kernel','No network'))
        assert ledger.verify_invocations(fixture['session']) and ledger.audit_valid()
        shared.check_no_actions(ledger)
        fixture['parent']=receipt['invocation_id'];fixture['completed'].append(args.phase)
        fixture['checkpoint']=current
        report.update(passed=True,answer=text,checkpoint=current,
                      task_contract_hash=sha(canonical(current['snapshot'])))
    except BaseException as error:
        report['error']=str(error)
        if fixture.get('session'):fixture['checkpoint']=shared.checkpoint(ledger,fixture['session'])
        raise
    finally:
        shared.write_json(fixture_path,fixture)
        shared.write_json(output/('phase-'+args.phase+'.json'),report)
        shared.write_json(output/'results.json',report)
        ledger.close()
        print(json.dumps({key:report.get(key) for key in ('phase','passed','error')}),flush=True)


if __name__=='__main__':main()

"""Opt-in real gateway continuity across Sutra-owned engine profiles.

Run each phase in a separate Python process, in this order:
low, mid, restart, high, verify. The restart phase shuts down only this
fixture's authenticated Sutra worker while holding the shared inference slot,
then calls High through the invocation gateway. No Ollama or Strata Control
operation is permitted. All phases use the same task and consumed budgets.

Set SUTRA_ENGINE_PYTHON to the absolute Python executable in Sutra's own engine
environment. --output must be a new absolute directory for the first low run.
No models or dependencies are downloaded by this script.
"""
import argparse
from contextlib import ExitStack, contextmanager
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'harness'))

from context_manager import canonical, sha
from conversation import answer, chat_axis, session_for
import engine_client
import engine_profiles
from invocation_gateway import InvocationLedger
from lifecycle import deadline
from model_catalog import combined
from review_budget import usage
from runtime_memory import lease


_spec = importlib.util.spec_from_file_location('sutra_live_native_helpers',
                                             ROOT / 'test/live-strata-management.py')
_helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helpers)
write_json, kernel_hash = _helpers.write_json, _helpers.kernel_hash
checkpoint, check_saved = _helpers.checkpoint, _helpers.check_saved
check_no_actions, check_recall = _helpers.check_no_actions, _helpers.check_recall
telemetry = _helpers.telemetry

PHASES = ('low', 'mid', 'restart', 'high')
MODELS = {'low': 'sutra:engine/engine-low16k',
          'mid': 'sutra:engine/engine-mid24k',
          'high': 'sutra:engine/engine-high32k'}
PROMPTS = {
    'low': (
        'Remember project Cedar-724, ticket CED724. The target is 18 and the actual is 21. '
        'Never change the STP kernel. No network, edits, tests, or new dependencies. '
        'Keep this task conversation only. Reply with one sentence confirming the facts.'
    ),
    'mid': (
        'Using our previous turn, name the project and ticket, calculate actual minus target, '
        'and repeat what must stay unchanged and which operations are prohibited. '
        'Answer in one short paragraph without performing any actions.'
    ),
    'restart': (
        'After this runtime restart, recall our saved project and ticket, the difference '
        'between actual and target, and all work restrictions. '
        'Answer in one short paragraph without performing any actions.'
    ),
    'high': (
        'In this fresh caller process, recall the same saved project and ticket, '
        'the difference between actual and target, and all work restrictions. '
        'Give one short paragraph without performing actions.'
    ),
}


def absolute(value, label):
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(label + ' must be an absolute path')
    return path


def model_choices(raw):
    models = MODELS if raw is None else json.loads(raw)
    if (not isinstance(models, dict) or set(models) != {'low', 'mid', 'high'} or
            any(not engine_profiles.is_engine(value) for value in models.values())):
        raise ValueError('--models must be a JSON object with three Sutra engine references')
    return dict(models)


def owned_profiles(models):
    """Inspect descriptors offline; fail before any worker can be started."""
    profiles = {tier: engine_profiles.load(ref) for tier, ref in models.items()}
    for profile in profiles.values():
        paths = [profile['engine']['executable'], profile['engine']['cwd'], profile['tokenizer_path']]
        paths.extend(arg for arg in profile['engine']['args'] if Path(arg).is_absolute())
        paths.extend(binding['path'] for binding in profile['artifact_bindings'])
        for value in paths:
            lowered = str(value).lower().replace('\\', '/')
            if '/strata/' in lowered or '/control/' in lowered:
                raise ValueError('The Sutra-owned profile still depends on a Strata/Control asset: ' + str(value))
    return profiles


@contextmanager
def forbid_foreign_runtimes():
    with ExitStack() as stack:
        for target in ('vscode_bridge.ollama', 'runtime_memory.api',
                       'strata_model.native_command', 'strata_model.status'):
            stack.enter_context(patch(target, side_effect=AssertionError(
                'Foreign runtime access is forbidden in this fixture: ' + target)))
        yield


def runtime_snapshot():
    state = engine_client.status()
    try:
        raw = engine_client.metadata(engine_client.directory())
    except (OSError, ValueError, engine_client.EngineServiceError):
        record = None
    else:
        # Never persist the worker's authentication token in an experiment report.
        record = {key: raw.get(key) for key in ('pid', 'runtime_hash', 'registry', 'python')}
    return {'status': state, 'service': record}


def stop_owned_worker(fixture, ledger, report):
    before = runtime_snapshot()
    assert before['service'] and before['status'].get('instance_id'), 'No saved worker to restart'
    assert before['status'].get('profile') == fixture['models']['mid'], before
    previous = fixture['attempts'][-1]['runtime_after']
    assert before['service'] == previous['service'], 'Worker identity changed before the restart phase'
    assert before['status']['owner_id'] == previous['status']['owner_id']
    assert before['status']['instance_id'] == previous['status']['instance_id']
    saved = checkpoint(ledger, fixture['session'])
    with lease():
        engine_client.shutdown()
        record_path = engine_client.directory() / 'worker.json'
        stop_at = time.monotonic() + 8
        while record_path.exists() and time.monotonic() < stop_at:
            time.sleep(.05)
        assert not record_path.exists(), 'Owned worker did not confirm shutdown; no replacement was started'
    after = runtime_snapshot()
    assert after['status'].get('running') is False, after
    assert checkpoint(ledger, fixture['session']) == saved, 'Restart changed durable memory or consumed budget'
    report['runtime_restart'] = {'before': before, 'after_shutdown': after,
                                 'ledger_unchanged_during_shutdown': True}


def preserve(previous, current):
    for key in ('origin', 'snapshot'):
        assert current[key] == previous[key], 'Task or permission changed: ' + key
    for key in ('limits', 'base_limits', 'allocation_version'):
        assert current['budget'][key] == previous['budget'][key], 'Task allocation changed: ' + key
    old, new = previous['budget']['used'], current['budget']['used']
    assert all(new[key] >= old[key] for key in old), 'A consumed budget was reset or refunded'
    assert new['calls'] > old['calls'] and new['tokens'] > old['tokens'], 'Native usage was not charged'
    for key in ('invocations', 'receipt_fingerprints'):
        assert current[key][:len(previous[key])] == previous[key], 'Existing evidence was rewritten: ' + key


def phase(args):
    output = absolute(args.output, '--output')
    models = model_choices(args.models)
    python = os.environ.get('SUTRA_ENGINE_PYTHON')
    if not python or not absolute(python, 'SUTRA_ENGINE_PYTHON').is_file():
        raise ValueError('SUTRA_ENGINE_PYTHON must name Sutra\'s installed engine Python')
    if args.registry:
        os.environ['SUTRA_ENGINE_REGISTRY'] = str(absolute(args.registry, '--registry'))
    runtime_dir = absolute(args.runtime_dir, '--runtime-dir') if args.runtime_dir else output / 'runtime-memory'
    os.environ.update(SUTRA_THINKING='off', SUTRA_ENGINE_MODELS=json.dumps(list(models.values())),
                      SUTRA_ENGINE_ONLY='1', SUTRA_STRATA_MODELS='[]',
                      SUTRA_MEMORY_OWNERSHIP='shared', SUTRA_RUNTIME_DIR=str(runtime_dir))
    profiles = owned_profiles(models)
    profile_hashes = {tier: profile['profile_hash'] for tier, profile in profiles.items()}
    fixture_path = output / 'fixture.json'
    if args.phase == 'low' and not fixture_path.exists():
        output.mkdir(parents=True, exist_ok=False)
        (output / 'workspace').mkdir()
        (output / 'state').mkdir()
        fixture = {'version': 1, 'models': models, 'profile_hashes': profile_hashes,
                   'kernel_sha256': kernel_hash(), 'completed': [], 'attempts': [],
                   'session': None, 'parent': None, 'python': python,
                   'registry': str(engine_profiles.registry_path()), 'runtime_dir': str(runtime_dir),
                   'axis': chat_axis(output / 'workspace', 'sutra-owned-engine-continuity')}
        write_json(fixture_path, fixture)
    else:
        fixture = json.loads(fixture_path.read_text())
        assert fixture['kernel_sha256'] == kernel_hash(), 'Canonical kernel changed between phases'
        assert fixture['models'] == models and fixture['profile_hashes'] == profile_hashes
        assert fixture['python'] == python and fixture['runtime_dir'] == str(runtime_dir)
        assert fixture['registry'] == str(engine_profiles.registry_path())
    storage = output / 'state'
    ledger = InvocationLedger(storage / 'state.sqlite')
    report = {'passed': False, 'phase': args.phase, 'models': models, 'caller_pid': os.getpid(),
              'scope': 'Real invocation gateway; one fresh caller process per phase; native runtime counters',
              'kernel_sha256': fixture['kernel_sha256'], 'profile_hashes': profile_hashes,
              'foreign_runtime_calls_allowed': False, 'workspace_actions_from_this_script': []}
    attempt = None
    try:
        before = check_saved(ledger, fixture) if fixture['session'] else None
        if args.phase == 'verify':
            assert fixture['completed'] == list(PHASES), fixture['completed']
            attempts = [item for item in fixture['attempts'] if item.get('passed')]
            assert len(attempts) == len(PHASES)
            assert len({item['caller_pid'] for item in attempts}) == len(PHASES), 'Reuse the task, not the caller process'
            check_no_actions(ledger)
            report.update(passed=True, checkpoint=before, attempts=fixture['attempts'], checks={
                'durable_context_constraints_and_permissions': True, 'consumed_budgets_preserved': True,
                'receipt_chains': True, 'canonical_kernel_unchanged': True, 'low_mid_high_recall': True,
                'owned_worker_restart_and_recovery': True, 'fresh_process_per_phase': True,
                'no_ollama_or_strata_control': True, 'profile_and_instance_bindings': True})
            return
        index = PHASES.index(args.phase)
        assert fixture['completed'] == list(PHASES[:index]), 'Run phases in the documented order'
        tier = args.phase if args.phase in ('low', 'mid') else 'high'
        request = {'prompt': PROMPTS[args.phase], 'models': models, 'tier': tier,
                   'parent': fixture['parent'], 'one_step': True}
        if not fixture['session']:
            fixture['session'] = session_for(ledger, request, axis=fixture['axis'])
            fixture['checkpoint'] = checkpoint(ledger, fixture['session'])
            write_json(fixture_path, fixture)
        request['conversation_session'] = fixture['session']
        with forbid_foreign_runtimes():
            catalog = combined(lambda *args: (_ for _ in ()).throw(AssertionError('Ollama is forbidden')))
            available = {row['name'] for row in catalog['models'] if row.get('eligible')}
            assert set(models.values()) <= available, catalog
            report['catalog'] = catalog
            if args.phase == 'restart':
                stop_owned_worker(fixture, ledger, report)
            attempt = {'phase': args.phase, 'tier': tier, 'caller_pid': os.getpid(),
                       'runtime_before': runtime_snapshot(), 'passed': False}
            fixture['attempts'].append(attempt)
            started = time.monotonic()
            with telemetry(storage), deadline(args.seconds):
                result = answer(ledger, {'workspace': str(output / 'workspace')}, storage,
                                request, axis=fixture['axis'])
            attempt.update(seconds=round(time.monotonic() - started, 3), response=result,
                           runtime_after=runtime_snapshot())
        report['attempt'] = attempt
        assert result['session'] == fixture['session'], 'Model switch created another task'
        receipt = result['result']
        assert receipt['admission']['admitted'], receipt['admission']
        assert result['route']['model'] == models[tier], result['route']
        envelope = json.loads(ledger.db.execute('SELECT record_json FROM invocations WHERE id=?',
                                               (receipt['invocation_id'],)).fetchone()[0])
        assert envelope['model'] == models[tier]
        identity = envelope.get('model_identity') or {}
        attempt['model_identity'] = identity
        assert profile_hashes[tier] in json.dumps(identity), 'Invocation lost its immutable profile identity'
        text = receipt['accepted_output']['answer']
        current = checkpoint(ledger, fixture['session'])
        permissions = current['snapshot']['constraints']
        assert all(permissions[name] is False for name in (
            'network_allowed', 'edits_allowed', 'tests_allowed', 'dependencies_allowed'))
        assert permissions['conversation_only'] is True
        if before:
            preserve(before, current)
        if fixture['parent']:
            assert result['memory_used'], 'Saved memory was not supplied after loading or switching'
            raw = ledger.db.execute('SELECT d.payload_json FROM deliveries d JOIN invocations i '
                                    'ON i.delivery_id=d.id WHERE i.id=?', (receipt['invocation_id'],)).fetchone()[0]
            pins = json.dumps(json.loads(raw)['memory']['pinned_user_statements'])
            assert all(value in pins for value in ('Cedar-724', 'CED724', 'STP kernel', 'No network'))
            check_recall(text)
        observed = attempt['runtime_after']
        state = observed['status']
        assert state.get('profile') == models[tier] and state.get('profile_hash') == profile_hashes[tier]
        assert state.get('context_tokens') == profiles[tier]['context_tokens']
        assert state.get('instance_id') and type(state.get('pid')) is int and state['pid'] > 0
        assert observed['service'] and observed['service']['python'] == python
        if args.phase == 'mid':
            previous = next(item for item in fixture['attempts'] if item.get('passed') and item['phase'] == 'low')
            assert state['owner_id'] == previous['runtime_after']['status']['owner_id'], 'Profile switching restarted the service'
            assert state['instance_id'] != previous['runtime_after']['status']['instance_id'], 'Profile switching reused another model instance'
        elif args.phase == 'restart':
            old = report['runtime_restart']['before']
            assert state['owner_id'] != old['status']['owner_id'], 'The service did not restart'
            assert state['instance_id'] != old['status']['instance_id'], 'Restart reused a stale instance'
        elif args.phase == 'high':
            previous = next(item for item in fixture['attempts'] if item.get('passed') and item['phase'] == 'restart')
            assert state['owner_id'] == previous['runtime_after']['status']['owner_id']
            assert state['instance_id'] == previous['runtime_after']['status']['instance_id'], 'Warm High was needlessly reloaded'
        token_calls = receipt['token_usage']['calls']
        assert token_calls and all(call['source'] == 'sutra_engine_response' for call in token_calls)
        assert all(type(call['input_tokens']) is int and type(call['output_tokens']) is int for call in token_calls)
        attempt['native_usage'] = {'gateway': receipt['token_usage'], 'last_generation': state['last_generation'],
                                   'scope': 'Actual tokenized prompt and native DONE output counters; no per-component attribution'}
        assert ledger.verify_invocations(fixture['session']) and ledger.audit_valid()
        check_no_actions(ledger)
        attempt['passed'] = True
        fixture['parent'] = receipt['invocation_id']
        fixture['completed'].append(args.phase)
        fixture['checkpoint'] = current
        report.update(passed=True, answer=text, checkpoint=current,
                      task_contract_hash=sha(canonical(current['snapshot'])))
    except BaseException as error:
        report['error'] = str(error)
        if attempt is not None:
            attempt['error'] = str(error)
        if fixture.get('session'):
            # Withheld or failed calls remain in the ledger and remain charged.
            fixture['checkpoint'] = checkpoint(ledger, fixture['session'])
        raise
    finally:
        write_json(fixture_path, fixture)
        write_json(output / ('phase-' + args.phase + '.json'), report)
        write_json(output / 'results.json', report)
        ledger.close()
        print(json.dumps({key: report.get(key) for key in ('phase', 'passed', 'error')}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--phase', choices=(*PHASES, 'verify'), required=True)
    parser.add_argument('--models', help='JSON object with low, mid and high Sutra engine references')
    parser.add_argument('--registry', help='Optional absolute trusted engine registry')
    parser.add_argument('--runtime-dir', help='Optional absolute shared inference directory')
    parser.add_argument('--seconds', type=int, default=900)
    args = parser.parse_args()
    if not 30 <= args.seconds <= 7200:
        parser.error('--seconds must be 30..7200')
    phase(args)


if __name__ == '__main__':
    main()

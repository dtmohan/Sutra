"""Incremental real-provider architecture-stage experiment in a private ledger.

prepare only freezes file hashes and creates a trial directory; it never calls a
model. Each step executes one normal bridge action and saves an atomic cursor.
There is no provider injection, resource bypass, automatic grant or forced
report. This is an API experiment, not a VS Code chat acceptance test.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import threading
import time


FORMAT = 'discovery-convergence-experiment-v1'
LIMITS = {'review_calls': 12, 'review_tokens': 250000, 'max_tools': 256,
          'review_source_kib': 65536, 'review_seconds': 1800, 'review_page_kib': 512}
SOFT_DISCOVERY_CALLS = 8
MAX_STEPS = 48
MAX_FILES = 10000
MAX_TREE_BYTES = 256 * 1024**2
MAX_JSON_BYTES = 2 * 1024**2
MODELS = {'low': 'qwen3:4b', 'mid': 'local-assistant:latest', 'high': 'strata:high'}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def sha(value):
    return hashlib.sha256(value).hexdigest()


def read_json(path):
    raw = Path(path).read_bytes()
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError('Experiment JSON exceeds its bound')
    return json.loads(raw)


def save_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name+'.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(json.dumps(value, indent=2, sort_keys=True)+'\n')
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def tree_digest(root):
    """Bind both trials to actual files; never follow links or hash model weights."""
    root = Path(root).resolve(strict=True)
    rows = []
    total = 0
    for folder, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(name for name in directories if name not in
            ('.git', '__pycache__', '.pytest_cache', 'node_modules'))
        for name in directories:
            if (Path(folder)/name).is_symlink():
                raise ValueError('Frozen tree contains a linked directory')
        for name in sorted(files):
            path = Path(folder)/name
            info = path.lstat()
            if path.is_symlink() or not path.is_file() or info.st_nlink != 1:
                raise ValueError('Frozen tree requires regular unlinked files')
            total += info.st_size
            if len(rows) >= MAX_FILES or total > MAX_TREE_BYTES:
                raise ValueError('Frozen tree exceeds experiment hashing bounds')
            rows.append((path.relative_to(root).as_posix(), sha(path.read_bytes())))
    return {'sha256': sha(canonical(rows).encode()), 'files': len(rows), 'bytes': total}


def environment(trial):
    # Preferences in the signed-in VS Code instance are neither read nor edited.
    result = {key: value for key, value in os.environ.items()
              if not key.startswith(('SUTRA_', 'KARMA_'))}
    result.update(PYTHONDONTWRITEBYTECODE='1', SUTRA_THINKING='adaptive',
        SUTRA_ACTIVITY='0', SUTRA_STRATA_PORT=str(trial['port']),
        SUTRA_STRATA_MODEL=trial['server_model'], SUTRA_STRATA_MODELS='["strata:high"]',
        SUTRA_MEMORY_OWNERSHIP='shared',
        SUTRA_RUNTIME_DIR=str(Path(trial['directory'])/'runtime-memory'),
        SUTRA_WORKSPACE_ALLOCATION='{"enabled":false}', KARMA_STORAGE_LIMIT_MIB='512',
        KARMA_NODE=shutil.which('node') or '',
        KARMA_TYPESCRIPT=str(Path(trial['runtime'])/'vendor/typescript/typescript.js'))
    # Deliberately do not set SUTRA_STRATA_ONLY: legacy High uses the existing
    # endpoint and must not activate/unload a separately managed runtime.
    return result


def project(config_path, output):
    """Select the complete shipped product text, without editing its runtime."""
    config = read_json(config_path)
    source = Path(config['workspace']).resolve(strict=True)
    parent = Path(config['directory']).resolve(strict=True)
    workspace = parent/'product-source-bc74250'
    directory = parent/'product-trials'
    if workspace.exists() or directory.exists() or Path(output).exists():
        raise ValueError('Product projection outputs already exist; refusing replacement')
    spec = importlib.util.spec_from_file_location('frozen_runtime_package', source/'package.py')
    package = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(package)
    supported = {'.py', '.js', '.json', '.html', '.css', '.md', '.txt'}
    excluded = {'vendor', 'node_modules', 'dist', 'build', '__pycache__', 'venv', 'target', 'coverage'}
    paths = {path.relative_to(source) for path in package.runtime_files(source)
             if path.suffix in supported and not (set(path.relative_to(source).parts) & excluded)
             and path.name != 'THIRD_PARTY_NOTICES.md'} | {Path('README.md')}
    rows = []
    for relative in sorted(paths):
        path = source/relative
        info = path.lstat()
        if path.is_symlink() or not path.is_file() or info.st_nlink != 1:
            raise ValueError('Product projection requires exact regular source files')
        rows.append({'path': relative.as_posix(), 'bytes': info.st_size,
                     'sha256': sha(path.read_bytes())})
    workspace.mkdir(mode=0o700)
    directory.mkdir(mode=0o700)
    for relative in sorted(paths):
        target = workspace/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source/relative, target)
    scope = {'profile': 'shipped-product-text-projection-v1', 'commit': config['commit'],
             'selection': 'all supported text from baseline package.runtime_files minus vendor/exclusions/notices, plus README.md',
             'scope_sha256': sha(canonical(rows).encode()), 'files': rows,
             'scope_limits': 'Product runtime architecture only; tests/vendor/binary implementation are not ingested.'}
    save_json(parent/'product-source-scope.json', scope)
    config.update(directory=str(directory), workspace=str(workspace),
                  source_scope={key: value for key, value in scope.items() if key != 'files'})
    save_json(output, config)
    print(canonical({'kind': 'product_projection', 'files': len(rows),
                     'scope_sha256': scope['scope_sha256'], 'workspace': str(workspace),
                     'config': str(output)}), flush=True)


def experiment_limits(config):
    supplied = config.get('limits', {})
    if (not isinstance(supplied, dict) or set(supplied)-set(LIMITS)
            or any(type(value) is not int or not 0 < value <= LIMITS[key]
                   for key, value in supplied.items())):
        raise ValueError('Experiment overrides can only narrow the explicit approved ceilings')
    return dict(LIMITS, **supplied)


def prepare(config_path, label, runtime=None):
    config = read_json(config_path)
    if label not in ('baseline', 'candidate'):
        raise ValueError('Choose baseline or candidate')
    runtime = Path(runtime or config.get(label+'_runtime', '')).resolve(strict=True)
    workspace = Path(config['workspace']).resolve(strict=True)
    directory = Path(config['directory']).resolve(strict=True)/label
    if directory == workspace or workspace in directory.parents:
        raise ValueError('Experiment state cannot be inside the frozen workspace')
    if not (runtime/'harness/vscode_bridge.py').is_file():
        raise ValueError('Choose a frozen complete Sutra runtime')
    if not isinstance(config.get('prompt'), str) or not 1 <= len(config['prompt']) <= 1800:
        raise ValueError('Choose one bounded exact architecture prompt')
    runtime_hash, source_hash = tree_digest(runtime), tree_digest(workspace)
    canonical_kernel = read_json(runtime/'harness/canonical-kernel.json')
    trial = {'format': FORMAT, 'label': label, 'directory': str(directory),
        'runtime': str(runtime), 'workspace': str(workspace), 'commit': config['commit'],
        'prompt': config['prompt'], 'prompt_sha256': sha(config['prompt'].encode()),
        'runtime_tree': runtime_hash, 'source_tree': source_hash,
        'source_scope': config.get('source_scope', {'selection': 'full archived checkout'}),
        'canonical_kernel': canonical_kernel, 'limits': experiment_limits(config),
        'model': 'strata:high', 'server_model': 'qwen3.8-flash-next-q2_0', 'port': 19483,
        'models': dict(MODELS),
        'thinking': 'adaptive', 'bridge_seconds': 450,
        'soft_discovery_calls': SOFT_DISCOVERY_CALLS, 'max_steps': MAX_STEPS,
        'chat_key': sha((str(directory)+':isolated-architecture-stage').encode()),
        'method_limits': ['Architecture-stage host review; initial conversational classification skipped.',
            'Real provider/Gateway/repository tools; this is not a VS Code chat trial.',
            'High selector/writer; normal distinct-model reviewer policy and configured Low/Mid are retained.',
            'One bridge action may include bounded repairs; 12 total model calls is the ledger ceiling.',
            'The 8-discovery-call observation boundary is checked only between bridge actions.',
            'An experiment stop preserves its incomplete state; it does not manufacture a report.',
            'No automatic allocation amendments, provider mocks or resource-gate bypasses.']}
    if config.get('experiment_profile'):
        trial['experiment_profile'] = config['experiment_profile']
    directory.mkdir(mode=0o700)
    (directory/'store').mkdir(mode=0o700)
    save_json(directory/'trial.json', trial)
    save_json(directory/'checkpoint.json', {'format': FORMAT, 'state': 'PREPARED', 'steps': 0,
                                            'flow_id': None, 'complete': False})
    print(canonical({'kind': 'prepared', 'trial': str(directory), 'runtime_tree': runtime_hash,
                     'source_tree': source_hash, 'limits': trial['limits']}), flush=True)


def sqlite_summary(directory):
    path = Path(directory)/'store/state.sqlite'
    if not path.is_file():
        return {'model_calls': 0, 'discovery_calls': 0, 'pending_calls': 0, 'settled_units': 0}
    db = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=2)
    db.execute('PRAGMA query_only=ON')
    try:
        rows = db.execute('''SELECT json_extract(i.record_json,'$.role'),
            json_extract(i.record_json,'$.model'),json_extract(i.record_json,'$.charge'),
            json_extract(i.record_json,'$.budget_profile.response_reserve'),
            json_extract(r.record_json,'$.status'),
            json_extract(r.record_json,'$.accounting_settlement.used_tokens'),
            json_extract(r.record_json,'$.elapsed_seconds')
            FROM invocations i LEFT JOIN invocation_receipts r ON r.invocation_id=i.id''').fetchall()
        calls = [{'role': role, 'model': model, 'reserved_units': charge+reserve,
                  'status': status or 'pending', 'settled_units': (used if used is not None else charge+reserve)
                  if status else 0, 'elapsed_seconds': elapsed}
                 for role, model, charge, reserve, status, used, elapsed in rows]
        return {'model_calls': len(calls), 'discovery_calls': sum(c['role'] == 'architect' for c in calls),
                'pending_calls': sum(c['status'] == 'pending' for c in calls),
                'settled_units': sum(c['settled_units'] for c in calls), 'calls': calls}
    except sqlite3.OperationalError:
        return {'model_calls': 0, 'discovery_calls': 0, 'pending_calls': 0, 'settled_units': 0}
    finally:
        db.close()


class Telemetry:
    def __init__(self, runtime, store):
        self.runtime, self.store = Path(runtime), Path(store)
        self.process = self.thread = None
        self.error = None

    def __enter__(self):
        if sys.platform == 'darwin':
            command = [str(self.runtime/'bin/telemetry')]
        else:
            command = [shutil.which('node') or 'node', str(self.runtime/'harness/telemetry_portable.js')]
        self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        text=True, env=dict(os.environ, ELECTRON_RUN_AS_NODE='1'))
        def collect():
            try:
                for line in self.process.stdout:
                    if len(line) > 65536:
                        self.error = 'Telemetry frame exceeded bound'
                        break
                    value = json.loads(line)
                    save_json(self.store/'telemetry.json', value)
            except (OSError, ValueError) as error:
                self.error = type(error).__name__
        self.thread = threading.Thread(target=collect, daemon=True)
        self.thread.start()
        until = time.monotonic()+3
        while not (self.store/'telemetry.json').is_file() and time.monotonic() < until:
            time.sleep(.05)
        return self

    def __exit__(self, *_):
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
            self.thread.join(timeout=1)
            self.process.stdout.close()


def compact_response(response):
    return {key: response[key] for key in ('flow_id', 'state', 'cursor', 'binding', 'bound_task',
            'stop', 'progress', 'recovery') if key in response}


def architecture_start_request(trial, binding):
    # The non-chat host entry retains the explicitly pre-created task. A new
    # chat_key intentionally creates fresh chat metadata and would lose that
    # task before start_review validates its binding. Ledgers isolate trials.
    return {'op': 'orchestrate', 'event': 'start_review', 'prompt': trial['prompt'],
            'models': dict(trial.get('models', MODELS)),
            'tier': 'high', 'armed': True, 'binding': binding}


def discovery_terminal(db, session):
    """Observe the durable stage checkpoint if writer dispatch needs a new slice.

    The next normal runtime step still verifies this record and its history;
    this only prevents the external experiment's soft boundary hiding a saved
    stage completion. It never creates a checkpoint or grants another call.
    """
    row = db.execute('''SELECT json_extract(d.payload_json,'$.discovery_key')
        FROM invocations i JOIN deliveries d ON d.id=i.delivery_id
        WHERE i.session=? AND json_extract(i.record_json,'$.role')='architect'
        ORDER BY i.rowid DESC LIMIT 1''', (session,)).fetchone()
    key = row[0] if row else None
    if not key:
        return None
    row = db.execute('''SELECT json_extract(payload_json,'$.checkpoint.stop_reason')
        FROM page_audit WHERE event='PARENT_DISCOVERY_CHECKPOINT'
        AND json_extract(payload_json,'$.session')=?
        AND json_extract(payload_json,'$.checkpoint.key')=?
        AND json_extract(payload_json,'$.checkpoint.complete')=1
        ORDER BY seq DESC LIMIT 1''', (session, key)).fetchone()
    return {'complete': True, 'reason': row[0]} if row else None


def worker(directory, resume=False, cancel=False):
    """A fresh interpreter loads only the selected frozen runtime."""
    directory = Path(directory).resolve(strict=True)
    trial = read_json(directory/'trial.json')
    checkpoint = read_json(directory/'checkpoint.json')
    runtime, workspace, store = Path(trial['runtime']), Path(trial['workspace']), directory/'store'
    sys.path.insert(0, str(runtime/'harness'))
    selected_environment = environment(trial)
    os.environ.clear()
    os.environ.update(selected_environment)
    from vscode_bridge import handle
    from invocation_gateway import InvocationLedger
    import orchestration_flow as flow
    from lifecycle import deadline
    from monitoring import require_resources
    from review_budget import usage
    models = dict(trial.get('models', MODELS))
    response = None
    started = time.monotonic()
    error = None
    print(canonical({'kind': 'worker_started', 'label': trial['label'], 'state': checkpoint['state']}), flush=True)
    try:
        with Telemetry(runtime, store) as telemetry, deadline(trial['bridge_seconds']):
            if not cancel:
                require_resources(store)
            setup_recovery = (checkpoint.get('state') == 'CLASSIFYING'
                and (checkpoint.get('error') or {}).get('message') ==
                    'Explicit review requires a user-created read-only task'
                and sqlite_summary(directory)['model_calls'] == 0)
            if cancel:
                response = handle(store, workspace, {'op': 'orchestrate', 'event': 'cancel',
                    'flow_id': checkpoint['flow_id'], 'cursor': checkpoint['cursor']})
            elif setup_recovery:
                handle(store, workspace, {'op': 'orchestrate', 'event': 'cancel',
                    'flow_id': checkpoint['flow_id'], 'cursor': checkpoint['cursor'],
                    'chat_key': trial['chat_key']})
                checkpoint['setup_recovery'] = 'Empty experiment setup flow cancelled through host API; task/cost retained'
            if cancel:
                pass
            elif not checkpoint.get('flow_id') or setup_recovery:
                if not setup_recovery:
                    handle(store, workspace, {'op': 'auto_new', 'goal': trial['prompt'], 'roots': ['.'],
                                              **trial['limits']})
                s = InvocationLedger(store/'state.sqlite')
                try:
                    meta = read_json(store/'workspace.json')
                    binding = flow.identity(s, meta['active'])
                finally:
                    s.close()
                response = handle(store, workspace, architecture_start_request(trial, binding))
            else:
                request = {'op': 'orchestrate', 'event': 'resume' if resume else 'step',
                    'flow_id': checkpoint['flow_id'], 'cursor': checkpoint['cursor'],
                    'binding': checkpoint.get('binding') or checkpoint.get('bound_task'),
                    'models': models, 'tier': 'high', 'armed': True}
                response = handle(store, workspace, request)
    except BaseException as caught:
        error = {'type': type(caught).__name__, 'message': str(caught)[:1200]}
    finally:
        # Inspect the now-settled workflow even if a normal bridge deadline
        # interrupted the action. Never replace an accepted prefix or spending.
        db = store/'state.sqlite'
        if db.is_file():
            s = InvocationLedger(db)
            try:
                fid = (response or {}).get('flow_id') or checkpoint.get('flow_id')
                if not fid:
                    row = s.db.execute("SELECT id FROM orchestration_nodes WHERE "
                        "json_extract(record_json,'$.kind')='flow' ORDER BY rowid DESC LIMIT 1").fetchone()
                    fid = row[0] if row else None
                if fid:
                    inspected = flow.response(s, fid)
                    details = flow.info(s, fid)[1]
                    checkpoint.update(compact_response(inspected))
                    meta = flow.flow_metadata(store, workspace, fid)
                    sid = meta.get('active') or read_json(store/'workspace.json').get('active')
                    if sid:
                        checkpoint['binding'] = flow.identity(s, sid)
                        checkpoint['task_budget'] = usage(s, sid)
                    terminal = discovery_terminal(s.db, sid) if sid else None
                    checkpoint['discovery_complete'] = bool(terminal or (details.get('parent_evidence') or {}).get('complete'))
                    checkpoint['discovery_terminal'] = terminal
                    checkpoint['parent_checkpoint'] = details.get('parent_checkpoint')
                    checkpoint['parent_generation'] = details.get('parent_generation')
                    checkpoint['last_event'] = s.db.execute('SELECT json_extract(record_json,\'$.kind\') '
                        'FROM orchestration_events WHERE node_id=? ORDER BY seq DESC LIMIT 1', (fid,)).fetchone()[0]
            finally:
                s.close()
        checkpoint.update(steps=checkpoint['steps']+1, last_action_seconds=time.monotonic()-started,
                          error=error, complete=checkpoint.get('state') == 'DONE',
                          observed_at=time.time(), accounting=sqlite_summary(directory))
        save_json(directory/'checkpoint.json', checkpoint)
        save_json(directory/('checkpoint-'+str(checkpoint['steps']).zfill(3)+'.json'), checkpoint)
        if response is not None:
            save_json(directory/('response-'+str(checkpoint['steps']).zfill(3)+'.json'), response)
        print(canonical({'kind': 'checkpoint', 'label': trial['label'], 'state': checkpoint.get('state'),
            'step': checkpoint['steps'], 'last_event': checkpoint.get('last_event'),
            'elapsed_seconds': checkpoint['last_action_seconds'], 'error': error,
            'accounting': checkpoint['accounting'], 'stop': checkpoint.get('stop')}), flush=True)
    return 1 if error else 0


def next_action_allowed(checkpoint, accounting):
    if checkpoint.get('complete') or checkpoint.get('state') in ('DONE', 'CANCELLED', 'QUARANTINED'):
        return False, 'workflow_terminal'
    if checkpoint['steps'] >= MAX_STEPS:
        return False, 'experiment_step_ceiling'
    if accounting['pending_calls']:
        return False, 'pending_operation_requires_inspection'
    if accounting['model_calls'] >= LIMITS['review_calls']:
        return False, 'experiment_model_call_ceiling'
    if (accounting['discovery_calls'] >= SOFT_DISCOVERY_CALLS
            and not checkpoint.get('discovery_complete') and not checkpoint.get('parent_checkpoint')
            and not checkpoint.get('parent_generation')):
        return False, 'experiment_discovery_observation_boundary'
    return True, None


def step(directory, resume=False, cancel=False):
    directory = Path(directory).resolve(strict=True)
    trial = read_json(directory/'trial.json')
    checkpoint = read_json(directory/'checkpoint.json')
    if tree_digest(trial['runtime']) != trial['runtime_tree']:
        raise ValueError('Frozen runtime changed; no experiment step was dispatched')
    if tree_digest(trial['workspace']) != trial['source_tree']:
        raise ValueError('Frozen source changed; no experiment step was dispatched')
    accounting = sqlite_summary(directory)
    allowed, reason = next_action_allowed(checkpoint, accounting)
    if cancel and accounting['pending_calls']:
        raise ValueError('Wait for the active call to settle before cancelling this experiment')
    if not allowed and not cancel:
        print(canonical({'kind': 'experiment_stopped', 'reason': reason,
                         'state_retained': checkpoint['state'], 'directory': str(directory)}), flush=True)
        return 0
    if checkpoint.get('state') == 'PAUSED' and not resume and not cancel:
        print(canonical({'kind': 'inspection_needed', 'stop': checkpoint.get('stop')}), flush=True)
        return 0
    command = [sys.executable, '-B', str(Path(__file__).resolve()), '_worker',
               '--trial-dir', str(directory)] + (['--resume'] if resume else []) + (['--cancel'] if cancel else [])
    process = subprocess.Popen(command, env=environment(trial))
    started = last = time.monotonic()
    try:
        while process.poll() is None:
            now = time.monotonic()
            if now-last >= 20:
                print(canonical({'kind': 'heartbeat', 'label': trial['label'],
                    'stage_before_action': checkpoint['state'], 'elapsed_seconds': round(now-started, 1),
                    'note': 'Normal bridge/model deadlines remain in force; no inference was interrupted.'}), flush=True)
                last = now
            time.sleep(.2)
    except KeyboardInterrupt:
        # Forward an explicit operator cancellation into normal reconciliation.
        process.send_signal(__import__('signal').SIGINT)
        process.wait()
        raise
    return process.returncode


def report_trial(directory):
    """Consolidate source-free facts; never promote rejected work or judge it."""
    directory = Path(directory).resolve(strict=True)
    trial = read_json(directory/'trial.json')
    checkpoint = read_json(directory/'checkpoint.json')
    traces = []
    for path in sorted((directory/'store/calls').glob('*.json'))[:64]:
        record = read_json(path)
        traces.append({key: record[key] for key in ('call_id', 'ok', 'elapsed_s', 'request_hash',
            'generation_limits', 'structured_output', 'response') if key in record})
        traces[-1]['response'] = {key: value for key, value in (record.get('response') or {}).items()
            if key in ('done', 'done_reason', 'prompt_eval_count', 'eval_count',
                       'first_answer_chunk_s', 'timings')}
    errors = []
    path = directory/'store/state.sqlite'
    if path.is_file():
        db = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=2)
        db.execute('PRAGMA query_only=ON')
        try:
            for raw, in db.execute('SELECT record_json FROM invocation_receipts ORDER BY rowid'):
                receipt = json.loads(raw)
                if receipt.get('status') != 'quarantined':
                    continue
                proposal = receipt.get('proposal') or {}
                nested = (proposal.get('report') or {}).get('task_observation') if isinstance(proposal.get('report'), dict) else None
                errors.append({'seq': receipt['seq'], 'role': receipt['role'],
                    'errors': [str(item)[:240] for item in (receipt.get('admission') or {}).get('errors', [])[:3]],
                    'failure_kind': receipt.get('failure_kind'),
                    'misplaced_optional_observation': nested is not None,
                    'misplaced_observation_type': type(nested).__name__ if nested is not None else None})
        finally:
            db.close()
    response_path = directory/('response-'+str(checkpoint['steps']).zfill(3)+'.json')
    response = read_json(response_path) if response_path.is_file() else {}
    data = response.get('data') or {}
    result = data.get('result') or {}
    publication = data.get('report_review') or {}
    history = [read_json(path) for path in sorted(directory.glob('checkpoint-[0-9][0-9][0-9].json'))]
    return {'format': 'discovery-convergence-observation-v1',
        'kind': 'real-provider API architecture-stage experiment; not VS Code UX acceptance',
        'label': trial['label'], 'directory': str(directory), 'runtime_tree': trial['runtime_tree'],
        'source_tree': trial['source_tree'], 'source_scope': trial.get('source_scope'),
        'commit': trial['commit'], 'prompt_sha256': trial['prompt_sha256'], 'models': trial.get('models', MODELS),
        'thinking': trial['thinking'], 'limits': trial['limits'],
        'state': checkpoint['state'], 'discovery_complete': checkpoint.get('discovery_complete'),
        'discovery_terminal': checkpoint.get('discovery_terminal'), 'stop': checkpoint.get('stop'),
        'accounting': sqlite_summary(directory), 'redacted_transport_diagnostics': traces,
        'quarantined_responses': errors,
        'host_report_returned': bool((result.get('admission') or {}).get('admitted')
                                     and result.get('accepted_output')),
        'publication_status': publication.get('status'),
        'semantic_quality': 'not assessed by this script',
        'retained_action_timings': [{'step': item['steps'], 'seconds': item['last_action_seconds'],
                                    'state': item['state']} for item in history],
        'method_limits': trial['method_limits'] + [
            'Fresh interpreter per action; persistent-worker RAM-cache UX latency is not measured.',
            'Transport trace bodies are omitted; their timing hashes are diagnostic, not independent receipt authentication.',
            'Early controller versions retained only console action timings; available per-action journals are listed explicitly.',
            'A future draft or reviewer call can be smaller or larger than its measured planning packet.']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    projected = sub.add_parser('project')
    projected.add_argument('--config', type=Path, required=True)
    projected.add_argument('--output', type=Path, required=True)
    prepared = sub.add_parser('prepare')
    prepared.add_argument('--config', type=Path, required=True)
    prepared.add_argument('--trial', choices=('baseline', 'candidate'), required=True)
    prepared.add_argument('--runtime', type=Path)
    for action in ('step', 'cancel', 'inspect', 'report', '_worker'):
        command = sub.add_parser(action)
        command.add_argument('--trial-dir', type=Path, required=True)
        if action not in ('inspect', 'report'):
            command.add_argument('--resume', action='store_true')
        if action == 'report':
            command.add_argument('--output', type=Path)
        if action == '_worker':
            command.add_argument('--cancel', action='store_true')
    args = parser.parse_args(argv)
    if args.action == 'project':
        project(args.config, args.output)
        return 0
    if args.action == 'prepare':
        prepare(args.config, args.trial, args.runtime)
        return 0
    if args.action == 'inspect':
        print(json.dumps({'checkpoint': read_json(args.trial_dir/'checkpoint.json'),
                          'accounting': sqlite_summary(args.trial_dir)}, indent=2))
        return 0
    if args.action == 'report':
        report = report_trial(args.trial_dir)
        if args.output:
            if args.output.exists():
                raise ValueError('Report output exists; refusing replacement')
            save_json(args.output, report)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    return worker(args.trial_dir, args.resume, args.cancel) if args.action == '_worker' else step(
        args.trial_dir, args.resume, args.action == 'cancel')


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, sqlite3.Error) as error:
        print(canonical({'kind': 'experiment_error', 'type': type(error).__name__, 'message': str(error)[:1200]}), flush=True)
        raise SystemExit(1)

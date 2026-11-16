"""Opt-in production-gateway validation against frozen synthetic task fixtures.

Requires a running local Ollama server and this Mac's telemetry helper. No
workspace tools are granted. The independent grader may execute a restricted
synthetic function; it does not confer test authority on a model response.
"""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import threading
import time

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO/'harness'))
import conversation as chat
from invocation_gateway import InvocationLedger
from lifecycle import deadline
from monitoring import resource_state
from review_budget import usage
from vscode_bridge import ollama


def graph_score(actual):
    # The paging prompt names fields, but does not prescribe an edge encoding.
    if not isinstance(actual, dict):
        return False
    edges = actual.get('edges')
    if not isinstance(edges, list):
        return False
    pairs = []
    for edge in edges:
        if isinstance(edge, dict):
            edge = [edge.get('source'), edge.get('target')]
        if not isinstance(edge, list) or len(edge) != 2 or not all(isinstance(v, str) for v in edge):
            return False
        pairs.append(edge)
    return sorted(pairs) == sorted([['retry', 'retry'], ['retry', 'normalize']]) and actual.get('recursive_functions') == ['retry']


def paging_json(answer):
    # Unlike the frozen JSON-only fixtures, paging may include a source block.
    blocks = re.findall(r'^```json\s*\n(.*?)^```\s*$', answer, re.M | re.S)
    try:
        return json.loads(blocks[0]) if len(blocks) == 1 else None
    except ValueError:
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--fixtures', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--paging', action='store_true')
    args = parser.parse_args()
    sys.path.insert(0, str(args.fixtures.resolve()))
    import fixture
    raw = (args.fixtures/'private-fixtures.json').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == (args.fixtures/'fixture.sha256').read_text().strip()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    os.environ.update(SUTRA_RUNTIME_DIR=str(Path.home()/'Library/Application Support/Code/User/globalStorage/deepak-local.karma-local-harness/runtime-memory'),
                      SUTRA_MEMORY_OWNERSHIP='shared', SUTRA_THINKING='adaptive', SUTRA_ACTIVITY='0')
    monitor = subprocess.Popen([str(REPO/'bin/telemetry')], stdout=subprocess.PIPE, text=True)

    def collect():
        for line in monitor.stdout:
            try:
                json.loads(line)
                temp = root/'telemetry.tmp'
                temp.write_text(line)
                temp.replace(root/'telemetry.json')
            except (ValueError, OSError):
                pass

    threading.Thread(target=collect, daemon=True).start()
    ledger = InvocationLedger(root/'state.sqlite')
    models = {'low': 'qwen3:4b', 'mid': 'local-assistant:latest', 'high': 'local-assistant:latest'}
    meta = {'workspace': str(root)}
    results = {'completed': False, 'fixture_sha256': hashlib.sha256(raw).hexdigest(),
               'rows': [], 'contracts': {}, 'sessions': {}}

    def save():
        (root/'results.json').write_text(json.dumps(results, indent=2)+'\n')

    def ask(label, prompt, parent=None, tier='mid', case=None, **extra):
        start = time.monotonic()
        request = dict(prompt=prompt, parent=parent, tier=tier, models=models, **extra)
        with deadline():
            out = chat.answer(ledger, meta, root, request)
        sid = out['session']
        pinned = chat.contract(ledger, sid)
        if sid in results['contracts']:
            assert pinned == results['contracts'][sid]
        else:
            results['contracts'][sid] = pinned
        spent = usage(ledger, sid)
        old = results['sessions'].get(sid)
        if old:
            assert all(spent['used'][k] >= v for k, v in old['used'].items())
        results['sessions'][sid] = spent
        assert ledger.verify_invocations(sid)
        assert not out['workspace_tools_used']
        proposal = out['result'].get('accepted_output') or {}
        row = dict(label=label, seconds=round(time.monotonic()-start, 3), output=out)
        if case:
            row['score'] = fixture.score(case, proposal.get('answer', ''))
        results['rows'].append(row)
        save()
        print(json.dumps({'label': label, 'seconds': row['seconds'], 'admitted': out['result']['admission'],
                          'reasoning': out['reasoning']['requested'], 'score': row.get('score')}), flush=True)
        if not out['result']['admission']['admitted']:
            raise RuntimeError('Withheld production response: '+label)
        return out

    try:
        for _ in range(50):
            if (root/'telemetry.json').exists():
                break
            time.sleep(.1)
        assert resource_state(root)['admitted'], 'Resources are not ready'
        assert not ollama('/api/ps')['models'], 'Defer: another model is resident'
        results['runtime'] = ollama('/api/version')
        save()
        if args.paging:
            from test_conversation import Fake
            seed_models = dict.fromkeys(('low', 'mid', 'high'), 'small')
            seed = chat.answer(ledger, meta, root, dict(prompt='Archived source packet:\n```python\n'+fixture.PAGE+'```',
                               models=seed_models, tier='low'), Fake(fault=lambda r, p: r.update(summary='Archived source.')))
            target = parent = seed['result']['invocation_id']
            for i in range(6):
                out = chat.answer(ledger, meta, root, dict(prompt='Topic '+str(i)+' '+('filler '*120), parent=parent,
                                  models=seed_models, tier='low'), Fake(fault=lambda r, p: r.update(answer='A bounded response. '*200,
                                                                                               summary='Unrelated topic.')))
                parent = out['result']['invocation_id']
            results['seeded_fixture_turns'] = 7
            prompt = ('Consult saved entry '+target+'. Identify its function-call relationships and any recursion. '
                      'Include a fenced JSON object with edges and recursive_functions. Do not guess.')
            out = ask('request-archived-evidence', prompt, parent, one_step=True)
            follow = out.get('routing_followup')
            results['explicit_page_request'] = bool(follow and follow['kind'] == 'page_in')
            if results['explicit_page_request']:
                out = ask('consume-archived-evidence', prompt, parent,
                          conversation_session=out['session'], routing_continuation=out['result']['invocation_id'])
            actual = paging_json(out['result']['accepted_output']['answer'])
            results['paging_answer_correct'] = graph_score(actual)
        else:
            out = ask('greeting', 'Hello there', tier='auto')
            out = ask('small-anchor', 'Remember: project Cedar, ticket CED724. Keep this discussion read-only; no network, no new dependencies, and keep the STP kernel unchanged.',
                      out['result']['invocation_id'], tier='auto')
            parent = out['result']['invocation_id']
            for i, case in enumerate(fixture.SEQUENCES['allocation']):
                out = ask('allocation-'+str(i+1), case['prompt'], parent, case=case)
                parent = out['result']['invocation_id']
            out = ask('switch-back-recall', 'What project and ticket did I specify, and what constraints must still apply? Reply with a fenced JSON object: project, ticket, constraints (list).',
                      parent, tier='low')
            results['recall'] = fixture.extract_json(out['result']['accepted_output']['answer'])
            for name in ('retry', 'allocation_holdout'):
                parent = None
                for i, case in enumerate(fixture.SEQUENCES[name]):
                    out = ask(name+'-'+str(i+1), case['prompt'], parent, case=case)
                    parent = out['result']['invocation_id']
        results['completed'] = True
    except BaseException as exc:
        results['error'] = type(exc).__name__+': '+str(exc)
        raise
    finally:
        save()
        ledger.close()
        monitor.terminate()
        monitor.wait(timeout=5)


if __name__ == '__main__':
    main()

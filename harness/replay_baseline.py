"""Replay recorded proposals through the policy check; no model calls or repair."""
import argparse
import json
from pathlib import Path
from checked_protocol import HOST_FIELDS
from policy_check import scenario_request, check


def replay(root):
    plan = json.loads((root / 'plan.json').read_text())
    definitions = {d['id']: d for d in plan['new_cases']}
    rows = []
    for result in json.loads((root / 'results.json').read_text()):
        definition = definitions.get(result['case'], {})
        if result['mode'] != 'dag' or 'scenario' not in definition or definition['family'] == 'stale':
            continue
        record = json.loads((root / 'calls' / (result['call_ids'][-1] + '.json')).read_text())
        payload = json.loads(record['request']['messages'][1]['content'])
        kind = 'retry' if definition['family'] == 'retry' else 'release'
        payload['policy_check'] = scenario_request(kind, definition['scenario'])
        proposal = {key: value for key, value in record['answer'].items() if key not in HOST_FIELDS}
        rows.append({'case': result['case'], 'originally_admitted': result['admission']['admitted'],
                     'raw_interpretation': proposal['interpretation'], 'verification': check(payload, proposal)})
    return {'mode': 'offline replay, archived source snapshots; no inference or response repair',
            'private_expected_answers_used': False, 'cases': rows}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('baseline')
    args = parser.parse_args()
    print(json.dumps(replay(Path(args.baseline)), indent=2))

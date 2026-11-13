"""The host owns task state; the model returns only a proposed result/action."""
import copy
import json
from dag_protocol import SCHEMA as LEGACY_SCHEMA, SYSTEM as LEGACY_SYSTEM

HOST_FIELDS = {'task_id', 'goal', 'constraints', 'selected_option', 'tests_status'}
SCHEMA = copy.deepcopy(LEGACY_SCHEMA)
for field in HOST_FIELDS:
    del SCHEMA['properties'][field]
SCHEMA['required'] = list(SCHEMA['properties'])
SYSTEM = LEGACY_SYSTEM.replace(
    'Copy task_id, goal wording, every constraint (no extras), and selected_option. Constraint values are strings: true/false, digits, comma-separated paths. Absent selection/target is "".',
    'The host attaches task_id, goal, constraints, selected_option and tests_status from the delivered contract; do not return those fields. Obey that contract when proposing actions. An absent target is "".'
).replace('Preserve contract.tests_status.',
    'Use contract.tests_status to choose the next coding operation; do not return tests_status. '
    'The interpretation answers the policy question; action independently continues the coding task. '
    'policy_check.inputs, when present, are scenario inputs, separate from the coding task test status.')
SYSTEM += '\nIf repair is supplied, address its host validation errors using the current sources.'


def worker_schema(payload):
    """Constrain protocol shape and workflow from host state, never fact values.

    No computed policy decision is used here. Unknown interpretation text remains
    unconstrained so the independent semantic gate can detect wrong answers.
    """
    # Shared primitive dictionaries in the base schema must not share mutations:
    # restricting target addresses must never constrain fact/interpretation values.
    schema = json.loads(json.dumps(SCHEMA))
    fields = schema['properties']
    index = payload['evidence_index']
    supported = [item['key'] for item in index if item['state'] == 'supported']
    unavailable = [item['key'] for item in index if item['state'] != 'supported']
    for name, keys in [('facts', supported), ('unavailable_evidence', unavailable)]:
        fields[name]['minItems'] = fields[name]['maxItems'] = len(keys)
        if keys:
            fields[name]['items']['properties']['key']['enum'] = keys
    if unavailable:
        fields['unavailable_evidence']['items']['properties']['state']['enum'] = sorted(
            {item['state'] for item in index if item['state'] != 'supported'})
    pages = {page['id']: page for page in payload['pages']}
    incomplete = any(dep not in pages or pages[dep]['version'] != version
                     for page in pages.values() for dep, version in page.get('depends_on', []))
    contract = payload['contract']
    if (not contract['task_id'] or not contract['goal'] or incomplete
        or any(item['state'] != 'supported' and (item['key'] != 'test_returncode' or item['state'] == 'conflicting') for item in index)):
        action, targets = 'clarify', ['']
    elif contract['tests_status'] == 'passed':
        action, targets = 'report', ['']
    elif contract['implementation_present']:
        action, targets = 'run_tests', [p for p in contract['constraints']['allowed_paths'] if p.startswith('tests/')]
    else:
        action, targets = 'edit', [p for p in contract['constraints']['allowed_paths'] if not p.startswith('tests/')]
    if not targets:
        action, targets = 'clarify', ['']
    fields['action']['enum'] = [action]
    fields['target']['enum'] = targets
    return schema


def attach_contract(proposal, contract):
    """Never treat a model-supplied field as authoritative task state."""
    if not isinstance(proposal, dict) or set(proposal) != set(SCHEMA['properties']):
        raise ValueError('Unexpected or missing model proposal fields')
    if (proposal['action'] not in SCHEMA['properties']['action']['enum']
        or type(proposal['complete']) is not bool or not isinstance(proposal['target'], str)
        or not isinstance(proposal['interpretation'], str)
        or not isinstance(proposal['interpretation_sources'], list)
        or any(not isinstance(p, str) for p in proposal['interpretation_sources'])):
        raise ValueError('Invalid model proposal field types')
    def encode(value):
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, list):
            return ','.join(value)
        return str(value)
    answer = copy.deepcopy(proposal)
    answer.update(task_id=contract['task_id'], goal=contract['goal'],
        selected_option=contract['selected_option'] or '', tests_status=contract['tests_status'],
        constraints=[{'key': key, 'value': encode(value)}
                     for key, value in sorted(contract['constraints'].items())])
    return answer

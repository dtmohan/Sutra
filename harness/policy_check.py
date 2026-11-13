"""Fail-closed evaluator for two explicitly supported controlled-prose policies.

Rules are parsed from the delivered, version-pinned source spans. Scenario inputs
come from the host adapter, never from the worker or private expected answers.
This is not an arbitrary-prose semantic verifier. Unknown text is unverified.
"""
import re
from context_manager import canonical, sha


class UnverifiablePolicy(ValueError):
    pass


def validate_request(spec):
    if not isinstance(spec, dict) or set(spec) != {'kind', 'entry_page', 'inputs'}:
        raise UnverifiablePolicy('Invalid policy check request')
    if not isinstance(spec['entry_page'], str) or not spec['entry_page']:
        raise UnverifiablePolicy('Missing policy entry page')
    values = spec['inputs']
    kinds = {'retry': {'environment': str, 'plan': str, 'incident': bool},
             'release': {'approval': bool, 'tests': str, 'freeze': bool}}
    if spec['kind'] not in kinds or not isinstance(values, dict):
        raise UnverifiablePolicy('Unsupported policy kind')
    fields = kinds[spec['kind']]
    if set(values) != set(fields) or any(type(values[k]) is not typ for k, typ in fields.items()):
        raise UnverifiablePolicy('Missing, extra or incorrectly typed scenario inputs')
    if any(isinstance(v, str) and not v for v in values.values()):
        raise UnverifiablePolicy('Empty scenario input')
    if spec['kind'] == 'release' and values['tests'] not in {'stale', 'failed', 'unverified', 'current and passed'}:
        raise UnverifiablePolicy('Unknown scenario test status')
    return spec


def scenario_request(kind, scenario, entry_page='policy-entry'):
    """Explicit adapter for key=value scenarios; no defaults or inference."""
    inputs = {}
    for item in scenario.strip().rstrip('.').split(';'):
        key, value = [part.strip() for part in item.split('=', 1)]
        if key in inputs:
            raise UnverifiablePolicy('Duplicate scenario input')
        inputs[key] = {'true': True, 'false': False}.get(value, value)
    return validate_request({'kind': kind, 'entry_page': entry_page, 'inputs': inputs})


def source_rules(payload, entry):
    pages = {p['id']: p for p in payload['pages']}
    if len(pages) != len(payload['pages']):
        raise UnverifiablePolicy('Duplicate delivered page')
    ordered, visiting, visited = [], set(), set()
    def visit(key, version=None):
        if key in visiting:
            raise UnverifiablePolicy('Cyclic policy dependencies')
        page = pages.get(key)
        if not page or (version is not None and page['version'] != version):
            raise UnverifiablePolicy('Missing policy prerequisite: ' + key)
        if page['authority'] not in {'user', 'file', 'tool'} or page['status'] not in {'current', 'aging'} or not page.get('source_span'):
            raise UnverifiablePolicy('Policy requires current authoritative source spans')
        if key in visited:
            return
        visiting.add(key)
        for dep, dep_version in page.get('depends_on', []):
            visit(dep, dep_version)
        visiting.remove(key); visited.add(key); ordered.append(page)
    visit(entry)
    if not re.fullmatch(r'Policy entry [\w-]+\. Evaluate the scenario using all prerequisite rules\.', ordered[-1]['text'].strip()):
        raise UnverifiablePolicy('Unsupported policy entry text')
    return ordered


def match(pattern, text):
    found = re.fullmatch(pattern, ' '.join(text.split()))
    if not found:
        raise UnverifiablePolicy('Unsupported or ambiguous policy rule text')
    return found.groups()


def evaluate(kind, texts, values):
    if kind == 'retry':
        if len(texts) != 3:
            raise UnverifiablePolicy('Retry policy requires exactly three rules')
        environment, outside = match(r'Retries are enabled only in (\w+)\. Outside \1 return ([A-Z_]+), regardless of plan or incident state\.', texts[0])
        plan, preferred, other = match(r'Within the enabled scope, (\w+) requests use retry_limit=(\d+); other plans use retry_limit=(\d+)\.', texts[1])
        incident, = match(r'Within that same enabled scope, an active incident overrides both plans: retry_limit=(\d+)\.', texts[2])
        if values['environment'] != environment:
            return outside
        limit = incident if values['incident'] else preferred if values['plan'] == plan else other
        return 'retry_limit=' + str(int(limit))
    if kind == 'release':
        if len(texts) != 2:
            raise UnverifiablePolicy('Release policy requires exactly two rules')
        denied, = match(r'Release is allowed only when approval is recorded AND tests are current and passed\. Otherwise return ([A-Z_]+)\.', texts[0])
        frozen, allowed = match(r'An active freeze overrides permission: return ([A-Z_]+) even when approval and tests satisfy the prerequisites\. Otherwise return ([A-Z_]+) if all prerequisites hold\.', texts[1])
        if denied != frozen or allowed == denied:
            raise UnverifiablePolicy('Inconsistent release outcomes')
        return allowed if values['approval'] and values['tests'] == 'current and passed' and not values['freeze'] else denied
    raise UnverifiablePolicy('Unsupported policy kind')


def check(payload, proposal):
    spec = payload.get('policy_check')
    if spec is None:
        return {'status': 'unconfigured' if proposal['interpretation'] else 'not_requested',
                'verified': False, 'errors': ['interpretation has no configured verifier'] if proposal['interpretation'] else []}
    try:
        validate_request(spec)
        pages = source_rules(payload, spec['entry_page'])
        decision = evaluate(spec['kind'], [p['text'] for p in pages[:-1]], spec['inputs'])
    except (UnverifiablePolicy, KeyError, TypeError, ValueError) as exc:
        abstained = proposal.get('action') == 'clarify' and not proposal.get('interpretation') and not proposal.get('complete')
        return {'status': 'unverifiable', 'verified': False, 'reason': str(exc),
                'errors': [] if abstained else ['policy cannot be verified: ' + str(exc)]}
    equal = proposal.get('interpretation') == decision
    return {'status': 'verified' if equal else 'mismatch', 'verified': equal,
            'computed_decision': decision, 'proposed_decision': proposal.get('interpretation'),
            'scope': 'supported controlled-prose policy with host-supplied scenario inputs',
            'input_hash': sha(canonical(spec)),
            'source_pages': [{'id': p['id'], 'version': p['version'], 'hash': p['hash']} for p in pages],
            'errors': [] if equal else ['policy decision does not match evaluated source rules']}

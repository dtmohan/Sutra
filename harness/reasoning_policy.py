"""Host-selected Ollama reasoning controls; model requests never set transport.

Named controls must be advertised. `false` is Ollama's documented generic
lowest-effort request, even when disabling thinking is unsupported. A later
advertised positive control is a distinct request, not proof of more compute:
some backends map `false` to the same effective mode. Metadata can be sparse.
This policy changes allocation of inference work, not kernel decisions.
"""
import json
import re
from context_manager import InvalidUpdate

LEVELS = ('minimal', 'low', 'medium', 'high', 'xhigh', 'max')
PREFERENCES = ('adaptive', 'off', 'on', 'model-default')


def capabilities(info):
    raw = info.get('thinking') or {}
    values = raw.get('values', []) if isinstance(raw, dict) else []
    if (not isinstance(values, list) or len(values) > 16 or
            any(type(v) is not bool and (not isinstance(v, str) or not 1 <= len(v) <= 32)
                for v in values)):
        values = []
    values = list(dict.fromkeys(values))
    default = raw.get('default') if isinstance(raw, dict) else None
    if type(default) not in (bool, str) or default not in values:
        default = None
    return {'values': values, 'default': default}


def ladder(cap):
    values = cap['values']
    if not values:
        return [None]  # Unknown support: use the default, never invent a level.
    names = [v for v in values if isinstance(v, str)]
    if names:
        if True in values or any(v not in LEVELS for v in names):
            return [None]  # No established ordering for an unfamiliar control.
        ordered = [v for v in LEVELS if v in names]
        return [False] + ordered
    return [False, True] if True in values else [False]


def static_control(cap, preference):
    if preference == 'off':
        return False
    if preference == 'model-default':
        return None
    if preference == 'on':
        if True in cap['values']:
            return True
        return next((v for v in LEVELS if v in cap['values']),
                    next((v for v in cap['values'] if isinstance(v, str)), False))
    raise InvalidUpdate('Unknown reasoning preference')


def make(cap, preference, step=0):
    if preference not in PREFERENCES or type(step) is not int or step < 0:
        raise InvalidUpdate('Invalid reasoning selection')
    choices = ladder(cap) if preference == 'adaptive' else [static_control(cap, preference)]
    if step >= len(choices):
        raise InvalidUpdate('No higher supported reasoning control')
    return {'version': 1, 'preference': preference, 'capabilities': cap,
            'step': step, 'requested': choices[step]}


def validate(plan):
    if (not isinstance(plan, dict) or type(plan.get('version')) is not int or
            type(plan.get('requested')) not in (type(None), bool, str)):
        raise InvalidUpdate('Invalid reasoning control type')
    cap = capabilities({'thinking': plan.get('capabilities')})
    if plan != make(cap, plan.get('preference'), plan.get('step')):
        raise InvalidUpdate('Reasoning selection differs from supported controls')


def next_control(plan):
    if not plan:
        return None
    validate(plan)
    if plan['preference'] != 'adaptive':
        return None
    choices = ladder(plan['capabilities'])
    step = plan['step'] + 1
    return choices[step] if step < len(choices) else None


def task_signal(p):
    """Use request cues before classification, without an extra model call."""
    request = p['request'].lower()
    if re.search(r'\b(think carefully|reason (?:step by step|through))\b', request):
        return 'explicit-reasoning-request'
    if re.search(r'\bfenced\s+(?:json|python)\b', request):
        return 'explicit-structured-result'
    if re.search(r'\b(maximi[sz]e|minimi[sz]e|optimal|optimi[sz]e|recompute|calculate|correction|best remaining|best plan)\b', request) and re.search(r'\d', request):
        return 'numeric-planning-or-correction'
    if (re.search(r'\b(analy[sz]e|debug|diagnose|trace|corrected|fix|refactor)\b', request)
            and ('```' in request or re.search(r'\b(function|code|algorithm|implementation|stack trace)\b', request))):
        return 'supplied-code-analysis'
    if re.search(r'\b(trade[- ]?offs?|pros and cons)\b', request):
        return 'trade-off-analysis'
    if (re.search(r'\b(compare|evaluate|assess|weigh|design|plan|recommend)\b', request)
            and re.search(r'\b(approach(?:es)?|alternatives?|options?|strateg(?:y|ies)|risks?|constraints?|budget|schedule)\b', request)):
        return 'comparison-or-planning'
    return 'routine-or-unclassified'


def initial_step(p, cap, preference):
    if preference != 'adaptive' or task_signal(p) == 'routine-or-unclassified':
        return 0
    return 1 if len(ladder(cap)) > 1 else 0


def prepare(s, sid, p, cap, preference):
    """Preserve the effort rung across paging, reloads and resource recovery."""
    step = initial_step(p, cap, preference)
    previous = p['routing_state']['previous']
    if previous:
        row = s.db.execute('SELECT d.payload_json FROM invocations i JOIN deliveries d '
                           'ON d.id=i.delivery_id WHERE i.id=? AND i.session=?', (previous, sid)).fetchone()
        if not row:
            raise InvalidUpdate('Reasoning continuation receipt is unavailable')
        prior = json.loads(row[0])
        old = prior.get('reasoning')
        if old and prior['route']['model'] == p['route']['model']:
            from conversation_routing import followup, RoutingBlocked
            validate(old)
            if old['capabilities'] != cap or old['preference'] != preference:
                raise RoutingBlocked('Reasoning settings or supported controls changed during a saved request. '
                                     'Restore them or start a separate request; spent budget is retained.')
            proposal = s.load_result(sid, previous)
            action = followup(proposal, prior)
            if action['kind'] not in ('page_in', 'reasoning'):
                raise RoutingBlocked('This model has already exhausted its reasoning choices for this request.')
            step = old['step'] + (action['kind'] == 'reasoning')
    return make(cap, preference, step)


def check(s, sid, p):
    plan = p['reasoning']
    validate(plan)
    if plan != prepare(s, sid, p, plan['capabilities'], plan['preference']):
        raise InvalidUpdate('Reasoning progression differs from its receipts')


def check_provider(provider, payload, fresh=False):
    plan = payload.get('reasoning')
    if plan is None:
        return
    validate(plan)
    info = getattr(provider, 'context_metadata', {})
    if fresh:
        from vscode_bridge import ollama
        info = ollama('/api/show', {'model': provider.model})
    if (capabilities(info) != plan['capabilities'] or
            type(getattr(provider, 'thinking', None)) is not type(plan['requested']) or
            provider.thinking != plan['requested']):
        raise InvalidUpdate('Provider reasoning control or capabilities changed before dispatch')

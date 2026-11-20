"""Read-only downstream planning for architecture discovery.

A selector plans headroom for a writer and a first claim-review page. This is
an estimated holdback checked against existing task/ancestor gates, NOT a
durable reservation or a guarantee that an unknown reviewer page will fit.
No provider, tokenizer, source reader, allocation recovery or dispatcher is used.
"""
import copy
import math

from context_manager import ContextBudgetExceeded, InvalidUpdate, canonical, sha
from paging import Budget


PROFILE = 'discovery-downstream-capacity-v2'
VERSION = 2
REVIEWER_HEADROOM_MULTIPLIER = 2
MAX_PLAN_BYTES = 8192
_DIMENSIONS = {'calls', 'tokens', 'operations', 'source_bytes', 'seconds', 'actions'}


def _budget(value):
    if not isinstance(value, Budget):
        raise InvalidUpdate('Downstream capacity needs a host Budget')
    for key in ('context_limit', 'response_reserve', 'template_reserve', 'input_limit'):
        number = getattr(value, key)
        if type(number) is not int or number < (0 if key == 'template_reserve' else 1):
            raise InvalidUpdate('Invalid downstream capacity budget')
    if value.response_reserve >= value.context_limit:
        raise InvalidUpdate('Downstream output reserve leaves no physical input capacity')
    return value


def _cost(value):
    if not isinstance(value, dict) or set(value) - _DIMENSIONS:
        raise InvalidUpdate('Invalid downstream capacity dimensions')
    for number in value.values():
        if (type(number) not in (int, float) or not math.isfinite(number)
                or not 0 <= number <= 2**53 - 1):
            raise InvalidUpdate('Invalid downstream capacity measurement')
    return dict(value)


def _binding(sid, parent, data, writer, reviewer, explicit_reviewer):
    if not isinstance(sid, str) or not sid or len(sid) > 128:
        raise InvalidUpdate('Invalid downstream capacity session')
    if not isinstance(parent, dict) or not isinstance(data, dict):
        raise InvalidUpdate('Invalid downstream planning input')
    return {'session': sid, 'parent_hash': sha(canonical(parent)),
            'source_view_hash': sha(canonical(data)),
            'writer_budget_hash': sha(canonical(vars(writer))),
            'reviewer_budget_hash': sha(canonical(vars(reviewer))),
            'reviewer_budget_explicit': explicit_reviewer}


def _physical(required, budget):
    # Wire bytes are a conservative planning bound. An exact-profile model can
    # fit even when that byte bound fails; do not turn unknown into a denial or
    # invoke its tokenizer here. Actual dispatch still checks its exact proof.
    if budget.context_profile == 2:
        from context_window import STRATA_CONTEXT_SLACK
        return True if required + STRATA_CONTEXT_SLACK <= budget.context_limit else None
    return required <= budget.context_limit


def _physical_output(budget):
    if budget.context_profile == 2:
        from context_window import STRATA_CONTEXT_SLACK
        return budget.response_reserve + STRATA_CONTEXT_SLACK
    return budget.response_reserve


def _writer(s, parent, data, budget):
    import chat_runtime as chat
    import review_context as context
    from review_role import constraint_rows

    packet = copy.deepcopy(parent)
    packet['constraint_echo'] = constraint_rows(packet['contract'])
    packet['memory_budget'] = vars(budget).copy()
    source_funded = (packet.get('parent_source_funding_version') == 1
                     and budget.context_profile == 1)
    physical_limit = budget.context_limit
    source_room = ((physical_limit - budget.response_reserve - budget.template_reserve) // 2
                   if source_funded else physical_limit // 3)
    packet['excerpt_bytes'] = max(64, min(65536, source_room))
    packet['map_bytes'] = max(512, min(65536, physical_limit // 3))
    if chat.focused_architecture(packet):
        packet['map_bytes'] = min(1024, packet['map_bytes'])
    if packet.get('parent_source_funding_version') == 1:
        packet['parent_discovery_note_limit'] = len(packet.get('discovery_notes_unverified', []))
        packet['parent_child_note_limit'] = len(packet.get('child_results', []))
        packet['parent_memory_extra_bytes'] = 0
    while True:
        packet['excerpts'] = chat.packet_excerpts(packet, data)
        if not packet['excerpts']:
            raise ContextBudgetExceeded('No source excerpt fits the writer planning view')
        context.evidence(packet, data)
        packet['facts'] = chat.facts(data['graph'], packet['sites'])
        # Never materialize new memory or retire mandatory conversation pins.
        packet['memory'] = chat.conversation_pages(s, packet, budget, mandatory_only=True)
        schema = chat.schema(packet)
        probe = dict(packet, repair={'errors': ['x' * 140] * 2})
        charge = budget.charge(chat.SYSTEM, probe, schema)
        required = charge + budget.response_reserve
        physical = _physical(required, budget)
        minimum_view = packet['map_bytes'] == 512 and packet['excerpt_bytes'] == 64
        if physical is not False or minimum_view:
            return packet, {'input_charge': charge, 'output_reserve': budget.response_reserve,
                'required_units': required, 'financial_fits': required <= budget.input_limit,
                'financial_limit_units': budget.input_limit,
                'physical_fit': physical, 'physical_method': 'conservative_wire_bound',
                'physical_context_tokens': budget.context_limit,
                'physical_output_reserve': _physical_output(budget),
                'packet_hash': sha(canonical(probe)), 'schema_hash': sha(canonical(schema)),
                'excerpt_files': len(packet['excerpts']),
                'excerpt_bytes': sum(len(t.encode()) for t in packet['excerpts'].values())}
        # Only a legacy physical-window failure reduces this planning view.
        # Financial shortage and an inconclusive exact-window bound do not.
        if packet['map_bytes'] > 512:
            packet['map_bytes'] = max(512, packet['map_bytes'] * 4 // 5)
        elif packet['excerpt_bytes'] > 64:
            packet['excerpt_bytes'] = max(64, packet['excerpt_bytes'] * 4 // 5)


def _reviewer(writer, budget):
    import claim_pages
    import report_review

    candidate = '0' * 32  # Planning-only identity; never registered or dispatched.
    evidence = claim_pages.inventory(candidate, writer)
    if not evidence:
        raise ContextBudgetExceeded('No source unit is available for the reviewer planning page')
    # Probe the largest existing transfer unit, with the actual reviewer schema.
    # Claim text is only a representative placeholder; it is not a claim about
    # source, a universal lower bound, or the unknown writer's eventual draft.
    eid = max(evidence, key=lambda key: len(canonical(evidence[key]).encode()))
    claims = {'overview': {'text': 'Review the proposed explanation against supplied source.',
                           'paths': [evidence[eid]['path']]}}
    plan = {'id': '0' * 32, 'pages': [{'claims': ['overview'], 'evidence': [eid]}]}
    packet = claim_pages.packet(writer, {'planning_only': True}, candidate, plan, 0,
                                claims, evidence, {}, False)
    schema = report_review.schema(packet)
    probe = dict(packet, repair={'errors': ['x' * 160] * 2})
    charge = budget.charge(report_review.SYSTEM, probe, schema)
    required = charge + budget.response_reserve
    if required > budget.input_limit or _physical(required, budget) is False:
        raise ContextBudgetExceeded('The representative reviewer page exceeds its declared allocation')
    return {'probe_input_charge': charge, 'output_reserve': budget.response_reserve,
            'probe_required_units': required, 'financial_fits': required <= budget.input_limit,
            'physical_fit': _physical(required, budget),
            'physical_method': 'conservative_wire_bound',
            'physical_context_tokens': budget.context_limit,
            'physical_output_reserve': _physical_output(budget),
            'page_ceiling_units': budget.input_limit,
            'probe_hash': sha(canonical(probe)), 'schema_hash': sha(canonical(schema)),
            'known_source_units': len(evidence)}


def _seal(plan):
    plan['digest'] = sha('discovery-capacity-v' + str(plan['version']) + ':' + canonical(plan))
    if len(canonical(plan).encode()) > MAX_PLAN_BYTES:
        raise InvalidUpdate('Downstream capacity observation exceeds its allocation')
    return plan


def measure(s, sid, parent_packet, data, writer_budget, *, reviewer_budget=None,
            writer_seconds=150, reviewer_seconds=150):
    """Measure a copied parent view and estimate first-page review headroom.

    Supplied Budgets are the caller's current declared profiles; this helper
    never expands them. A missing explicit reviewer budget is disclosed as a
    same-budget proxy. The actual future writer/page still needs fresh physical
    and financial admission. The versioned two-probe margin is empirical, not
    an upper bound on the unknown draft. Stage allowance uses the separate
    declared-ceiling basis, so changing this estimate does not change that cap.
    Seconds must match caller-selected transport caps.
    """
    writer = _budget(writer_budget)
    explicit = reviewer_budget is not None
    reviewer = _budget(reviewer_budget if explicit else writer_budget)
    for seconds in (writer_seconds, reviewer_seconds):
        if type(seconds) is not int or not 1 <= seconds <= 3600:
            raise InvalidUpdate('Invalid downstream transport planning limit')
    binding = _binding(sid, parent_packet, data, writer, reviewer, explicit)
    result = {'version': VERSION, 'profile': PROFILE, 'mode': 'planning_holdback',
              'binding': binding, 'durable_reservation': False,
              'full_publication_guaranteed': False, 'reviewer_pages_targeted': 1,
              'writer_capacity_guaranteed': False,
              'reviewer_page_capacity_guaranteed': False,
              'repair_calls_held': 0, 'reviewer_budget_source':
                  'explicit' if explicit else 'same_writer_budget_proxy',
              'headroom_calibration': 'representative_reviewer_probe_times_two_v2',
              'headroom_multiplier': REVIEWER_HEADROOM_MULTIPLIER,
              'cost_floor_kind': 'measured_planning_packets_not_a_future_execution_lower_bound',
              'writer_view': 'known_parent_planning_packet_not_final_dispatch',
              'dispatch_requires_fresh_fit': True,
              'unprotected_work': ['first_reviewer_page_exceeding_estimate', 'additional_claim_pages',
                                   'repair_attempts', 'new_source_operations']}
    try:
        packet, result['writer'] = _writer(s, parent_packet, data, writer)
        result['reviewer'] = _reviewer(packet, reviewer)
    except ContextBudgetExceeded:
        result.update(status='unavailable', reason='downstream_packet_unavailable',
                      cost_floor=None, holdback=None, stage_allowance_cost=None)
        return _seal(result)
    result['writer']['transport_seconds'] = writer_seconds
    result['reviewer']['transport_seconds'] = reviewer_seconds
    result['reviewer']['financial_limit_units'] = reviewer.input_limit
    headroom = min(reviewer.input_limit, REVIEWER_HEADROOM_MULTIPLIER * result['reviewer']['probe_required_units'])
    result['reviewer']['estimated_headroom_units'] = headroom
    base = {'calls': 2, 'tokens': result['writer']['required_units'] +
            result['reviewer']['probe_required_units'], 'seconds': writer_seconds + reviewer_seconds}
    result.update(status='measured', cost_floor=base,
                  holdback=dict(base, tokens=result['writer']['required_units'] + headroom),
                  stage_allowance_cost=dict(base, tokens=result['writer']['required_units'] + reviewer.input_limit))
    return _seal(result)


def validate(plan, *, sid=None, parent_packet=None, data=None, writer_budget=None,
             reviewer_budget=None):
    """Check corruption and, optionally, the complete current host input binding.

    A digest is not an authorization signature. Callers must obtain this record
    from their own projection, never accept a model-supplied planning record.
    """
    if (not isinstance(plan, dict) or type(plan.get('version')) is not int
            or plan.get('version') not in (1, 2)
            or plan.get('profile') != 'discovery-downstream-capacity-v' + str(plan['version'])
            or plan.get('mode') != 'planning_holdback'
            or plan.get('durable_reservation') is not False
            or plan.get('full_publication_guaranteed') is not False
            or len(canonical(plan).encode()) > MAX_PLAN_BYTES):
        raise InvalidUpdate('Invalid downstream capacity profile')
    raw = {key: value for key, value in plan.items() if key != 'digest'}
    if plan.get('digest') != sha('discovery-capacity-v' + str(plan['version']) + ':' + canonical(raw)):
        raise InvalidUpdate('Downstream capacity observation changed')
    binding = plan.get('binding')
    binding_fields = {'session', 'parent_hash', 'source_view_hash', 'writer_budget_hash',
                      'reviewer_budget_hash', 'reviewer_budget_explicit'}
    if (not isinstance(binding, dict) or set(binding) != binding_fields
            or not isinstance(binding.get('session'), str) or not 1 <= len(binding['session']) <= 128
            or not binding['session'].isprintable() or not binding['session'].strip()
            or type(binding.get('reviewer_budget_explicit')) is not bool
            or any(not isinstance(binding.get(key), str) or len(binding[key]) != 64
                   or any(char not in '0123456789abcdef' for char in binding[key])
                   for key in binding_fields - {'session', 'reviewer_budget_explicit'})):
        raise InvalidUpdate('Invalid downstream capacity binding')
    if plan['version'] == 2 and (plan.get('writer_capacity_guaranteed') is not False
            or plan.get('reviewer_page_capacity_guaranteed') is not False):
        raise InvalidUpdate('Planning estimates cannot guarantee future stage capacity')
    if plan.get('status') not in ('measured', 'unavailable'):
        raise InvalidUpdate('Invalid downstream capacity state')
    if plan['status'] == 'measured':
        floor = _cost(plan['cost_floor']); holdback = _cost(plan['holdback'])
        writer, reviewer = plan['writer'], plan['reviewer']
        counts = (writer['input_charge'], writer['output_reserve'], writer['required_units'],
                  writer['financial_limit_units'], reviewer['probe_input_charge'],
                  reviewer['output_reserve'], reviewer['probe_required_units'],
                  reviewer['page_ceiling_units'], writer['transport_seconds'], reviewer['transport_seconds'])
        counts += tuple(stage[key] for stage in (writer, reviewer)
                        for key in ('physical_context_tokens', 'physical_output_reserve'))
        if any(type(number) is not int or not 0 < number <= 2**53 - 1 for number in counts):
            raise InvalidUpdate('Invalid downstream stage measurement')
        target_key = 'reviewer_pages_held' if plan['version'] == 1 else 'reviewer_pages_targeted'
        if (plan.get(target_key) != 1 or type(plan[target_key]) is not int
                or plan.get('repair_calls_held') != 0 or type(plan['repair_calls_held']) is not int
                or writer['required_units'] != writer['input_charge'] + writer['output_reserve']
                or reviewer['probe_required_units'] != reviewer['probe_input_charge'] + reviewer['output_reserve']
                or type(writer['financial_fits']) is not bool or type(reviewer['financial_fits']) is not bool
                or writer['financial_fits'] != (writer['required_units'] <= writer['financial_limit_units'])
                or reviewer['financial_fits'] is not True
                or reviewer['probe_required_units'] > reviewer['page_ceiling_units']
                or any(stage['physical_fit'] is not None and type(stage['physical_fit']) is not bool
                       for stage in (writer, reviewer))):
            raise InvalidUpdate('Inconsistent downstream capacity measurements')
        expected = {'calls': 2, 'tokens': writer['required_units'] + reviewer['probe_required_units'],
                    'seconds': writer['transport_seconds'] + reviewer['transport_seconds']}
        declared_cost = dict(expected, tokens=writer['required_units'] + reviewer['page_ceiling_units'])
        if plan['version'] == 1:
            expected_holdback = declared_cost
        else:
            headroom = reviewer.get('estimated_headroom_units')
            if (plan.get('headroom_calibration') != 'representative_reviewer_probe_times_two_v2'
                    or type(plan.get('headroom_multiplier')) is not int
                    or plan['headroom_multiplier'] != REVIEWER_HEADROOM_MULTIPLIER
                    or plan.get('reviewer_page_capacity_guaranteed') is not False
                    or reviewer.get('financial_limit_units') != reviewer['page_ceiling_units']
                    or type(reviewer.get('financial_limit_units')) is not int
                    or type(headroom) is not int
                    or headroom != min(reviewer['page_ceiling_units'],
                                       REVIEWER_HEADROOM_MULTIPLIER * reviewer['probe_required_units'])
                    or _cost(plan['stage_allowance_cost']) != declared_cost):
                raise InvalidUpdate('Downstream headroom calibration differs from its declared basis')
            expected_holdback = dict(expected, tokens=writer['required_units'] + headroom)
        if floor != expected or holdback != expected_holdback:
            raise InvalidUpdate('Downstream capacity cost differs from its stage measurements')
    provided = (sid, parent_packet, data, writer_budget)
    if any(value is not None for value in provided):
        if any(value is None for value in provided):
            raise InvalidUpdate('Downstream binding needs the complete host input')
        explicit = reviewer_budget is not None
        expected = _binding(sid, parent_packet, data, _budget(writer_budget),
                            _budget(reviewer_budget if explicit else writer_budget), explicit)
        if plan['binding'] != expected:
            raise InvalidUpdate('Downstream capacity belongs to another source, task or budget')
    return plan


def gate(s, sid, owner, plan, next_cost=None):
    """Read existing kernel gates for next work plus the uncharged holdback.

    No reservation, ledger mutation, automatic growth, dispatch or cancellation
    is performed. A failed *holdback* is a reason to finish discovery, not to
    claim that the already-earned partial report must be discarded.
    """
    import orchestration
    import review_budget
    from karma_adapter import KarmaAdapter
    from smriti_adapter import SmritiAdapter

    validate(plan)
    if plan['binding']['session'] != sid:
        raise InvalidUpdate('Downstream gate belongs to another session')
    next_cost = _cost({} if next_cost is None else next_cost)
    if plan['status'] != 'measured':
        return {'profile': plan['profile'], 'capacity_fits': None, 'financial_fits': None,
                'reason': 'downstream_packet_unavailable', 'frames': [],
                'combined_cost': None, 'durable_reservation': False}
    combined = {key: next_cost.get(key, 0) + plan['holdback'].get(key, 0)
                for key in set(next_cost) | set(plan['holdback'])}
    frames = orchestration.gates(s, owner, combined)
    task = review_budget.decision(s, sid, combined)
    if task is not None:
        frames.append(task)
    fits = all(frame['decision'] == 'ALLOW' for frame in frames)
    stages = (plan['writer'], plan['reviewer'])
    allocation_frames = []
    for role, stage, required, limit in (
            ('writer', plan['writer'], plan['writer']['required_units'], plan['writer']['financial_limit_units']),
            ('report_reviewer', plan['reviewer'], plan['reviewer']['probe_required_units'],
             plan['reviewer']['page_ceiling_units'])):
        frame = KarmaAdapter().allocations({'tokens': 0}, {'tokens': limit}, {'tokens': required})
        allocation_frames.append(dict(frame, scope='downstream_per_call_reservation', role=role))
        if stage['physical_fit'] is False:
            charge = stage.get('input_charge', stage.get('probe_input_charge'))
            frame = SmritiAdapter().context(charge, stage['physical_output_reserve'],
                                            stage['physical_context_tokens'])
            if frame['decision'] == 'ALLOW':
                raise InvalidUpdate('Physical downstream failure has no denying context frame')
            frames.append(dict(frame, scope='downstream_physical_window', role=role,
                               measurement_basis=stage['physical_method']))
    frames.extend(allocation_frames)
    allocated = all(frame['decision'] == 'ALLOW' for frame in allocation_frames)
    physical = all(stage['physical_fit'] is True for stage in stages)
    if not allocated:
        reason, capacity = 'downstream_per_call_allocation', False
    elif not fits:
        reason, capacity = 'downstream_holdback', False
    elif any(stage['physical_fit'] is False for stage in stages):
        reason, capacity = 'downstream_physical_window', False
    elif not physical:
        reason, capacity = 'physical_fit_unknown', None
    else:
        reason, capacity = 'capacity_available', True
    return {'profile': plan['profile'], 'capacity_fits': capacity, 'financial_fits': fits and allocated,
            'reason': reason, 'frames': frames, 'combined_cost': combined,
            'durable_reservation': False}

"""Host validators for each supported invocation role. No inference here."""
import re
from context_manager import InvalidUpdate
from legacy_model import EXTRACTOR, PATCH_SCHEMA, resolve_operations
from checked_protocol import SYSTEM, worker_schema
from protocol import SELECT_SYSTEM, SELECT_SCHEMA, schema_for


def expected_request(role, payload):
    if role=='memory_probe':
        from memory_probe import SYSTEM as MEMORY_SYSTEM, SCHEMA as MEMORY_SCHEMA
        return MEMORY_SYSTEM, MEMORY_SCHEMA
    if role=='report_reviewer':
        from report_review import SYSTEM as REPORT_SYSTEM, schema
        return REPORT_SYSTEM, schema(payload)
    if role=='subagent':
        from subagent_runtime import SYSTEM as CHILD_SYSTEM,schema
        return CHILD_SYSTEM,schema(payload)
    if role=='conversation':
        from conversation import SYSTEM as CONVERSATION_SYSTEM, schema
        return CONVERSATION_SYSTEM, schema(payload)
    if role=='chat_reviewer':
        from chat_runtime import SYSTEM as CHAT_SYSTEM,schema
        return CHAT_SYSTEM,schema(payload)
    if role == 'static_explainer':
        from static_role import SYSTEM as STATIC_SYSTEM,schema
        return STATIC_SYSTEM,schema(payload)
    if role in ('api_mapper','api_reviewer'):
        from api_roles import MAP_SYSTEM,REVIEW_SYSTEM,schema
        return (MAP_SYSTEM if role=='api_mapper' else REVIEW_SYSTEM),schema(role,payload)
    if role == 'architect':
        from architecture_role import SYSTEM as ARCH_SYSTEM, schema as arch_schema
        return ARCH_SYSTEM, arch_schema(payload)
    if role == 'coder':
        from coding_role import SYSTEM as CODER_SYSTEM, coding_schema
        return CODER_SYSTEM, coding_schema(payload)
    if role == 'reviewer':
        from review_role import SYSTEM as REVIEW_SYSTEM, review_schema
        return REVIEW_SYSTEM, review_schema(payload)
    if role == 'parser': return EXTRACTOR, PATCH_SCHEMA
    if role == 'selector': return SELECT_SYSTEM, schema_for(payload, SELECT_SCHEMA)
    if role == 'worker': return SYSTEM, worker_schema(payload)
    raise InvalidUpdate('Unsupported invocation role: ' + str(role))


def validate_shape(value, schema, path='response'):
    """Validate the exact JSON-schema subset emitted by this harness."""
    supported = {'type','properties','required','additionalProperties','items','enum',
                 'minimum','maximum','minItems','maxItems','minLength','maxLength'}
    if set(schema) - supported: raise InvalidUpdate('Unsupported schema keyword')
    kinds = schema.get('type'); kinds = kinds if isinstance(kinds, list) else [kinds]
    checks = {'object':lambda v:isinstance(v,dict), 'array':lambda v:isinstance(v,list),
              'string':lambda v:isinstance(v,str), 'boolean':lambda v:type(v) is bool,
              'integer':lambda v:type(v) is int, 'null':lambda v:v is None}
    if not any(k in checks and checks[k](value) for k in kinds):
        raise InvalidUpdate(path + ': incorrect type')
    if 'enum' in schema and value not in schema['enum']: raise InvalidUpdate(path + ': value outside allowed choices')
    if isinstance(value, dict):
        if set(schema.get('required', [])) - set(value): raise InvalidUpdate(path + ': missing fields')
        properties = schema.get('properties', {})
        if schema.get('additionalProperties') is False and set(value) - set(properties):
            raise InvalidUpdate(path + ': unexpected fields')
        for key, item in value.items():
            if key in properties: validate_shape(item, properties[key], path + '.' + key)
    elif isinstance(value, list):
        if not schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', len(value)):
            raise InvalidUpdate(path + ': incorrect entry count')
        for item in value: validate_shape(item, schema['items'], path + '[]')
    elif isinstance(value, str):
        if not schema.get('minLength',0) <= len(value) <= schema.get('maxLength',len(value)):
            raise InvalidUpdate(path + ': string length '+str(len(value))+' outside '+str(schema.get('minLength',0))+'..'+str(schema.get('maxLength',len(value)))+' characters; select a shorter exact quote when quoting source')
    elif type(value) is int:
        if not schema.get('minimum', value) <= value <= schema.get('maximum', value):
            raise InvalidUpdate(path + ': out of bounds')


def validate_permission_ops(operations):
    # Bounded, explicit permission language. Quotes alone do not prove polarity.
    forms = {
        'review_only': {False: ('read only review is disabled',), True: ('read only review is required',)},
        'edits_allowed': {False: ('code edits are forbidden', 'do not edit code'), True: ('code edits are allowed',)},
        'tests_allowed': {False: ('test execution is forbidden', 'do not execute tests'), True: ('test execution is allowed',)},
        'network_allowed': {
            False: ('no network access', 'do not access the network', 'network calls are forbidden', 'disable networking', 'disable networking now'),
            True: ('network access is allowed', 'network calls are allowed', 'enable networking', 'enable networking now')},
        'dependencies_allowed': {
            False: ('no new dependencies', 'do not add dependencies', 'adding dependencies is forbidden'),
            True: ('new dependencies are allowed', 'adding dependencies is allowed')},
    }
    for op in operations:
        if op['key'] in forms:
            quote = re.sub(r'\s+', ' ', op['quote'].lower()).strip().rstrip('.!?')
            matches = {v for v, phrases in forms[op['key']].items() if quote in phrases}
            if len(matches) != 1 or op['value'] != str(next(iter(matches))).lower():
                raise InvalidUpdate('Permission polarity unsupported or inconsistent with user source')
        if op['key'] == 'output_format' and op['value'] not in op['quote']:
            raise InvalidUpdate('Output format absent from user source')


def validate_result(store, session, role, payload, schema, proposal, delivery_id):
    validate_shape(proposal, schema)
    if role=='memory_probe':
        from memory_probe import validate
        return validate(store,session,payload,proposal)
    if role=='report_reviewer':
        from report_review import validate
        return validate(store,session,payload,proposal)
    if role=='subagent':
        from subagent_runtime import validate
        return validate(store,session,payload,proposal)
    if role=='conversation':
        from conversation import validate
        return validate(store,session,payload,proposal)
    if role=='chat_reviewer':
        from chat_runtime import validate
        return validate(store,session,payload,proposal)
    if role == 'static_explainer':
        from static_role import validate
        return validate(store,session,payload,proposal)
    if role in ('api_mapper','api_reviewer'):
        from api_roles import validate
        return validate(store,session,role,payload,proposal)
    if role == 'architect':
        from architecture_role import validate
        return validate(store, session, payload, proposal)
    if role == 'coder':
        from coding_role import validate
        return validate(store, session, payload, proposal)
    if role == 'reviewer':
        from review_role import validate
        return validate(store, session, payload, proposal)
    if role == 'parser':
        ops = resolve_operations(payload['CURRENT_USER_MESSAGE'], proposal['operations'])
        validate_permission_ops(ops)
        store.validate_user_patch(session, payload['CURRENT_USER_MESSAGE'], ops)
        return {'admission': {'admitted': True, 'errors': []}, 'operations': ops,
                'checks': ['schema','source_units','typed_updates','permission_polarity','revision']}
    if role == 'selector':
        if not proposal['read_pages'] and not proposal['browse']:
            raise InvalidUpdate('Empty context selection')
        if len(set(proposal['read_pages'])) != len(proposal['read_pages']):
            raise InvalidUpdate('Duplicate page selection')
        for item in proposal['browse']:
            valid = {0} | {d['next_offset'] for d in payload.get('directory', [])
                           if d['group'] == item['group'] and d['next_offset'] is not None}
            if item['offset'] not in valid: raise InvalidUpdate('Invalid catalog cursor')
        for pid in proposal['read_pages']: store.read_page(session, pid)
        return {'admission': {'admitted': True, 'errors': []},
                'checks': ['schema','advertised_addresses','catalog_cursors','readable_pages','revision']}
    result = store.finish(session, delivery_id, proposal, [r['key'] for r in payload['evidence_index']])
    result['checks'] = ['schema','contract','workflow','source_provenance','evidence_coverage','declared_policy']
    return result

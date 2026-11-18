"""Single invocation boundary. Durable dispatch, role checks, receipts, quarantine.

The dispatch permit prevents accidental transport bypass in trusted host code;
it is not a Python sandbox or protection against a privileged process.
"""
import copy
from contextvars import ContextVar
import json
import socket
import time
import uuid
from context_manager import canonical, sha, InvalidUpdate, Conflict
from governance import GovernedContext
from paging import PageFault
from invocation_roles import expected_request, validate_result
from legacy_model import source_units

_permit = ContextVar('model_dispatch_permit', default=None)


def request_hash(call_id, model, system, payload, schema):
    return sha(canonical([call_id, model, system, payload, schema]))


def check_dispatch(call_id, model, system, payload, schema):
    permit = _permit.get()
    if not permit or permit['used'] or permit['hash'] != request_hash(call_id, model, system, payload, schema):
        raise InvalidUpdate('Model transport requires an unused, matching invocation-gateway permit')


def consume_dispatch(call_id, model, system, payload, schema, packet=None):
    permit = _permit.get()
    digest = request_hash(call_id, model, system, payload, schema)
    if not permit or permit['used'] or permit['hash'] != digest:
        raise InvalidUpdate('Model transport requires an unused, matching invocation-gateway permit')
    if permit.get('packet_hash') and (packet is None or sha(canonical(packet)) != permit['packet_hash']):
        raise InvalidUpdate('Model-facing packet differs from committed dispatch')
    permit['used'] = True


class InvocationLedger(GovernedContext):
    def __init__(self, path):
        super().__init__(path)
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS invocations(id TEXT PRIMARY KEY, session TEXT,
            run_id TEXT, call_id TEXT, delivery_id TEXT, record_json TEXT, digest TEXT,
            UNIQUE(run_id,call_id));
          CREATE TABLE IF NOT EXISTS invocation_receipts(session TEXT, seq INTEGER,
            invocation_id TEXT UNIQUE, id TEXT UNIQUE, record_json TEXT,
            PRIMARY KEY(session,seq));
          CREATE TABLE IF NOT EXISTS invocation_quarantine(invocation_id TEXT PRIMARY KEY,
            reason TEXT, created_at REAL);
        ''')
        for table in ('invocations','invocation_receipts','invocation_quarantine'):
            for op in ('UPDATE','DELETE'):
                self.db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{op} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'immutable invocation record'); END")
        self.db.commit()

    def register(self, run_id, call_id, role, payload, system, schema, delivery_id, model, charge, budget=None, resource=None, model_identity=None, model_interface=None):
        expected_system, expected_schema = expected_request(role, payload)
        if system != expected_system or schema != expected_schema:
            raise InvalidUpdate('Role system/schema mismatch')
        envelope = {'id': uuid.uuid4().hex, 'run_id': run_id, 'call_id': call_id,
                    'role': role, 'model': model, 'delivery_id': delivery_id,
                    'system_hash': sha(system), 'schema': schema, 'payload_hash': sha(canonical(payload)),
                    'request_hash': request_hash(call_id, model, system, payload, schema),
                    'model_identity': model_identity, 'charge': charge, 'budget_profile':budget or {'context_limit':8192,'response_reserve':1024,'template_reserve':256}, 'created_at': time.time()}
        if model_interface is not None:
            from model_packet import REFERENCE_VERSIONS, build, descriptor
            packet = build(system, payload, schema)
            if packet is None or model_interface != descriptor(packet, envelope['budget_profile']['template_reserve']):
                raise InvalidUpdate('Model interface does not match committed request')
            if model_interface['role'] != role or charge != model_interface['input_reservation']:
                raise InvalidUpdate('Model packet charge or role changed')
            if model_interface['version'] in REFERENCE_VERSIONS:
                from strata_model import MODEL
                if model != MODEL:
                    raise InvalidUpdate('Citation reference packet requires the routed Strata model')
            envelope['model_interface'] = model_interface
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            run = self.run(run_id); session = run['session']; config = run['config']
            from calibration import assert_binding,EMPTY_HASH
            binding=assert_binding(self,session,config.get('calibration_binding',{'hash':EMPTY_HASH,'profiles':[]}))
            envelope['calibration_binding']=binding
            if role!='parser' and payload.get('contract',{}).get('calibration_binding',{'hash':EMPTY_HASH,'profiles':[]})!=binding:
                raise InvalidUpdate('Delivered calibration binding differs from committed run')
            if (role == 'parser') != (config.get('purpose') == 'parser'):
                raise InvalidUpdate('Role does not match run purpose')
            row = self.db.execute('SELECT session,revision,payload_json FROM deliveries WHERE id=?', (delivery_id,)).fetchone()
            if not row or row[0] != session or row[2] != canonical(payload):
                raise InvalidUpdate('Wrong session or payload delivery')
            state = self.snapshot(session)
            if row[1] != state['revision']: raise Conflict('Stale invocation revision')
            if role == 'parser':
                if sha(canonical([payload['CURRENT_USER_MESSAGE'], [], None])) != config['request_hash'] or payload['SOURCE_UNITS'] != source_units(payload['CURRENT_USER_MESSAGE']):
                    raise InvalidUpdate('Parser source does not match committed user message')
                if payload['PRIOR_STATE'] != self.packet(session, max_bytes=100_000):
                    raise InvalidUpdate('Parser prior state differs from host state')
            else:
                if payload['contract']['contract_hash'] != config['contract_hash']:
                    raise Conflict('Invocation contract changed')
                if role in ('memory_probe','reviewer','coder','architect','api_mapper','api_reviewer','static_explainer','chat_reviewer','review_reader','conversation','subagent','report_reviewer','document_extract','document_verdict_reviewer'):
                    if role == 'document_extract':
                        from document_stages import request_check
                        request_check(self, session, payload)
                        if payload['model'] != model:
                            raise InvalidUpdate('Dispatch model differs from document extraction route')
                    elif role == 'document_verdict_reviewer':
                        from document_verdict_review import request_check
                        request_check(self, session, payload)
                        if payload['reviewer_model'] != model:
                            raise InvalidUpdate('Dispatch model differs from document verdict review route')
                    elif role=='memory_probe':
                        from memory_probe import check
                        check(self,session,payload)
                        if payload['model']!=model:raise InvalidUpdate('Memory probe model changed')
                    elif role == 'review_reader':
                        from review_followup import check
                        check(self, session, payload)
                        if payload['planning_model'] != model:
                            raise InvalidUpdate('Architecture read planner differs from the selected model')
                    elif role=='report_reviewer':
                        from report_review import request_check
                        request_check(self,session,payload)
                        expected=payload['reviewer_selection']['reviewer_identity']
                        if expected!=model_identity or expected['name']!=model:
                            raise InvalidUpdate('Architecture reviewer identity differs from committed selection')
                    elif role=='subagent':
                        from subagent_runtime import request_check
                        request_check(self,session,payload)
                        if payload['route']['model']!=model:raise InvalidUpdate('Child model differs from host route')
                        if payload['memory_budget']!=envelope['budget_profile']:raise InvalidUpdate('Child context budget differs from provider')
                    elif role=='conversation':
                        from conversation import request_check
                        request_check(self,session,payload)
                        if payload['route']['model']!=model:raise InvalidUpdate('Dispatch model differs from conversation route')
                        if any(payload['memory_allocation'][k]!=v for k,v in envelope['budget_profile'].items()):
                            raise InvalidUpdate('Conversation allocation differs from provider budget')
                    elif role=='chat_reviewer':
                        from chat_runtime import request_check
                        request_check(self,session,payload)
                        if payload['route']['record']['choice']['model']!=model:raise InvalidUpdate('Dispatch model differs from committed route')
                        if payload.get('memory_budget')!=envelope['budget_profile']:raise InvalidUpdate('Review context budget differs from provider')
                    elif role=='static_explainer':
                        from static_role import request_check
                        request_check(self,session,payload)
                    elif role in ('api_mapper','api_reviewer'):
                        from api_roles import request_check
                        request_check(self,session,role,payload)
                    elif role == 'architect':
                        from architecture_role import request_check
                        request_check(self, session, payload)
                    else:
                        from review_role import check_request
                        check_request(self, session, payload)
                        if payload.get('native_route'):
                            from native_runtime import check_route
                            check_route(self,session,payload)
                            if payload['native_route']['record']['choice']['model']!=model:raise InvalidUpdate('Dispatch model differs from native route')
                    if sha(canonical([payload['request'], [], None])) != config['request_hash']:
                        raise InvalidUpdate('Review request differs from committed run')
            envelope.update(session=session, revision=row[1], epoch=config['epoch'],
                            contract_hash=config['contract_hash'], logic_hash=config['logic_hash'])
            from review_budget import require
            cost={'calls':1,'tokens':charge+envelope['budget_profile']['response_reserve'],'seconds':150}
            if state['constraints'].get('native_task'):cost.update(operations=1,source_bytes=sum(len(p.get('text','').encode()) for p in payload.get('pages',[])))
            envelope['task_budget_kernel']=require(self,session,cost)
            from orchestration import admit
            envelope['orchestration']=admit(self,session,'model',envelope['id'],cost,
                paths=payload.get('source_paths',[]),role=role,payload=payload,model=model,resource=resource)
            envelope['budget_kernel'] = self.reserve(run_id, 'DISPATCH', {'call_id': call_id, 'invocation_id': envelope['id'], 'role': role})
            raw = canonical(envelope); digest = sha('invocation-v1:' + raw)
            self.db.execute('INSERT INTO invocations VALUES(?,?,?,?,?,?,?)',
                (envelope['id'],session,run_id,call_id,delivery_id,raw,digest))
            self._audit('INVOCATION_DISPATCH', {'id': envelope['id'], 'hash': digest})
        return envelope

    def finalize(self, envelope, payload, proposal, transport_error=None, elapsed_seconds=None, token_usage=None,
                 accounting_settlement=None, failure_kind=None):
        session = envelope['session']; rid = envelope['delivery_id']
        model_proposal = copy.deepcopy(proposal)
        from task_observations import split, capture
        proposal, observation_fields = split(envelope['role'], proposal)
        host_fields = {}
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT record_json,digest FROM invocations WHERE id=?', (envelope['id'],)).fetchone()
            if not row or row[0] != canonical(envelope): raise InvalidUpdate('Invocation envelope changed')
            try:
                if transport_error: raise InvalidUpdate(transport_error)
                from calibration import assert_binding,EMPTY_HASH
                assert_binding(self,session,envelope.get('calibration_binding',{'hash':EMPTY_HASH,'profiles':[]}))
                run = self.run(envelope['run_id'])
                if any(e['kind'] in ('ALLOW','FIN','RESOURCE_WAIT') for e in run['events']):
                    raise InvalidUpdate('Run terminated during inference')
                if self.snapshot(session)['revision'] != envelope['revision']:
                    raise Conflict('Task revision changed during inference')
                if self.logic_hash != envelope['logic_hash']: raise Conflict('Validator changed during inference')
                for parent in run['config']['parents']: self.load_checkpoint(session, parent)
                for p in payload.get('pages', []):
                    page = self.read_page(session, p['id'], p['version'])
                    if page.content_hash != p['hash']: raise PageFault('Delivered page changed')
                if interface := envelope.get('model_interface'):
                    from model_packet import bind_output
                    proposal, host_fields = bind_output(envelope['role'], payload, envelope['schema'], proposal,
                                                        interface['version'])
                elif envelope['role'] == 'conversation' and payload.get('response_contract') == 2:
                    from conversation import bind_output
                    proposal = bind_output(payload, proposal)
                    host_fields = {'context_binding': payload['context_binding']}
                result = validate_result(self, session, envelope['role'], payload, envelope['schema'], proposal, rid)
                if result['admission']['admitted'] and envelope['role'] == 'parser':
                    self.apply_user_patch(session, 'invocation-' + envelope['id'], payload['CURRENT_USER_MESSAGE'],
                                          result['operations'], envelope['revision'])
            except (ValueError, TypeError, KeyError, InvalidUpdate, Conflict, PageFault) as exc:
                result = {'admission': {'admitted': False, 'errors': [str(exc)]}, 'checks': []}
            admitted = result['admission']['admitted']
            previous = self.db.execute('SELECT seq,id FROM invocation_receipts WHERE session=? ORDER BY seq DESC LIMIT 1', (session,)).fetchone()
            seq, prev = previous if previous else (0, 'GENESIS')
            receipt = {'session': session, 'seq': seq + 1, 'invocation_id': envelope['id'],
                       'elapsed_seconds':elapsed_seconds if elapsed_seconds is not None else 150,
                       'token_usage': token_usage or {'source':'unavailable','input_tokens':None,'output_tokens':None},
                       'invocation_hash': row[1], 'role': envelope['role'],
                       'calibration_binding':envelope.get('calibration_binding'),
                       'contract_hash': envelope['contract_hash'], 'epoch': envelope['epoch'],
                       'logic_hash': envelope['logic_hash'], 'payload_hash': envelope['payload_hash'],
                       'response_hash': sha(canonical(proposal)), 'proposal': proposal,
                       'admission': result['admission'], 'checks': result['checks'],
                       'status': 'accepted' if admitted else 'quarantined', 'previous_hash': prev,
                       'resulting_contract_hash': self.origin(session, bootstrap=True)['contract_hash']}
            if failure_kind is not None:
                receipt['failure_kind'] = failure_kind
            observation = capture(self, envelope, payload, proposal, observation_fields)
            if observation is not None:
                receipt['task_observation'] = observation
            if observation_fields:
                receipt['observation_fields'] = observation_fields
            if envelope.get('model_interface') or observation_fields or (envelope['role'] == 'conversation' and payload.get('response_contract') == 2):
                receipt.update(model_response_hash=sha(canonical(model_proposal)), host_fields=host_fields)
            if envelope.get('model_interface'):
                from budget_accounting import used
                receipt['accounting_settlement'] = accounting_settlement
                used(envelope, receipt)
                receipt['host_fields_authority'] = 'host_binding_not_model_acknowledgement'
            raw = canonical(receipt); digest = sha('invocation-receipt-v1:' + raw)
            self.db.execute('INSERT INTO invocation_receipts VALUES(?,?,?,?,?)', (session,seq+1,envelope['id'],digest,raw))
            self._audit('INVOCATION_RECEIPT', {'id': digest, 'record': receipt})
            from orchestration import settle
            settle(self,envelope.get('orchestration'),digest,receipt['elapsed_seconds'])
        if envelope['role'] == 'parser' and admitted: self.current_contract(session)
        result.update(invocation_id=envelope['id'], invocation_receipt_id=digest,
                      accepted_output=copy.deepcopy(proposal) if admitted else None,
                      model_proposal=model_proposal)
        if observation is not None:
            result['task_observation'] = observation
        return result

    def quarantine_invocation(self, session, invocation_id, reason):
        if not reason or not self.db.execute('SELECT 1 FROM invocations WHERE id=? AND session=?', (invocation_id,session)).fetchone():
            raise InvalidUpdate('Invalid invocation quarantine')
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO invocation_quarantine VALUES(?,?,?)', (invocation_id,reason,time.time()))
            self._audit('INVOCATION_QUARANTINE', {'id': invocation_id, 'reason': reason})

    def load_result(self, session, invocation_id):
        row = self.db.execute('SELECT record_json FROM invocation_receipts WHERE session=? AND invocation_id=?', (session,invocation_id)).fetchone()
        if not row: raise PageFault('Unknown, wrong-session or pending invocation')
        receipt = json.loads(row[0])
        if receipt['status'] != 'accepted' or self.db.execute('SELECT 1 FROM invocation_quarantine WHERE invocation_id=?', (invocation_id,)).fetchone():
            raise PageFault('Invocation result quarantined')
        if receipt['resulting_contract_hash'] != self.origin(session, bootstrap=True)['contract_hash'] or receipt['logic_hash'] != self.logic_hash:
            raise PageFault('Invocation result has stale contract or validator')
        env = json.loads(self.db.execute('SELECT record_json FROM invocations WHERE id=?', (invocation_id,)).fetchone()[0])
        from calibration import assert_binding,EMPTY_HASH
        try:assert_binding(self,session,env.get('calibration_binding',{'hash':EMPTY_HASH,'profiles':[]}))
        except InvalidUpdate as exc:raise PageFault(str(exc)) from None
        if env['role'] == 'worker':
            assertions = {row[0] for row in self.db.execute(
                'SELECT id FROM assertion_receipts WHERE session=? AND delivery_id=?', (session,env['delivery_id']))}
            checkpoints = [cid for cid,raw in self.db.execute(
                'SELECT id,record_json FROM checkpoints WHERE session=? AND run_id=?', (session,env['run_id']))
                if assertions.intersection(json.loads(raw)['receipt_ids'])]
            if not checkpoints: raise PageFault('Worker checkpoint not committed')
            if any(self.checkpoint_status(session,cid) != 'available' for cid in checkpoints):
                raise PageFault('Worker checkpoint or ancestor quarantined')
        if env['role'] != 'parser' and self.snapshot(session)['revision'] != env['revision']:
            raise PageFault('Invocation result has stale task state')
        payload = json.loads(self.db.execute('SELECT payload_json FROM deliveries WHERE id=?', (env['delivery_id'],)).fetchone()[0])
        for p in payload.get('pages', []):
            page = self.read_page(session, p['id'], p['version'])
            if page.content_hash != p['hash']: raise PageFault('Invocation result has changed sources')
        if payload.get('conversation_handoff'):
            from conversation import check_workspace_handoff
            check_workspace_handoff(self,session,payload)
        if env['role'] in ('reviewer','coder','architect'):
            for parent in payload.get('derived_from', []): self.load_result(session, parent)
        if env['role'] == 'architect':
            from repository_tools import validate_evidence
            validate_evidence(self, session, payload['workspace'], payload['evidence'])
        if env['role']=='static_explainer':
            from static_role import request_check
            request_check(self,session,payload)
        if env['role']=='conversation' and payload.get('memory'):
            from conversation_memory import history
            history(self,session,payload['memory']['invocation_id'])
        if env['role']=='conversation' and payload.get('memory_selection'):
            self.load_result(session,payload['memory_selection'])
        if env['role']=='conversation' and payload.get('routing_state',{}).get('previous'):
            self.load_result(session,payload['routing_state']['previous'])
        if env['role']=='chat_reviewer':
            from chat_runtime import request_check
            request_check(self,session,payload)
        if env['role']=='report_reviewer':
            from report_review import request_check
            request_check(self,session,payload)
        if env['role']=='subagent':
            from subagent_runtime import request_check
            request_check(self,session,payload)
        if env['role'] in ('api_mapper','api_reviewer'):
            from api_roles import check_sources
            check_sources(self,session,payload)
            if env['role']=='api_reviewer':self.load_result(session,payload['candidate_id'])
        return receipt['proposal']  # Derived context only; never source authority.

    def verify_invocations(self, session, complete=True):
        if not self.verify_protocol(session): return False
        previous = 'GENESIS'; seq = 0
        for n, iid, digest, raw in self.db.execute('SELECT seq,invocation_id,id,record_json FROM invocation_receipts WHERE session=? ORDER BY seq', (session,)):
            r = json.loads(raw); seq += 1
            env = self.db.execute('SELECT record_json,digest,delivery_id FROM invocations WHERE id=? AND session=?', (iid,session)).fetchone()
            if not env or sha('invocation-v1:' + env[0]) != env[1] or r['invocation_hash'] != env[1]: return False
            if n != seq or r['previous_hash'] != previous or sha('invocation-receipt-v1:' + raw) != digest: return False
            if sha(canonical(r['proposal'])) != r['response_hash']: return False
            if 'model_response_hash' in r:
                original = copy.deepcopy(r['proposal'])
                fields = r.get('host_fields', {})
                if fields:
                    delivered = json.loads(self.db.execute('SELECT payload_json FROM deliveries WHERE id=?', (env[2],)).fetchone()[0])
                    envelope = json.loads(env[0])
                    if envelope.get('model_interface'):
                        from model_packet import host_fields
                        try:
                            expected = host_fields(r['role'], delivered, envelope['schema'],
                                                   envelope['model_interface']['version'], original)
                        except (KeyError, TypeError, AttributeError):
                            return False
                        if fields != expected: return False
                    elif r['role'] != 'conversation' or fields != {'context_binding': delivered['context_binding']}: return False
                    if not isinstance(original, dict) or any(original.get(k) != v for k, v in fields.items()): return False
                    for key in fields: original.pop(key)
                    from model_packet import REFERENCE_VERSIONS, recover_citation_references
                    if (envelope.get('model_interface') or {}).get('version') in REFERENCE_VERSIONS:
                        try:
                            original['citations'] = recover_citation_references(envelope['schema'], original['citations'])
                        except (KeyError, TypeError, ValueError):
                            return False
                observed = r.get('observation_fields', {})
                if observed:
                    from task_observations import FIELD, ROLES
                    if r['role'] not in ROLES or set(observed) != {FIELD} or not isinstance(original, dict) or FIELD in original: return False
                    original.update(observed)
                if sha(canonical(original)) != r['model_response_hash']: return False
            if json.loads(env[0]).get('model_interface'):
                from budget_accounting import used
                try: used(json.loads(env[0]), r)
                except (InvalidUpdate, KeyError, TypeError): return False
            payload = self.db.execute('SELECT payload_hash FROM deliveries WHERE id=?', (env[2],)).fetchone()
            if not payload or payload[0] != r['payload_hash']: return False
            previous = digest
        count = self.db.execute('SELECT COUNT(*) FROM invocations WHERE session=?', (session,)).fetchone()[0]
        return not complete or count == seq


class InvocationGateway:
    def __init__(self, db, run_id, provider):
        self.db, self.run_id, self.provider = db, run_id, provider

    def invoke(self, role, call_id, system, payload, schema, delivery_id):
        from local_model import LocalModel
        from pathlib import Path
        from reasoning_policy import check_provider
        check_provider(self.provider, payload, fresh=isinstance(self.provider, LocalModel))
        if isinstance(self.provider, LocalModel):
            with self.provider.dispatch_gate(Path(self.db).parent,call_id,system,payload,schema) as plan:
                self.provider.memory_plan=plan
                return self._invoke(role,call_id,system,payload,schema,delivery_id)
        return self._invoke(role,call_id,system,payload,schema,delivery_id)

    def _invoke(self, role, call_id, system, payload, schema, delivery_id):
        from lifecycle import timeout
        timeout(150)
        from storage_policy import require_space
        from pathlib import Path
        require_space(Path(self.db).parent)
        # Copy to prevent provider-side mutations of the committed request.
        payload, schema = copy.deepcopy(payload), copy.deepcopy(schema)
        name = getattr(self.provider, 'model', 'test-adapter')
        ledger = InvocationLedger(self.db)
        try:
            charge = self.provider.budget.check(system, payload, schema)
            from local_model import LocalModel
            from monitoring import require_resources
            resource=require_resources(Path(self.db).parent) if isinstance(self.provider,LocalModel) else {'profile':'injected-test-provider'}
            if getattr(self.provider,'memory_plan',None):resource=dict(resource,runtime_memory=self.provider.memory_plan)
            from model_identity import observe, unknown
            identity=observe(name) if isinstance(self.provider,LocalModel) else getattr(self.provider,'model_identity',unknown(name))
            expected=getattr(self.provider,'expected_identity',None)
            if expected is not None and identity!=expected: raise InvalidUpdate('Selected model identity changed before dispatch')
            self.provider.dispatch_identity=identity
            from model_packet import build, descriptor
            packet = build(system, payload, schema) if getattr(self.provider, 'model_interface', None) == 1 else None
            interface = descriptor(packet, self.provider.budget.template_reserve) if packet else None
            envelope = ledger.register(self.run_id, call_id, role, payload, system, schema, delivery_id, name, charge, budget={'context_limit':self.provider.budget.context_limit,'response_reserve':self.provider.budget.response_reserve,'template_reserve':self.provider.budget.template_reserve},resource=resource,model_identity=identity,model_interface=interface)
        finally: ledger.close()
        permit = {'hash': envelope['request_hash'], 'used': False, 'packet_hash': (envelope.get('model_interface') or {}).get('packet_hash')}
        token = _permit.set(permit); error = None; proposal = None; record = None;started=time.monotonic()
        failure_kind = None
        repairable = False
        from model_activity import scope
        try:
            with scope(envelope) as activity:
                proposal, record = self.provider.call(call_id, system, copy.deepcopy(payload), copy.deepcopy(schema))
                if isinstance(self.provider,LocalModel) and observe(name)!=identity:
                    raise InvalidUpdate('Model identity changed during inference')
        except Exception as exc:
            from local_model import OutputLimitExceeded, StructuredOutputRejected
            if isinstance(exc, OutputLimitExceeded):
                failure_kind = 'output_limit'
            elif isinstance(exc, (TimeoutError, socket.timeout)):
                failure_kind = 'timeout'
            repairable = isinstance(exc, StructuredOutputRejected)
            error = type(exc).__name__ + ': ' + str(exc)
            record = getattr(exc, 'call_record', None) or record or {'call_id': call_id, 'ok': False, 'error': error}
        finally: _permit.reset(token)
        ledger = InvocationLedger(self.db)
        try:
            if not error: ledger.confirm_delivery(delivery_id)
            from token_usage import measured, history
            from budget_accounting import settlement
            result = ledger.finalize(envelope, payload, proposal, error, time.monotonic() - started,
                                     measured(record), settlement(envelope, record), failure_kind)
            activity.finish(result['admission']['admitted'], measured(record))
            result['token_usage'] = history(ledger.db, run_id=self.run_id)
            if error and not repairable: result['blocked'] = error
            if failure_kind is not None: result['failure_kind'] = failure_kind
            result['record'] = record
            return result
        finally: ledger.close()

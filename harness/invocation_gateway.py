"""Single invocation boundary. Durable dispatch, role checks, receipts, quarantine.

The dispatch permit prevents accidental transport bypass in trusted host code;
it is not a Python sandbox or protection against a privileged process.
"""
import copy
from contextvars import ContextVar
import json
import socket
import sqlite3
import time
import uuid
from context_manager import canonical, sha, InvalidUpdate, Conflict
from governance import GovernedContext
from paging import PageFault
from invocation_roles import expected_request, validate_result
from legacy_model import source_units
from contextlib import nullcontext

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


def transport_seconds(default=150):
    permit = _permit.get()
    return permit.get('transport_seconds', default) if permit else default


def transport_timeout(started, default=150):
    from lifecycle import timeout
    permit = _permit.get()
    remaining = transport_seconds(default)-(time.monotonic()-started)
    if permit and permit.get('transport_deadline') is not None:
        remaining = min(remaining, permit['transport_deadline']-time.monotonic())
    if remaining <= 0: raise TimeoutError('Model transport deadline reached')
    return timeout(remaining)


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
          CREATE INDEX IF NOT EXISTS receipt_invocations_session
            ON invocations(session,id);
        ''')
        for table in ('invocations','invocation_receipts','invocation_quarantine'):
            for op in ('UPDATE','DELETE'):
                self.db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{op} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'immutable invocation record'); END")
        self.db.commit()

    def register(self, run_id, call_id, role, payload, system, schema, delivery_id, model, charge, budget=None, resource=None, model_identity=None, model_interface=None, context_measurement=None, transport_timing=None):
        expected_system, expected_schema = expected_request(role, payload)
        if system != expected_system or schema != expected_schema:
            raise InvalidUpdate('Role system/schema mismatch')
        envelope = {'id': uuid.uuid4().hex, 'run_id': run_id, 'call_id': call_id,
                    'role': role, 'model': model, 'delivery_id': delivery_id,
                    'system_hash': sha(system), 'schema': schema, 'payload_hash': sha(canonical(payload)),
                    'request_hash': request_hash(call_id, model, system, payload, schema),
                    'model_identity': model_identity, 'charge': charge, 'budget_profile':budget or {'context_limit':8192,'response_reserve':1024,'template_reserve':256}, 'created_at': time.time()}
        from paging import Budget
        from context_window import exact, validate_proof
        allocation = Budget(**envelope['budget_profile'])
        from invocation_timing import validate as validate_timing
        call_seconds = validate_timing(transport_timing, role, payload, allocation.response_reserve)
        if transport_timing is not None: envelope['transport_timing'] = copy.deepcopy(transport_timing)
        if exact(allocation):
            from strata_model import is_strata
            from remote_profiles import is_remote
            window = allocation.context_window
            if ((window['provider'] == 'strata' and not is_strata(model))
                    or (window['provider'] == 'sutra_remote'
                        and (not is_remote(model) or model != window['reference']))):
                raise InvalidUpdate('Exact physical context requires its matching routed provider')
            validate_proof(context_measurement, allocation, system, payload, schema, model_identity)
            envelope['physical_context'] = copy.deepcopy(context_measurement)
        elif context_measurement is not None:
            raise InvalidUpdate('Legacy invocation cannot carry an exact context proof')
        if model_interface is not None:
            from model_packet import REFERENCE_VERSIONS, build, descriptor
            packet = build(system, payload, schema)
            if packet is None or model_interface != descriptor(packet, envelope['budget_profile']['template_reserve']):
                raise InvalidUpdate('Model interface does not match committed request')
            if model_interface['role'] != role or charge != model_interface['input_reservation']:
                raise InvalidUpdate('Model packet charge or role changed')
            if model_interface['version'] in REFERENCE_VERSIONS:
                from strata_model import is_strata
                from engine_profiles import is_engine
                from remote_profiles import is_remote
                if not is_strata(model) and not is_engine(model) and not is_remote(model):
                    raise InvalidUpdate('Citation reference packet requires the routed native model')
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
                        from chat_runtime import check_parent_output_dispatch, request_check
                        request_check(self,session,payload)
                        if payload['route']['record']['choice']['model']!=model:raise InvalidUpdate('Dispatch model differs from committed route')
                        if payload.get('memory_budget')!=envelope['budget_profile']:raise InvalidUpdate('Review context budget differs from provider')
                        if 'parent_draft_recovery' in payload:
                            check_parent_output_dispatch(self, session, payload, model, model_identity)
                    elif role=='static_explainer':
                        from static_role import request_check
                        request_check(self,session,payload)
                    elif role in ('api_mapper','api_reviewer'):
                        from api_roles import request_check
                        request_check(self,session,role,payload)
                    elif role == 'architect':
                        from architecture_role import request_check
                        request_check(self, session, payload)
                        if payload.get('evidence_discovery_version') == 4 and payload.get('memory_budget') != envelope['budget_profile']:
                            raise InvalidUpdate('Discovery context budget differs from provider')
                        prefix = (context_measurement or {}).get('prefix_reuse')
                        if prefix is not None:
                            from remote_prefix import binding, isolated
                            window = allocation.context_window
                            memory = (resource or {}).get('runtime_memory') or {}
                            sample = memory.get('hardware', {}).get('sample') or {}
                            if (not isolated(sample, window)
                                    or prefix['scope_sha256'] != binding(self, run_id, window, memory.get('instance'))):
                                raise InvalidUpdate('Remote review prefix scope changed before dispatch')
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
            cost={'calls':1,'tokens':charge+envelope['budget_profile']['response_reserve'],'seconds':call_seconds}
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
                 accounting_settlement=None, failure_kind=None, generation_progress=None, transport_failure=None):
        session = envelope['session']; rid = envelope['delivery_id']
        model_proposal = copy.deepcopy(proposal)
        from task_observations import split, capture
        proposal, observation_fields = split(envelope['role'], proposal)
        host_fields = {}
        reference_sets = None
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
                    from model_packet import bind_output, evidence_set_changes
                    proposal, host_fields = bind_output(envelope['role'], payload, envelope['schema'], proposal,
                                                        interface['version'])
                    reference_sets = evidence_set_changes(envelope['role'], payload, model_proposal, proposal)
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
            from invocation_timing import reserved_seconds
            receipt = {'session': session, 'seq': seq + 1, 'invocation_id': envelope['id'],
                       'elapsed_seconds':elapsed_seconds if elapsed_seconds is not None else reserved_seconds(envelope),
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
            from local_model import transport_observation
            transport_failure = (transport_observation({'transport':transport_failure}) if transport_error else None)
            if transport_failure is not None:
                receipt['transport_failure'] = transport_failure
            observation = capture(self, envelope, payload, proposal, observation_fields)
            if observation is not None:
                receipt['task_observation'] = observation
            # Coherence is bounded diagnostic work over this invocation's
            # explicit artifacts. It cannot change the result above, start an
            # inference/repair, or prevent settlement if its own check fails.
            # Intentional lifecycle cancellation inherits BaseException and
            # remains outside these ordinary diagnostic exception handlers.
            coherence = None
            coherence_started = time.perf_counter_ns()
            try:
                from coherence_observations import capture as capture_coherence
                coherence = capture_coherence(self, envelope, payload, proposal, observation)
                if coherence is not None:
                    from karma_adapter import KarmaAdapter
                    coherence['kernel'] = KarmaAdapter().coherence(coherence['signals'])
                    coherence['gateway_status'] = receipt['status']
                    coherence['model'] = str(envelope['model'])[:160]
            except Exception:
                coherence = {'format': 1, 'profile': 'stage-text-coherence-v1',
                             'mode': 'observe_only', 'status': 'unavailable',
                             'invocation_id': envelope['id'], 'role': envelope['role'],
                             'model': envelope['model'], 'session': session,
                             'gateway_status': receipt['status'],
                             'issues': [{'code': 'coherence_unavailable', 'kind': 'structural',
                                         'detail': 'The bounded coherence check is unavailable. Admission and accounting are unchanged.'}],
                             'analysis_limits': {'free_text_coherence': 'unassessed',
                                                 'source_entailment': 'unverified'}}
            if coherence is not None:
                coherence['elapsed_ns'] = time.perf_counter_ns() - coherence_started
                receipt['coherence_observation'] = coherence
                try:
                    from accounting_metrics import record as record_timing
                    record_timing(self, 'text_coherence', coherence['elapsed_ns'],
                                  status=coherence['status'], role=envelope['role'])
                except Exception:
                    pass  # A diagnostic timing subscriber cannot strand work.
            progress_observation = None
            if generation_progress is not None:
                try:
                    from generation_progress import validate_snapshot
                    from karma_adapter import KarmaAdapter
                    progress_observation = {'mode': 'observe_only',
                        'invocation_id': envelope['id'], 'session': session,
                        'snapshot': validate_snapshot(generation_progress),
                        'kernel': KarmaAdapter().generation_progress(generation_progress)}
                    receipt['generation_progress_observation'] = progress_observation
                except Exception:
                    pass  # A malformed optional stream counter is not a grant or admission failure.
            if envelope['role'] == 'subagent':
                try:
                    from child_checkpoint_progress import capture as capture_checkpoint_progress
                    checkpoint_progress = capture_checkpoint_progress(envelope, payload, receipt,
                                                                      elapsed_seconds=elapsed_seconds)
                    if checkpoint_progress is not None:
                        receipt['child_checkpoint_progress'] = checkpoint_progress
                except Exception:
                    pass  # An optional observation cannot strand the invocation.
            if observation_fields:
                receipt['observation_fields'] = observation_fields
            if reference_sets:
                receipt['evidence_reference_sets'] = reference_sets
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
        if coherence is not None:
            result['coherence_observation'] = coherence
        if progress_observation is not None:
            result['generation_progress_observation'] = progress_observation
        if transport_failure is not None:
            result['transport_failure'] = transport_failure
        return result

    def quarantine_invocation(self, session, invocation_id, reason):
        if not reason or not self.db.execute('SELECT 1 FROM invocations WHERE id=? AND session=?', (invocation_id,session)).fetchone():
            raise InvalidUpdate('Invalid invocation quarantine')
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO invocation_quarantine VALUES(?,?,?)', (invocation_id,reason,time.time()))
            self._audit('INVOCATION_QUARANTINE', {'id': invocation_id, 'reason': reason})

    def _result_batch_stamp(self):
        return (self._audit_identity(), self.logic_hash, self.db.total_changes,
                self.db.execute('PRAGMA data_version').fetchone()[0],
                self.db.execute('PRAGMA schema_version').fetchone()[0],
                getattr(self, '_audit_guard_epoch', None), self.db.in_transaction)

    def load_architect_results(self, session, invocation_ids):
        """Verify one ancestry DAG; never retain success across this read batch.

        Live dependencies are checked again before returning any result. A
        caller-owned transaction keeps its existing snapshot and ownership;
        this method never begins, commits or rolls it back.
        """
        invocation_ids = tuple(invocation_ids)
        if not invocation_ids: return []
        before = self._result_batch_stamp()
        batch = {'owner': self, 'session': session, 'done': {}, 'visiting': set()}
        results = [self.load_result(session, iid, _architect_batch=batch) for iid in invocation_ids]
        self._architect_freshness(session, batch['done'].values())
        if self._result_batch_stamp() != before:
            raise PageFault('Architect result verification changed during its read batch')
        return results

    def _architect_freshness(self, session, checked):
        """Recheck unique live dependencies, not the ancestry traversal."""
        from calibration import assert_binding, EMPTY_HASH
        from review_upgrade import result_policy
        contract = self.origin(session, bootstrap=True)['contract_hash']
        revision = self.snapshot(session)['revision']
        calibrations, pages, handoffs, sources = {}, {}, {}, {}
        for receipt, env, payload in checked:
            if self.db.execute('SELECT 1 FROM invocation_quarantine WHERE invocation_id=?', (env['id'],)).fetchone():
                raise PageFault('Invocation result quarantined')
            if receipt['resulting_contract_hash'] != contract or not result_policy(self, session, receipt, env):
                raise PageFault('Invocation result has stale contract or validator')
            if revision != env['revision']: raise PageFault('Invocation result has stale task state')
            expected = env.get('calibration_binding', {'hash': EMPTY_HASH, 'profiles': []})
            calibrations[canonical(expected)] = expected
            for page_ref in payload.get('pages', []): pages[canonical(page_ref)] = page_ref
            if payload.get('conversation_handoff'):
                key = canonical([payload['conversation_handoff'], payload['request']])
                handoffs[key] = payload
            evidence = sources.setdefault(payload['workspace'], {})
            for entry in payload['evidence']: evidence[canonical(entry)] = entry
        for page_ref in pages.values():
            page = self.read_page(session, page_ref['id'], page_ref['version'])
            if page.content_hash != page_ref['hash']: raise PageFault('Invocation result has changed sources')
        from conversation import check_workspace_handoff
        for payload in handoffs.values():
            check_workspace_handoff(self, session, payload)
        from repository_tools import validate_evidence
        for workspace, evidence in sources.items():
            validate_evidence(self, session, workspace, list(evidence.values()))
        for expected in calibrations.values():
            try: assert_binding(self, session, expected)
            except InvalidUpdate as exc: raise PageFault(str(exc)) from None

    def load_result(self, session, invocation_id, *, _architect_batch=None):
        if _architect_batch is None:
            return self._load_result(session, invocation_id)
        batch = _architect_batch
        if batch['owner'] is not self or batch['session'] != session:
            raise PageFault('Architect result batch belongs to another ledger or session')
        if invocation_id in batch['done']:
            return copy.deepcopy(batch['done'][invocation_id][0]['proposal'])
        if invocation_id in batch['visiting']:
            raise PageFault('Architect result ancestry contains a cycle')
        batch['visiting'].add(invocation_id)
        try:
            return self._load_result(session, invocation_id, _architect_batch=batch)
        finally:
            batch['visiting'].remove(invocation_id)

    def _load_result(self, session, invocation_id, _architect_batch=None):
        row = self.db.execute(
            'SELECT r.record_json,r.id,i.record_json,i.digest FROM invocation_receipts r'
            ' JOIN invocations i ON i.id=r.invocation_id AND i.session=r.session'
            ' WHERE r.session=? AND r.invocation_id=?', (session, invocation_id)).fetchone()
        if not row: raise PageFault('Unknown, wrong-session or pending invocation')
        receipt_raw, receipt_hash, envelope_raw, envelope_hash = row
        receipt, env = json.loads(receipt_raw), json.loads(envelope_raw)
        if receipt['status'] != 'accepted' or self.db.execute('SELECT 1 FROM invocation_quarantine WHERE invocation_id=?', (invocation_id,)).fetchone():
            raise PageFault('Invocation result quarantined')
        from review_upgrade import result_policy
        if (receipt['resulting_contract_hash'] != self.origin(session, bootstrap=True)['contract_hash']
                or not result_policy(self, session, receipt, env)):
            raise PageFault('Invocation result has stale contract or validator')
        historical = receipt['logic_hash'] != self.logic_hash
        if historical and (sha('invocation-receipt-v1:' + receipt_raw) != receipt_hash
                           or sha('invocation-v1:' + envelope_raw) != envelope_hash
                           or receipt['invocation_hash'] != envelope_hash
                           or env['id'] != invocation_id or receipt['invocation_id'] != invocation_id
                           or sha(canonical(receipt['proposal'])) != receipt['response_hash']):
            raise PageFault('Historical invocation receipt or envelope corrupted')
        if _architect_batch is not None and env['role'] != 'architect':
            raise PageFault('Architect result batch requires architect ancestry')
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
        payload_row = self.db.execute(
            'SELECT payload_json FROM deliveries WHERE id=? AND session=?', (env['delivery_id'], session)).fetchone()
        if not payload_row or historical and not (sha(payload_row[0]) == env['payload_hash'] == receipt['payload_hash']):
            raise PageFault('Invocation result has missing or changed delivery')
        payload = json.loads(payload_row[0])
        from invocation_timing import validate as validate_timing
        validate_timing(env.get('transport_timing'), env['role'], payload, env['budget_profile']['response_reserve'])
        if env.get('budget_profile', {}).get('context_profile') == 2:
            from paging import Budget
            from context_window import validate_proof
            system, _ = expected_request(env['role'], payload)
            validate_proof(env.get('physical_context'), Budget(**env['budget_profile']), system, payload,
                           env['schema'], env.get('model_identity'))
        for p in payload.get('pages', []):
            page = self.read_page(session, p['id'], p['version'])
            if page.content_hash != p['hash']: raise PageFault('Invocation result has changed sources')
        if payload.get('conversation_handoff'):
            from conversation import check_workspace_handoff
            check_workspace_handoff(self,session,payload)
        if env['role'] in ('reviewer','coder','architect'):
            for parent in payload.get('derived_from', []):
                if _architect_batch is None: self.load_result(session, parent)
                else: self.load_result(session, parent, _architect_batch=_architect_batch)
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
        if _architect_batch is not None:
            _architect_batch['done'][invocation_id] = (copy.deepcopy(receipt), env, payload)
        return receipt['proposal']  # Derived context only; never source authority.

    def verify_invocations(self, session, complete=True):
        from accounting_metrics import record
        started = time.perf_counter_ns()
        stats = {'receipt_rows': 0, 'invocation_count': 0}
        owns_transaction = not self.db.in_transaction
        ok = False
        try:
            if owns_transaction:
                self.db.execute('BEGIN')
            ok = self._verify_invocations(session, complete, stats)
            return ok
        except (KeyError, TypeError, ValueError, sqlite3.DatabaseError):
            return False
        finally:
            if owns_transaction and self.db.in_transaction:
                self.db.execute('ROLLBACK')
            record(self, 'invocation_verification', time.perf_counter_ns() - started,
                   session=session, ok=ok, complete=complete, **stats)

    def _verify_invocations(self, session, complete, stats):
        if not self.verify_protocol(session): return False
        previous = 'GENESIS'; seq = 0
        for n, iid, digest, raw in self.db.execute('SELECT seq,invocation_id,id,record_json FROM invocation_receipts WHERE session=? ORDER BY seq', (session,)):
            stats['receipt_rows'] += 1
            r = json.loads(raw); seq += 1
            env = self.db.execute('SELECT record_json,digest,delivery_id FROM invocations WHERE id=? AND session=?', (iid,session)).fetchone()
            if not env or sha('invocation-v1:' + env[0]) != env[1] or r['invocation_hash'] != env[1]: return False
            envelope = json.loads(env[0])
            if envelope.get('transport_timing') is not None:
                from invocation_timing import validate as validate_timing
                delivered = json.loads(self.db.execute('SELECT payload_json FROM deliveries WHERE id=?', (env[2],)).fetchone()[0])
                try:
                    validate_timing(envelope['transport_timing'], envelope['role'], delivered, envelope['budget_profile']['response_reserve'])
                except (InvalidUpdate, KeyError, TypeError, ValueError):
                    return False
            if envelope.get('budget_profile', {}).get('context_profile') == 2:
                from paging import Budget
                from context_window import validate_proof
                delivered = json.loads(self.db.execute('SELECT payload_json FROM deliveries WHERE id=?', (env[2],)).fetchone()[0])
                try:
                    system, _ = expected_request(envelope['role'], delivered)
                    validate_proof(envelope.get('physical_context'), Budget(**envelope['budget_profile']), system,
                                   delivered, envelope['schema'], envelope.get('model_identity'))
                except (InvalidUpdate, KeyError, TypeError, ValueError):
                    return False
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
                    from model_packet import RETAINED_ARCHITECT_VERSION, recover_architect_references
                    if (envelope.get('model_interface') or {}).get('version') == RETAINED_ARCHITECT_VERSION:
                        try:
                            original['findings'] = recover_architect_references(envelope['schema'], original['findings'])
                        except (KeyError, TypeError, ValueError):
                            return False
                    from model_packet import REVIEW_JUDGMENT_VERSION, recover_judgment_choices
                    if (envelope.get('model_interface') or {}).get('version') == REVIEW_JUDGMENT_VERSION:
                        try:
                            original = recover_judgment_choices(original)
                        except (InvalidUpdate, KeyError, TypeError, ValueError):
                            return False
                if r.get('evidence_reference_sets') is not None:
                    from model_packet import restore_evidence_sets
                    delivered = json.loads(self.db.execute('SELECT payload_json FROM deliveries WHERE id=?', (env[2],)).fetchone()[0])
                    envelope = json.loads(env[0])
                    try:
                        original = restore_evidence_sets(r['role'], delivered, envelope['schema'], original,
                            r['evidence_reference_sets'], envelope['model_interface']['version'])
                    except (InvalidUpdate, ValueError, TypeError, KeyError):
                        return False
                observed = r.get('observation_fields', {})
                if observed:
                    from task_observations import restore
                    try:
                        original = restore(r['role'], original, observed)
                    except (InvalidUpdate, ValueError, TypeError, KeyError):
                        return False
                if sha(canonical(original)) != r['model_response_hash']: return False
            if json.loads(env[0]).get('model_interface'):
                from budget_accounting import used
                try: used(json.loads(env[0]), r)
                except (InvalidUpdate, KeyError, TypeError): return False
            payload = self.db.execute('SELECT payload_hash FROM deliveries WHERE id=?', (env[2],)).fetchone()
            if not payload or payload[0] != r['payload_hash']: return False
            previous = digest
        count = self.db.execute('SELECT COUNT(*) FROM invocations WHERE session=?', (session,)).fetchone()[0]
        stats['invocation_count'] = count
        return not complete or count == seq


class InvocationGateway:
    def __init__(self, db, run_id, provider, *, ledger=None):
        self.db, self.run_id, self.provider = db, run_id, provider
        if ledger is not None and not isinstance(ledger, InvocationLedger):
            raise InvalidUpdate('Gateway borrowing requires an InvocationLedger')
        self._borrowed = ledger
        self._borrow_proof = None

    def _scope_record(self, ledger, nid, session):
        row = ledger.db.execute('SELECT record_json,digest FROM orchestration_nodes WHERE id=?',
                                (nid,)).fetchone()
        if (not row or sha('orchestration-node-v1:' + row[0]) != row[1]
                or json.loads(row[0])['session'] != session):
            raise InvalidUpdate('Borrowed gateway orchestration scope differs')
        return tuple(row)

    def _borrow_state(self):
        from pathlib import Path
        from governance import policy_hash
        from orchestration import ACTIVE
        ledger = self._borrowed
        try:
            if ledger.db.in_transaction:
                raise InvalidUpdate('Borrowed gateway requires no outstanding caller transaction')
            path = Path(self.db).resolve()
            if str(ledger.path) == ':memory:' or path != Path(ledger.path).resolve():
                raise InvalidUpdate('Borrowed gateway database path differs')
            identity = ledger._audit_identity()
            if identity != ledger._audit_file_identity:
                raise InvalidUpdate('Borrowed gateway database identity changed')
            if ledger.logic_hash != policy_hash():
                raise InvalidUpdate('Borrowed gateway policy changed')
            run = ledger.db.execute('SELECT session,config_json FROM protocol_runs WHERE id=?',
                                    (self.run_id,)).fetchone()
            if not run:
                raise InvalidUpdate('Borrowed gateway run is unavailable')
            active = ACTIVE.get()
            audit_prefix = getattr(ledger, '_accounting_audit_prefix_enabled', False)
            if type(audit_prefix) is not bool:
                raise InvalidUpdate('Borrowed gateway audit profile is invalid')
            return {'path': str(path), 'identity': identity, 'logic_hash': ledger.logic_hash,
                    'run': tuple(run), 'active': active,
                    'accounting_anchor': copy.deepcopy(getattr(ledger, '_accounting_trusted_anchor', None)),
                    'audit_prefix': audit_prefix,
                    'scope': self._scope_record(ledger, active, run[0]) if active else None}
        except (sqlite3.Error, OSError) as exc:
            raise InvalidUpdate('Borrowed gateway connection is unavailable: ' + str(exc)) from None

    def _phase_ledger(self, phase):
        if self._borrowed is None:
            return InvocationLedger(self.db)
        current = self._borrow_state()
        if current != self._borrow_proof:
            raise InvalidUpdate('Borrowed gateway binding changed during invocation')
        ledger = self._borrowed
        owner = getattr(self, '_borrow_owner', None)
        if owner and self._scope_record(ledger, owner[0], current['run'][0]) != owner[1]:
            raise InvalidUpdate('Borrowed gateway charge owner changed')
        # Preserve only the profile explicitly selected by the host caller.
        # An unanchored ledger does not acquire trust from a stored DB digest.
        if getattr(ledger, '_accounting_trusted_anchor', None) is not None:
            from accounting_batches import recover, status
            from accounting_metrics import measure
            with measure(ledger, 'gateway_prewarm', phase=phase):
                cached = getattr(ledger, '_accounting_cache', None)
                if getattr(cached, 'recovery_mode', None) != 'trusted_seal':
                    # An explicit deep audit may have replaced the projection
                    # with its strict reconstruction. Reapply the caller's
                    # original selection, never a newer DB-derived seal.
                    recover(ledger, trusted_anchor=self._borrow_proof['accounting_anchor'],
                            audit_prefix=self._borrow_proof['audit_prefix'])
                else:
                    status(ledger)
                if getattr(ledger, '_accounting_audit_prefix_enabled', False) and not ledger.audit_valid():
                    raise InvalidUpdate('Borrowed gateway audit prefix verification failed')
        return ledger

    def _release_ledger(self, ledger):
        if self._borrowed is None:
            ledger.close()

    def invoke(self, role, call_id, system, payload, schema, delivery_id):
        if self._borrowed is not None:
            self._borrow_proof = self._borrow_state()
            self._borrow_owner = None
        from local_model import LocalModel
        from pathlib import Path
        from reasoning_policy import check_provider
        check_provider(self.provider, payload, fresh=isinstance(self.provider, LocalModel))
        if isinstance(self.provider, LocalModel):
            from monitoring import ResourceWait
            entered = False
            try:
                with self.provider.dispatch_gate(Path(self.db).parent,call_id,system,payload,schema) as plan:
                    entered = True
                    self.provider.memory_plan=plan
                    return self._invoke(role,call_id,system,payload,schema,delivery_id)
            except ResourceWait as error:
                state = copy.deepcopy(error.state)
                runtime = dict(state.get('runtime') or {})
                runtime['model'] = self.provider.model
                if not entered:
                    runtime['inference_started'] = False
                state['runtime'] = runtime
                # A wait is a governed stop even when it occurs after the
                # dispatch reservation. _invoke has settled any such receipt;
                # keep the original cause in the same immutable run trace.
                ledger = self._phase_ledger('resource_wait')
                try:
                    with ledger.db:
                        ledger.db.execute('BEGIN IMMEDIATE')
                        run = ledger.run(self.run_id)
                        if not any(event['kind'] in ('ALLOW', 'FIN', 'RESOURCE_WAIT')
                                   for event in run['events']):
                            ledger._audit('RESOURCE_GATE', {'run_id': self.run_id,
                                                            'measurement': state})
                            ledger._event(self.run_id, 'RESOURCE_WAIT',
                                          {'reason': str(error), 'measurement': state})
                finally:
                    self._release_ledger(ledger)
                raise ResourceWait(state) from error
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
        ledger = self._phase_ledger('register')
        try:
            charge = self.provider.budget.check(system, payload, schema)
            from local_model import LocalModel
            from provider_resources import require_resources
            resource=require_resources(Path(self.db).parent,provider=self.provider,committed=True) if isinstance(self.provider,LocalModel) else {'profile':'injected-test-provider'}
            if getattr(self.provider,'memory_plan',None):resource=dict(resource,runtime_memory=self.provider.memory_plan)
            from model_identity import observe, unknown
            identity=observe(name) if isinstance(self.provider,LocalModel) else getattr(self.provider,'model_identity',unknown(name))
            expected=getattr(self.provider,'expected_identity',None)
            if expected is not None and identity!=expected: raise InvalidUpdate('Selected model identity changed before dispatch')
            self.provider.dispatch_identity=identity
            from model_packet import build, descriptor
            packet = build(system, payload, schema) if getattr(self.provider, 'model_interface', None) == 1 else None
            interface = descriptor(packet, self.provider.budget.template_reserve) if packet else None
            from context_window import check as check_window, exact, runtime_context, validate_proof
            measurement = None
            if exact(self.provider.budget):
                # A model can restart after its dispatch gate. Revalidate the
                # live identity immediately before committing the invocation.
                from remote_prefix import plan as prefix_plan
                prefix = prefix_plan(ledger, self.run_id, role, self.provider)
                options = {'prefix': prefix} if prefix is not None else {}
                measurement = check_window(self.provider.budget, system, payload, schema,
                                           thinking=getattr(self.provider, 'thinking', None), **options)
                validate_proof(measurement, self.provider.budget, system, payload, schema, identity)
                self.provider.context_measurement = measurement
                if prefix is not None:
                    self.provider.runtime_context_tokens = runtime_context(self.provider.budget, measurement)
            from invocation_timing import plan as timing_plan, require_headroom
            timing = timing_plan(self.provider, role, payload, ledger=ledger, run_id=self.run_id)
            require_headroom(timing)
            envelope = ledger.register(self.run_id, call_id, role, payload, system, schema, delivery_id, name, charge,
                budget=vars(self.provider.budget).copy(),resource=resource,model_identity=identity,model_interface=interface,
                context_measurement=measurement, transport_timing=timing)
            if self._borrowed is not None and envelope.get('orchestration'):
                nid = envelope['orchestration']['node_id']
                self._borrow_owner = (nid, self._scope_record(ledger, nid, envelope['session']))
        finally: self._release_ledger(ledger)
        if self._borrowed is not None and self._borrow_state() != self._borrow_proof:
            raise InvalidUpdate('Borrowed gateway binding changed before inference')
        from invocation_timing import reserved_seconds
        call_seconds = reserved_seconds(envelope)
        started=time.monotonic()
        permit = {'hash': envelope['request_hash'], 'used': False, 'packet_hash': (envelope.get('model_interface') or {}).get('packet_hash'),
                  'transport_seconds': call_seconds,
                  'transport_deadline': started+call_seconds if envelope.get('transport_timing') else None}
        token = _permit.set(permit); error = None; proposal = None; record = None
        failure_kind = None
        repairable = False
        resource_wait = None
        from model_activity import scope
        try:
            from lifecycle import call_deadline
            with scope(envelope) as activity, (call_deadline(call_seconds) if envelope.get('transport_timing') else nullcontext()):
                proposal, record = self.provider.call(call_id, system, copy.deepcopy(payload), copy.deepcopy(schema))
                if isinstance(self.provider,LocalModel) and observe(name)!=identity:
                    raise InvalidUpdate('Model identity changed during inference')
        except Exception as exc:
            from local_model import OutputLimitExceeded, StructuredOutputRejected
            from monitoring import ResourceWait
            if isinstance(exc, ResourceWait):
                resource_wait = exc
            if isinstance(exc, OutputLimitExceeded):
                failure_kind = 'output_limit'
            elif isinstance(exc, (TimeoutError, socket.timeout)):
                failure_kind = 'timeout'
            repairable = isinstance(exc, StructuredOutputRejected)
            error = type(exc).__name__ + ': ' + str(exc)
            record = getattr(exc, 'call_record', None) or record or {'call_id': call_id, 'ok': False, 'error': error}
        finally: _permit.reset(token)
        ledger = self._phase_ledger('finalize')
        try:
            if not error: ledger.confirm_delivery(delivery_id)
            from token_usage import measured, history
            from budget_accounting import settlement
            try: progress = activity.progress_snapshot(finish=True)
            except Exception: progress = None
            result = ledger.finalize(envelope, payload, proposal, error, time.monotonic() - started,
                                     measured(record), settlement(envelope, record), failure_kind,
                                     progress, record.get('transport') if isinstance(record,dict) else None)
            activity.finish(result['admission']['admitted'], measured(record))
            result['token_usage'] = history(ledger.db, run_id=self.run_id)
            if error and not repairable: result['blocked'] = error
            if failure_kind is not None: result['failure_kind'] = failure_kind
            result['record'] = record
            if resource_wait is not None:
                from monitoring import ResourceWait
                state = copy.deepcopy(resource_wait.state)
                runtime = dict(state.get('runtime') or {})
                runtime.update(model=name, invocation_id=envelope['id'])
                state['runtime'] = runtime
                raise ResourceWait(state) from resource_wait
            return result
        finally: self._release_ledger(ledger)

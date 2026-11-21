"""Local protocol ledger: contract epochs, assertion receipts and quarantine.

Hashes are tamper-evident commitments, not signatures, ZK proofs or attestations.
Only the trusted host writes this ledger. Derived checkpoints remain model data.
"""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

from context_manager import canonical, sha, InvalidUpdate, Conflict
from evidence import EvidenceContext, CURRENT
from paging import PageFault


POLICY_FILES = ('remote_profiles.py','remote_control.py','remote_model.py','remote_memory.py','provider_resources.py','task_continuation.py','retained_context.py','discovery_governor.py','discovery_capacity.py','discovery_control.py','output_shape.py','text_coherence.py','coherence_observations.py','generation_progress.py','engine_profiles.py','engine_model.py','engine_client.py','engine_service.py','engine_process.py','engine_codec.py','engine_control.py','execution_sessions.py','source_paging.py','review_source_receipts.py','review_followup.py','review_discovery.py','review_targets.py','evidence_admission.py','document_review.py','document_routing.py','document_stages.py','document_verdict_review.py','document_source.py','document_text.py','task_stack.py','git_provenance.py','capability_registry.py','capability_packages.py','capabilities.py','capability-packages.json','claim_pages.py','allocation_recovery.py','workspace_allocation.py','model_packet.py','budget_accounting.py','review_reasoning.py','model_activity.py','resource_routing.py','memory_layer.py','memory-policy.json','task_observations.py','decoder_schema.py','conversation_prompt.py','reasoning_policy.py','conversation_routing.py','memory_probe.py','memory_profiles.py','memory_observation.py','runtime_memory.py','report_review.py','model_identity.py','review_report.py','review_context.py','orchestration.py','orchestration_flow.py','subagent_runtime.py','native_access.py','conversation_memory.py','model_context.py','token_usage.py','visuals.py','conversation.py','native_runtime.py','model_catalog.py','temperature_signals.py','temperature-provenance.json','smc_temperature.h','chat_runtime.py','model_routing.py','context_manager.py', 'paging.py', 'evidence.py', 'checked_protocol.py',
                'policy_check.py', 'governance.py', 'governed_runtime.py', 'dag_runtime.py',
                'protocol.py', 'dag_protocol.py', 'kernel.py', 'invocation_gateway.py', 'invocation_timing.py',
                'invocation_roles.py', 'runtime.py', 'local_model.py', 'strata_model.py', 'context_window.py', 'legacy_model.py',
                'kernel_adapter.py', 'kernel_boundary.py', 'smriti_adapter.py', 'karma_adapter.py', 'calibration.py', 'stp_canonical/kernel.py', 'stp_canonical/__init__.py',
                'review_role.py', 'vscode_bridge.py', 'coding_role.py', 'host_actions.py',
                'monitoring.py', 'unittest_entry.py', 'telemetry.m', 'lifecycle.py', 'storage_policy.py',
                'repository_tools.py', 'architecture_role.py', 'architecture_runtime.py','api_extract.py','api_extract.js','api_facts.py','api_roles.py','api_runtime.py',
                'static_typescript.js','static_analysis.py','static_role.py','static_runtime.py','platform_support.py','telemetry_portable.js','review_budget.py','accounting_batches.py','accounting_metrics.py','accounting_audit.py','ledger_sqlite.py','auto_review.py')


def policy_hash():
    root = Path(__file__).parent
    return sha(canonical({name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in POLICY_FILES}))


class GovernedContext(EvidenceContext):
    def __init__(self, path):
        super().__init__(path)
        self.logic_hash = policy_hash()
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS contract_epochs(session TEXT, epoch INTEGER,
            record_json TEXT NOT NULL, digest TEXT NOT NULL,
            PRIMARY KEY(session,epoch), UNIQUE(session,digest));
          CREATE TABLE IF NOT EXISTS protocol_runs(id TEXT PRIMARY KEY, session TEXT,
            config_json TEXT NOT NULL, created_at REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS protocol_events(run_id TEXT, seq INTEGER,
            record_json TEXT NOT NULL, PRIMARY KEY(run_id,seq));
          CREATE TABLE IF NOT EXISTS assertion_receipts(session TEXT, seq INTEGER,
            id TEXT UNIQUE, run_id TEXT, delivery_id TEXT, record_json TEXT NOT NULL,
            PRIMARY KEY(session,seq));
          CREATE TABLE IF NOT EXISTS checkpoints(id TEXT PRIMARY KEY, session TEXT,
            run_id TEXT, record_json TEXT NOT NULL, digest TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS quarantine_events(checkpoint_id TEXT PRIMARY KEY,
            reason TEXT NOT NULL, created_at REAL NOT NULL);
          CREATE INDEX IF NOT EXISTS receipt_protocol_runs_session
            ON protocol_runs(session,id);
          CREATE INDEX IF NOT EXISTS receipt_run_commits_session
            ON page_audit(json_extract(payload_json,'$.session'),seq)
            WHERE event='RUN_COMMIT';
          CREATE INDEX IF NOT EXISTS receipt_protocol_events_run
            ON page_audit(json_extract(payload_json,'$.run_id'),seq)
            WHERE event='PROTOCOL_EVENT';
        ''')
        for table in ('contract_epochs', 'protocol_runs', 'protocol_events', 'assertion_receipts', 'checkpoints', 'quarantine_events'):
            for op in ('UPDATE', 'DELETE'):
                self.db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{op} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'immutable protocol record'); END")
        self.db.commit()
        from calibration import initialize
        initialize(self)
        from orchestration import initialize as initialize_orchestration
        initialize_orchestration(self)

    def _contract_history(self, session):
        state = {'task_id': None, 'goal': None, 'constraints': {}, 'selected_option': None}
        previous_state, previous_hash, history = None, 'GENESIS', []
        for revision, event_hash, raw in self.db.execute(
            "SELECT revision,event_hash,payload FROM events WHERE session=? AND kind IN ('user_patch','conversation_contract') ORDER BY revision", (session,)):
            for op in json.loads(raw):
                key = op['key']
                if key in ('task_id', 'goal', 'selected_option'): state[key] = op['value']
                else: state['constraints'][key] = op['value']
            if canonical(state) == previous_state:
                continue
            previous_state = canonical(state)
            record = {'session': session, 'epoch': len(history) + 1, 'source_revision': revision,
                      'source_event_hash': event_hash, 'previous_hash': previous_hash,
                      'origin_hash': sha('origin-v1:' + previous_state), 'contract': copy.deepcopy(state)}
            digest = sha('contract-v1:' + canonical(record))
            history.append((record, digest)); previous_hash = digest
        return history

    def current_contract(self, session):
        if not self.validate_chain(session): raise InvalidUpdate('Invalid task event chain')
        history = self._contract_history(session)
        if not history: raise InvalidUpdate('No user contract established')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            for record, digest in history:
                row = self.db.execute('SELECT record_json,digest FROM contract_epochs WHERE session=? AND epoch=?',
                                      (session, record['epoch'])).fetchone()
                if row:
                    if row != (canonical(record), digest): raise InvalidUpdate('Contract epoch commitment mismatch')
                else:
                    self.db.execute('INSERT INTO contract_epochs VALUES(?,?,?,?)',
                                    (session, record['epoch'], canonical(record), digest))
                    self._audit('CONTRACT_EPOCH', {'session': session, 'epoch': record['epoch'], 'hash': digest})
        record, digest = history[-1]
        return dict(record, contract_hash=digest)

    def pinned(self, session):
        self.current_contract(session)
        return self.validation_contract(session)

    def validation_contract(self, session):
        """Reconstruct a pinned packet without committing inside a validation transaction."""
        result = super().pinned(session);contract=self.origin(session)
        result['epoch'] = contract['epoch']; result['contract_hash'] = contract['contract_hash']
        from calibration import current,EMPTY_HASH
        binding=current(self,session,usable=True)
        if binding['hash']!=EMPTY_HASH:result['calibration_binding']=binding
        return result

    def origin(self, session, bootstrap=False):
        history = self._contract_history(session)
        if history:
            record, digest = history[-1]
            return dict(record, contract_hash=digest)
        if not bootstrap: raise InvalidUpdate('No user contract established')
        self.snapshot(session)  # Unknown sessions cannot obtain a bootstrap origin.
        return {'epoch': 0, 'contract_hash': sha('bootstrap-v1:' + session)}

    def begin_run(self, session, request, required_facts, policy_request, *, max_repairs=2,
                  max_calls=12, max_seconds=600, parents=(), run_id=None, purpose='task'):
        if type(max_repairs) is not int or not 0 <= max_repairs <= 2:
            raise InvalidUpdate('Repair limit must be 0..2')
        if type(max_calls) is not int or not 1 <= max_calls <= 32 or not 1 <= max_seconds <= 3600:
            raise InvalidUpdate('Invalid protocol budget')
        if purpose not in ('task', 'parser'): raise InvalidUpdate('Unknown run purpose')
        contract = self.origin(session, bootstrap=purpose == 'parser')
        if contract['epoch']: self.current_contract(session)
        for parent in parents: self.load_checkpoint(session, parent)
        config = {'contract_hash': contract['contract_hash'], 'epoch': contract['epoch'],
                  'logic_hash': self.logic_hash, 'request_hash': sha(canonical([request, required_facts, policy_request])),
                  'max_repairs': max_repairs, 'max_calls': max_calls, 'max_seconds': max_seconds,
                  'parents': list(parents), 'purpose': purpose}
        from calibration import current
        config['calibration_binding']=current(self,session,usable=True)
        run_id = run_id or uuid.uuid4().hex
        with self.db:
            self.db.execute('INSERT INTO protocol_runs VALUES(?,?,?,?)', (run_id, session, canonical(config), time.time()))
            self._audit('RUN_COMMIT', {'run_id': run_id, 'session': session, 'config': config})
            self._event(run_id, 'START', {'contract_hash': config['contract_hash']})
        return run_id

    def run(self, run_id):
        row = self.db.execute('SELECT session,config_json,created_at FROM protocol_runs WHERE id=?', (run_id,)).fetchone()
        if not row: raise InvalidUpdate('Unknown protocol run')
        events = [json.loads(x[0]) for x in self.db.execute('SELECT record_json FROM protocol_events WHERE run_id=? ORDER BY seq', (run_id,))]
        return {'id': run_id, 'session': row[0], 'config': json.loads(row[1]), 'created_at': row[2], 'events': events}

    def _event(self, run_id, kind, data):
        seq = self.db.execute('SELECT COUNT(*) FROM protocol_events WHERE run_id=?', (run_id,)).fetchone()[0] + 1
        event = {'kind': kind, 'data': data, 'at': time.time()}
        self.db.execute('INSERT INTO protocol_events VALUES(?,?,?)', (run_id, seq, canonical(event)))
        self._audit('PROTOCOL_EVENT', {'run_id': run_id, 'seq': seq, **event})

    def event(self, run_id, kind, data):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if any(e['kind'] in ('ALLOW', 'FIN', 'RESOURCE_WAIT') for e in self.run(run_id)['events']):
                raise InvalidUpdate('Protocol run already terminal')
            if kind not in ('NACK', 'BRIDGE', 'ALLOW', 'FIN', 'RESOURCE_WAIT'):
                raise InvalidUpdate('Unknown protocol transition')
            self._event(run_id, kind, data)

    def reserve(self, run_id, kind, data):
        """Durably reserve a slot before dispatch; reopening cannot reset budgets."""
        owns_transaction = not self.db.in_transaction
        try:
            if owns_transaction: self.db.execute('BEGIN IMMEDIATE')
            if kind not in ('ATTEMPT', 'DISPATCH'): raise InvalidUpdate('Unknown reservation kind')
            run = self.run(run_id); config = run['config']; events = run['events']
            if any(e['kind'] in ('ALLOW', 'FIN', 'RESOURCE_WAIT') for e in events):
                raise InvalidUpdate('Protocol run already terminal')
            origin = self.origin(run['session'], bootstrap=config.get('purpose') == 'parser')
            if origin['contract_hash'] != config['contract_hash']:
                raise Conflict('Contract epoch changed')
            if self.logic_hash != config['logic_hash']: raise Conflict('Validator version changed')
            from calibration import assert_binding,EMPTY_HASH
            assert_binding(self,run['session'],config.get('calibration_binding',{'hash':EMPTY_HASH,'profiles':[]}))
            if time.time() - run['created_at'] >= config['max_seconds']:
                raise InvalidUpdate('Protocol wall-time budget exhausted')
            limit = 1 + config['max_repairs'] if kind == 'ATTEMPT' else config['max_calls']
            from karma_adapter import budget_decision
            measured = budget_decision(sum(e['kind'] == kind for e in events), limit)
            if measured['decision'] != 'ALLOW':
                raise InvalidUpdate('Protocol ' + kind.lower() + ' budget exhausted: ' + measured['decision'])
            if kind == 'DISPATCH':
                for parent in config['parents']: self.load_checkpoint(run['session'], parent)
            self._event(run_id, kind, dict(data, budget_kernel=measured))
            if owns_transaction: self.db.commit()
            return measured
        except Exception:
            if owns_transaction: self.db.rollback()
            raise

    def checkpoint_status(self, session, checkpoint_id, stack=()):
        if checkpoint_id in stack: return 'quarantined_cycle'
        row = self.db.execute('SELECT session,record_json,digest FROM checkpoints WHERE id=?', (checkpoint_id,)).fetchone()
        if not row or row[0] != session: return 'quarantined_wrong_session'
        if sha('checkpoint-v1:' + row[1]) != row[2]: return 'quarantined_corrupt'
        record = json.loads(row[1])
        if self.db.execute('SELECT 1 FROM quarantine_events WHERE checkpoint_id=?', (checkpoint_id,)).fetchone():
            return 'quarantined'
        # A worker call and its checkpoint are two views of the same output.
        # Link through immutable assertion/delivery receipts, including older
        # checkpoints which predate the unified invocation gateway.
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='invocation_quarantine'").fetchone():
            for receipt_id in record['receipt_ids']:
                if self.db.execute('''SELECT 1 FROM assertion_receipts a
                    JOIN invocations i ON i.delivery_id=a.delivery_id AND i.session=a.session
                    JOIN invocation_quarantine q ON q.invocation_id=i.id
                    WHERE a.id=? AND a.session=?''', (receipt_id, session)).fetchone():
                    return 'quarantined_invocation'
        history = self._contract_history(session)
        if not history or history[-1][1] != record['contract_hash']: return 'quarantined_old_contract'
        if record['logic_hash'] != self.logic_hash: return 'quarantined_old_validator'
        from calibration import assert_binding,EMPTY_HASH
        try:assert_binding(self,session,record.get('calibration_binding',{'hash':EMPTY_HASH,'profiles':[]}))
        except InvalidUpdate:return 'quarantined_calibration'
        if record.get('evidence_index') != self.evidence_index(session, record.get('required_facts', [])):
            return 'quarantined_changed_catalog'
        for parent in record['parents']:
            if self.checkpoint_status(session, parent, stack + (checkpoint_id,)) != 'available':
                return 'quarantined_ancestor'
        for ref in record['source_pages']:
            try:
                page = self.page(session, ref['id'], ref['version'])
                if page.content_hash != ref['hash'] or super().status(page) not in CURRENT:
                    return 'quarantined_stale_evidence'
            except PageFault: return 'quarantined_missing_evidence'
        return 'available'

    def load_checkpoint(self, session, checkpoint_id):
        status = self.checkpoint_status(session, checkpoint_id)
        if status != 'available': raise PageFault('Checkpoint ' + checkpoint_id + ': ' + status)
        row = self.db.execute('SELECT record_json FROM checkpoints WHERE id=?', (checkpoint_id,)).fetchone()
        return json.loads(row[0])

    def quarantine(self, session, checkpoint_id, reason):
        row = self.db.execute('SELECT session FROM checkpoints WHERE id=?', (checkpoint_id,)).fetchone()
        if not row or row[0] != session or not reason: raise InvalidUpdate('Invalid quarantine request')
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO quarantine_events VALUES(?,?,?)', (checkpoint_id, reason, time.time()))
            self._audit('CHECKPOINT_QUARANTINE', {'id': checkpoint_id, 'reason': reason})

    def status(self, page, now=None, stack=()):
        checkpoint_id = json.loads(page.source_json).get('checkpoint_id')
        if checkpoint_id:
            status = self.checkpoint_status(page.session, checkpoint_id)
            if status != 'available': return status
        return super().status(page, now, stack)

    def catalog(self, session, group=None, offset=0, limit=6):
        result = super().catalog(session, group, offset, limit)
        if group is not None:
            result['items'] = [item for item in result['items'] if not item['status'].startswith('quarantined')]
        return result

    def record_result(self, run_id, attempt, result, model_name):
        run = self.run(run_id); session = run['session']; config = run['config']
        workers = [turn for turn in result['turns'] if turn['stage'] == 'worker']
        if not workers: return None
        rid = workers[-1]['delivery_id']
        row = self.db.execute('SELECT payload_json,payload_hash FROM deliveries WHERE id=? AND session=?', (rid, session)).fetchone()
        if not row: raise InvalidUpdate('Missing receipt delivery')
        payload = json.loads(row[0]); proposal = result.get('model_proposal', {})
        if not proposal:
            proposal = next((c.get('answer', {}) for c in reversed(result['calls']) if c.get('answer')), {})
        result['last_delivery_id'] = rid
        result['model_proposal'] = proposal
        try:
            from calibration import assert_binding,EMPTY_HASH
            assert_binding(self,session,config.get('calibration_binding',{'hash':EMPTY_HASH,'profiles':[]}))
            checked = self.finish(session, rid, proposal, [item['key'] for item in payload['evidence_index']])
            result.update(checked)
        except (ValueError, TypeError, KeyError, InvalidUpdate) as exc:
            result['admission'] = {'admitted': False, 'errors': [str(exc)]}
        current = self.current_contract(session)
        if current['contract_hash'] != config['contract_hash'] or self.logic_hash != config['logic_hash']:
            result['admission']['errors'].append('Contract epoch or validator changed')
            result['admission']['admitted'] = False
            result['admission']['interpretation_semantics_verified'] = False
        if result.get('invocation_id'):
            gateway_row = self.db.execute('SELECT record_json FROM invocation_receipts WHERE invocation_id=?', (result['invocation_id'],)).fetchone()
            gateway_receipt = json.loads(gateway_row[0]) if gateway_row else {}
            if gateway_receipt.get('status') != 'accepted':
                result['admission']['errors'] += gateway_receipt.get('admission', {}).get('errors', ['Missing gateway admission'])
                result['admission']['admitted'] = False
        admitted = result['admission']['admitted']
        assertions = []
        if isinstance(proposal, dict):
            facts = proposal.get('facts', []); states = proposal.get('unavailable_evidence', [])
            assertions += [('fact', f.get('key', ''), f) for f in (facts if isinstance(facts,list) else []) if isinstance(f, dict)]
            assertions += [('evidence_state', f.get('key', ''), f) for f in (states if isinstance(states,list) else []) if isinstance(f, dict)]
            if proposal.get('interpretation'):
                assertions.append(('interpretation', sha(canonical(payload.get('policy_check'))), {'text': proposal['interpretation']}))
            assertions.append(('operation', 'next', {k: proposal.get(k) for k in ('action', 'target', 'complete')}))
        else: assertions = [('invalid_proposal', 'raw', {'text': str(proposal)})]
        refs = [{'id': p['id'], 'version': p['version'], 'hash': p['hash'], 'source_span': p.get('source_span')}
                for p in payload['pages'] if p['authority'] != 'model']
        ids = []; response_hash = sha(canonical(proposal))
        checkpoint_id = uuid.uuid4().hex
        with self.db:
            # Lock before reading the next sequence number, not merely on INSERT.
            self.db.execute('BEGIN IMMEDIATE')
            previous = self.db.execute('SELECT seq,id FROM assertion_receipts WHERE session=? ORDER BY seq DESC LIMIT 1', (session,)).fetchone()
            seq, previous_hash = previous if previous else (0, 'GENESIS')
            for kind, subject, assertion in assertions:
                seq += 1
                claim_id = sha(canonical([session, config['contract_hash'], kind, subject]))
                body = {'seq': seq, 'session': session, 'run_id': run_id, 'attempt': attempt,
                        'kind': kind, 'claim_id': claim_id, 'assertion': assertion,
                        'contract_hash': config['contract_hash'], 'epoch': config['epoch'],
                        'logic_hash': config['logic_hash'], 'model': model_name,
                        'calibration_binding':config.get('calibration_binding'),
                        'delivery_id': rid, 'payload_hash': row[1], 'response_hash': response_hash,
                        'status': 'accepted' if admitted else 'rejected', 'source_pages': refs,
                        'semantic_check': result['admission'].get('policy_verification', {}).get('status', 'not_requested') if kind == 'interpretation' else 'not_applicable',
                        'previous_hash': previous_hash}
                digest = sha('assertion-v1:' + canonical(body))
                self.db.execute('INSERT INTO assertion_receipts VALUES(?,?,?,?,?,?)', (session, seq, digest, run_id, rid, canonical(body)))
                self._audit('ASSERTION_RECEIPT', {'id': digest, 'record': body})
                ids.append(digest); previous_hash = digest
            checkpoint = {'id': checkpoint_id, 'session': session, 'run_id': run_id,
                          'calibration_binding':config.get('calibration_binding'),
                          'contract_hash': config['contract_hash'], 'logic_hash': config['logic_hash'],
                          'parents': config['parents'], 'receipt_ids': ids, 'source_pages': refs,
                          'required_facts': [item['key'] for item in payload['evidence_index']],
                          'evidence_index': payload['evidence_index'],
                          'candidate': result.get('answer', {}), 'proposal': proposal,
                          'initial_admitted': admitted}
            raw = canonical(checkpoint)
            self.db.execute('INSERT INTO checkpoints VALUES(?,?,?,?,?)', (checkpoint_id, session, run_id, raw, sha('checkpoint-v1:' + raw)))
            if not admitted:
                self.db.execute('INSERT INTO quarantine_events VALUES(?,?,?)', (checkpoint_id, 'rejected proposal', time.time()))
            self._audit('CHECKPOINT_COMMIT', {'record': checkpoint, 'quarantined': not admitted})
        # A crash before this optional index write leaves a stored but unindexed checkpoint.
        self.put_page(session, 'checkpoint-' + checkpoint_id, 'checkpoints', 'Derived checkpoint (never source authority)',
                      canonical({'prior_proposal': proposal}), authority='model',
                      source={'checkpoint_id': checkpoint_id})
        result['receipt_ids'] = ids; result['checkpoint_id'] = checkpoint_id
        return checkpoint_id

    def verify_protocol(self, session):
        # Scope materialized rows and their audit mirrors to one read snapshot.
        # The indexes below avoid replaying unrelated sessions' audit payloads;
        # all checks within this session remain part of verification.
        from accounting_metrics import record
        started = time.perf_counter_ns()
        stats = {'contract_rows': 0, 'assertion_rows': 0, 'run_rows': 0,
                 'audit_event_rows': 0, 'protocol_event_rows': 0}
        owns_transaction = not self.db.in_transaction
        ok = False
        try:
            if owns_transaction:
                self.db.execute('BEGIN')
            ok = self._verify_protocol(session, stats)
            return ok
        except (KeyError, TypeError, ValueError, sqlite3.DatabaseError):
            return False
        finally:
            if owns_transaction and self.db.in_transaction:
                # SQL transaction control does not reset an unrelated caller's
                # active SELECT cursor on Python 3.9's sqlite3 wrapper.
                self.db.execute('ROLLBACK')
            record(self, 'protocol_verification', time.perf_counter_ns() - started,
                   session=session, ok=ok, **stats)

    def _verify_protocol(self, session, stats=None):
        stats = stats if stats is not None else {'contract_rows': 0,
            'assertion_rows': 0, 'run_rows': 0, 'audit_event_rows': 0,
            'protocol_event_rows': 0}
        if not self.db.execute('SELECT 1 FROM sessions WHERE id=?', (session,)).fetchone(): return False
        if not self.validate_chain(session) or not self.audit_valid(): return False
        for record, digest in self._contract_history(session):
            stats['contract_rows'] += 1
            row = self.db.execute('SELECT record_json,digest FROM contract_epochs WHERE session=? AND epoch=?', (session, record['epoch'])).fetchone()
            if row != (canonical(record), digest): return False
        previous, seq = 'GENESIS', 0
        for n, digest, raw in self.db.execute('SELECT seq,id,record_json FROM assertion_receipts WHERE session=? ORDER BY seq', (session,)):
            stats['assertion_rows'] += 1
            body = json.loads(raw); seq += 1
            if n != seq or body['previous_hash'] != previous or sha('assertion-v1:' + raw) != digest: return False
            delivery = self.db.execute('SELECT payload_hash FROM deliveries WHERE id=? AND session=?', (body['delivery_id'], session)).fetchone()
            if not delivery or delivery[0] != body['payload_hash']: return False
            previous = digest
        runs = dict(self.db.execute('SELECT id,config_json FROM protocol_runs WHERE session=?', (session,)))
        stats['run_rows'] = len(runs)
        mirrored = set()
        for raw, in self.db.execute("""SELECT payload_json FROM page_audit
            WHERE event='RUN_COMMIT' AND json_extract(payload_json,'$.session')=?
            ORDER BY seq""", (session,)):
            stats['audit_event_rows'] += 1
            commit = json.loads(raw)
            run_id = commit['run_id']
            if run_id in mirrored or runs.get(run_id) != canonical(commit['config']): return False
            mirrored.add(run_id)
        if mirrored != runs.keys(): return False
        # Resolve each run through its indexed audit slice. A global join over
        # page_audit used to decode the entire ledger for every receipt check.
        for run_id, raw_config in runs.items():
            config = json.loads(raw_config)
            events = []
            for raw, in self.db.execute("""SELECT payload_json FROM page_audit
                WHERE event='PROTOCOL_EVENT' AND json_extract(payload_json,'$.run_id')=?
                ORDER BY seq""", (run_id,)):
                stats['audit_event_rows'] += 1
                event = json.loads(raw)
                if event.pop('run_id') != run_id or event.pop('seq') != len(events) + 1: return False
                events.append(event)
            rows = list(self.db.execute('SELECT seq,record_json FROM protocol_events WHERE run_id=? ORDER BY seq', (run_id,)))
            stats['protocol_event_rows'] += len(rows)
            if rows != [(seq, canonical(e)) for seq, e in enumerate(events, 1)]: return False
            if not events or events[0]['kind'] != 'START': return False
            if sum(e['kind'] == 'DISPATCH' for e in events) > config['max_calls']: return False
            if sum(e['kind'] == 'ATTEMPT' for e in events) > config['max_repairs'] + 1: return False
            terminal = [i for i,e in enumerate(events) if e['kind'] in ('ALLOW','FIN','RESOURCE_WAIT')]
            if terminal and (len(terminal) != 1 or terminal[0] != len(events)-1): return False
        return True

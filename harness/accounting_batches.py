"""Verified accounting projections and bounded append-only checkpoints.

The immutable orchestration records remain authoritative. A connection verifies
their history once, then verifies appended tails. Checkpoints bind source heads,
scope digests, cumulative spending and pending charges; they grant no authority.
No kernel decision or allocation is changed by rotating an accounting batch.
"""
from decimal import Decimal, InvalidOperation
from collections import deque
import json
import os
import time

from context_manager import InvalidUpdate, canonical, sha

VERSION = 2
SUPPORTED_VERSIONS = (1, 2)
DEFAULT_BATCH_SIZE = 64
SOURCES = {
    'nodes': ('orchestration_nodes', 'id,parent_id,session,record_json,digest'),
    'charges': ('orchestration_charges', 'id,node_id,kind,resource_id,cost_json'),
    'settlements': ('orchestration_settlements', 'charge_id,record_json,digest'),
    'events': ('orchestration_events', 'node_id,seq,record_json,digest'),
}
TERMINAL = {'DONE', 'CANCELLED', 'QUARANTINED'}


def initialize(s):
    legacy = not s.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='orchestration_accounting_config'").fetchone()
    s.db.executescript('''
      CREATE INDEX IF NOT EXISTS orchestration_nodes_parent ON orchestration_nodes(parent_id);
      CREATE INDEX IF NOT EXISTS orchestration_nodes_session_kind
        ON orchestration_nodes(session,json_extract(record_json,'$.kind'));
      CREATE INDEX IF NOT EXISTS orchestration_charges_node ON orchestration_charges(node_id);
      CREATE TABLE IF NOT EXISTS orchestration_accounting_config
        (id INTEGER PRIMARY KEY,record_json TEXT NOT NULL,digest TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS orchestration_accounting_checkpoints
        (seq INTEGER PRIMARY KEY,record_json TEXT NOT NULL,digest TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS orchestration_accounting_seal_headers
        (seq INTEGER PRIMARY KEY,record_json TEXT NOT NULL,digest TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS orchestration_accounting_findings
        (id INTEGER PRIMARY KEY,record_json TEXT NOT NULL,digest TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS page_audit_accounting_event ON page_audit(event);
    ''')
    for table in ('orchestration_accounting_config', 'orchestration_accounting_checkpoints',
                  'orchestration_accounting_seal_headers', 'orchestration_accounting_findings'):
        for operation in ('UPDATE', 'DELETE'):
            s.db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation} "
                         f"BEFORE {operation} ON {table} BEGIN "
                         "SELECT RAISE(ABORT,'immutable accounting record'); END")
    for table, _ in SOURCES.values():
        s.db.execute(f"CREATE TRIGGER IF NOT EXISTS monotonic_{table}_INSERT "
                     f"AFTER INSERT ON {table} WHEN NEW.rowid<=0 "
                     f"OR NEW.rowid<(SELECT MAX(rowid) FROM {table}) BEGIN "
                     "SELECT RAISE(ABORT,'non-monotonic accounting source'); END")
    if not s.db.execute('SELECT 1 FROM orchestration_accounting_config').fetchone():
        raw = canonical({'version': VERSION, 'batch_size': DEFAULT_BATCH_SIZE})
        s.db.execute('INSERT INTO orchestration_accounting_config VALUES(1,?,?)',
                     (raw, sha('accounting-config-v1:' + raw)))
    # V1 checkpoints have no snapshot payload and can be indexed once. Missing
    # V2 headers are an integrity finding, never silently reconstructed.
    for seq, raw, digest in s.db.execute('''SELECT c.seq,c.record_json,c.digest
            FROM orchestration_accounting_checkpoints c
            LEFT JOIN orchestration_accounting_seal_headers h ON h.seq=c.seq
            WHERE h.seq IS NULL ORDER BY c.seq''').fetchall():
        value = json.loads(raw)
        if value.get('version') != 1 or sha('accounting-checkpoint-v1:' + raw) != digest:
            raise InvalidUpdate('Accounting seal header is missing or corrupt')
        header = {'version': 1, 'seq': seq, 'previous': value['previous'], 'legacy_digest': digest}
        s.db.execute('INSERT INTO orchestration_accounting_seal_headers VALUES(?,?,?)',
                     (seq, canonical(header), digest))
    s.db.commit()
    # Old ledgers have no accounting checkpoint yet. Import their verified
    # spending once without rewriting any original node, charge or permission.
    if (legacy and s.db.execute('SELECT 1 FROM orchestration_charges LIMIT 1').fetchone()
            and not s.db.execute('SELECT 1 FROM orchestration_accounting_checkpoints LIMIT 1').fetchone()):
        with s.db:
            s.db.execute('BEGIN IMMEDIATE')
            verify(s)
            flush(s, reason='legacy_import')


def configure(s, batch_size=DEFAULT_BATCH_SIZE):
    """Set batch size before charges; this does not change a task allocation."""
    if type(batch_size) is not int or not 1 <= batch_size <= 4096:
        raise InvalidUpdate('Accounting batch size must be 1..4096 requests')
    if s.db.in_transaction:
        raise InvalidUpdate('Configure accounting before opening an effect transaction')
    with s.db:
        s.db.execute('BEGIN IMMEDIATE')
        if s.db.execute('SELECT 1 FROM orchestration_charges LIMIT 1').fetchone():
            raise InvalidUpdate('Accounting batch configuration is already committed')
        if s.db.execute('SELECT 1 FROM orchestration_accounting_checkpoints LIMIT 1').fetchone():
            raise InvalidUpdate('Accounting checkpoints already exist')
        current = _config(s)
        if current['batch_size'] == batch_size:
            return current
        # Immutable configuration epochs; choosing a cadence grants no capacity.
        raw = canonical({'version': VERSION, 'batch_size': batch_size})
        epoch = s.db.execute('SELECT COALESCE(MAX(id),0)+1 FROM orchestration_accounting_config').fetchone()[0]
        s.db.execute('INSERT INTO orchestration_accounting_config VALUES(?,?,?)',
                     (epoch, raw, sha('accounting-config-v1:' + raw)))
    s._accounting_cache = None
    return _config(s)


def _config(s):
    row = s.db.execute('SELECT record_json,digest FROM orchestration_accounting_config ORDER BY id DESC LIMIT 1').fetchone()
    if not row or sha('accounting-config-v1:' + row[0]) != row[1]:
        raise InvalidUpdate('Corrupt accounting configuration')
    value = json.loads(row[0])
    if (value.get('version') not in SUPPORTED_VERSIONS or type(value.get('batch_size')) is not int
            or not 1 <= value['batch_size'] <= 4096):
        raise InvalidUpdate('Unsupported accounting configuration')
    return value


def _identity(s):
    try:
        st = os.stat(s.path)
        return st.st_dev, st.st_ino
    except (OSError, TypeError):
        return None


def _blocked(s):
    reason = getattr(s, '_accounting_blocked', None)
    row = s.db.execute('SELECT record_json,digest FROM orchestration_accounting_findings ORDER BY id LIMIT 1').fetchone()
    if row:
        try:
            reason = json.loads(row[0]).get('reason', 'durable accounting audit finding')
        except (ValueError, AttributeError):
            reason = 'corrupt accounting finding'
        if sha('accounting-finding-v1:' + row[0]) != row[1]:
            reason = 'corrupt accounting finding'
        s._accounting_blocked = reason
    if reason:
        raise InvalidUpdate('Accounting admission blocked: ' + str(reason))


def block_admission(s, reason, head=None):
    """Record a host/background-audit finding; never silently clear it."""
    s._accounting_blocked = str(reason)
    s._accounting_cache = None
    s._accounting_pending_finding = (str(reason), head)
    if s.db.in_transaction or getattr(s, 'accounting_read_only', False):
        # The effect transaction may be about to roll back. Its controller can
        # persist the finding after rollback using this same public boundary.
        return False
    try:
        with s.db:
            s.db.execute('BEGIN IMMEDIATE')
            if not s.db.execute('SELECT 1 FROM orchestration_accounting_findings LIMIT 1').fetchone():
                value = {'reason': str(reason), 'head': head, 'at': time.time()}
                raw = canonical(value)
                s.db.execute('INSERT INTO orchestration_accounting_findings(record_json,digest) VALUES(?,?)',
                             (raw, sha('accounting-finding-v1:' + raw)))
        s._accounting_pending_finding = None
        return True
    except Exception:
        # Read-only auditors return their finding to the writable host. The
        # local admission latch remains active even if persistence is denied.
        return False


def persist_pending_finding(s):
    pending = getattr(s, '_accounting_pending_finding', None)
    if pending is None:
        return True
    if s.db.in_transaction:
        return False
    return block_admission(s, *pending)


def assert_node_digest(s, nid, digest):
    # Inspection remains available after quarantine; only admission is stopped.
    cached = getattr(s, '_accounting_cache', None)
    if cached is not None and cached.connection is s.db and nid in cached.nodes:
        if cached.nodes[nid]['digest'] != digest:
            raise InvalidUpdate('Node scope differs from the verified accounting seal')


def check_admission(s):
    _blocked(s)


def set_trusted_anchor(s, anchor):
    """Accept trust supplied by the caller, never a marker read from this DB."""
    if (not isinstance(anchor, dict) or set(anchor) != {'seq', 'digest'}
            or type(anchor['seq']) is not int or anchor['seq'] <= 0
            or not isinstance(anchor['digest'], str) or len(anchor['digest']) != 64
            or any(c not in '0123456789abcdef' for c in anchor['digest'])):
        raise InvalidUpdate('Invalid trusted accounting anchor')
    _blocked(s)
    row = s.db.execute('SELECT digest FROM orchestration_accounting_checkpoints WHERE seq=?', (anchor['seq'],)).fetchone()
    if not row or row[0] != anchor['digest']:
        raise InvalidUpdate('Trusted accounting anchor differs from this ledger')
    s._accounting_trusted_anchor = dict(anchor)
    # Accounting recovery alone never opts a caller into trusting a page-audit
    # prefix. Its stronger profile is deliberately connection-local and explicit.
    s._accounting_audit_prefix_enabled = False
    s._accounting_audit_prefix_profile = None
    s._accounting_cache = None
    return dict(anchor)


def _audit_profile_binding(s, anchor, config):
    path = ('memory', id(s.db)) if s.path == ':memory:' else os.path.realpath(s.path)
    return (path, _identity(s), id(s.db), getattr(s, 'logic_hash', None),
            anchor['seq'], anchor['digest'], config['version'], config['batch_size'])


def _select_audit_prefix(s):
    """Bind only the original caller-authenticated V2 seal, not the DB head."""
    anchor = s._accounting_trusted_anchor
    config = _config(s)
    row = s.db.execute('SELECT seq,record_json,digest FROM orchestration_accounting_checkpoints '
                       'WHERE seq=?', (anchor['seq'],)).fetchone()
    if not row or row[2] != anchor['digest']:
        raise InvalidUpdate('Trusted audit-prefix seal differs from selected anchor')
    value = _check_body(s, row, config)
    if value['version'] != VERSION:
        raise InvalidUpdate('Trusted audit prefix requires a V2 seal')
    cursor = value['audit_cursor']
    audit = s.db.execute('SELECT event,payload_json,previous_hash,event_hash FROM page_audit '
                         'WHERE seq=?', (cursor['seq'],)).fetchone()
    # _check_body authenticated the raw body, header, cursor and canonical audit
    # event. Retain the exact verified bytes; later checks compare, not reparse a
    # potentially large recovery payload for every receipt in this connection.
    return {'binding': _audit_profile_binding(s, anchor, config), 'anchor': dict(anchor),
            'raw': row[1], 'header_raw': canonical(_header(value)),
            'audit_row': audit, 'head': (cursor['seq'], audit[3])}


def trusted_audit_prefix(s):
    """Explicit host profile only; absence always means strict page auditing.

    The same externally supplied seal digest commits audit_cursor in its header.
    This authenticates a historical prefix, not the current source bytes. A full
    audit still re-derives those bytes; later seals are never auto-promoted.
    """
    if not getattr(s, '_accounting_audit_prefix_enabled', False):
        return None
    profile = getattr(s, '_accounting_audit_prefix_profile', None)
    anchor = getattr(s, '_accounting_trusted_anchor', None)
    if (profile is None or anchor != profile['anchor'] or
            _audit_profile_binding(s, anchor, _config(s)) != profile['binding']):
        raise InvalidUpdate('Trusted audit-prefix binding changed')
    row = s.db.execute('SELECT record_json,digest FROM orchestration_accounting_checkpoints '
                       'WHERE seq=?', (anchor['seq'],)).fetchone()
    header = s.db.execute('SELECT record_json,digest FROM orchestration_accounting_seal_headers '
                          'WHERE seq=?', (anchor['seq'],)).fetchone()
    audit = s.db.execute('SELECT event,payload_json,previous_hash,event_hash FROM page_audit '
                         'WHERE seq=?', (profile['head'][0],)).fetchone()
    if (row != (profile['raw'], anchor['digest']) or
            header != (profile['header_raw'], anchor['digest']) or audit != profile['audit_row']):
        raise InvalidUpdate('Trusted audit-prefix source differs from authenticated seal')
    return {'head': profile['head'], 'binding': profile['binding']}


def _number(value):
    if type(value) not in (int, float):
        raise InvalidUpdate('Invalid accounting measurement')
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise InvalidUpdate('Invalid accounting measurement') from None
    if not number.is_finite() or number < 0:
        raise InvalidUpdate('Invalid accounting measurement')
    return number


def _plain(values):
    return {key: int(value) if value == value.to_integral_value() else float(value)
            for key, value in values.items()}


class _Projection:
    def __init__(self, s):
        self.connection = s.db
        self.identity = _identity(s)
        self.schema = s.db.execute('PRAGMA schema_version').fetchone()[0]
        self.data_version = s.db.execute('PRAGMA data_version').fetchone()[0]
        self.config = _config(s)
        self.marks = {key: 0 for key in SOURCES}
        self.heads = {key: 'GENESIS' for key in SOURCES}
        self.anchors = {}
        self.nodes = {}
        self.states = {}
        self.event_heads = {}
        self.children = {}
        self.active = {}
        self.totals = {}
        self.action_seconds = {}
        self.charges = {}
        self.pending = set()
        self.dispatches = {}
        self.root_ids = set()
        self.live_ids = set()
        self.recent_charges = deque(maxlen=2 * self.config['batch_size'])
        self.checkpoint_rows = []
        self.checkpoint_anchor = None
        self.sealed = {key: 0 for key in SOURCES}
        self.last_activity_at = None
        self.batch_opened_at = None
        self.recovery_mode = 'strict'
        self.compacted = False

    def ancestors(self, nid):
        seen = set()
        while nid:
            if nid in seen or nid not in self.nodes:
                raise InvalidUpdate('Invalid accounting ancestry')
            seen.add(nid)
            yield nid
            nid = self.nodes[nid]['parent_id']

    def apply_cost(self, nid, kind, cost, direction):
        amounts = {key: _number(value) for key, value in cost.items()}
        for ancestor in self.ancestors(nid):
            values = self.totals[ancestor]
            if set(amounts) - set(values):
                raise InvalidUpdate('Accounting allocation dimensions differ')
            for key, value in amounts.items():
                values[key] += direction * value
                if values[key] < 0:
                    raise InvalidUpdate('Negative cumulative accounting balance')
            if kind in ('edit', 'run_tests'):
                self.action_seconds[ancestor] += direction * amounts.get('seconds', Decimal(0))

    def add(self, source, row):
        rowid, *values = row
        if rowid <= self.marks[source]:
            raise InvalidUpdate('Accounting source cursor regressed')
        if source == 'nodes':
            nid, parent, session, raw, digest = values
            value = json.loads(raw)
            if (sha('orchestration-node-v1:' + raw) != digest
                    or (value.get('id'), value.get('parent_id'), value.get('session')) != (nid, parent, session)):
                raise InvalidUpdate('Corrupt accounting node source')
            if parent and parent not in self.nodes:
                raise InvalidUpdate('Unknown accounting parent')
            self.nodes[nid] = {key: value.get(key) for key in
                               ('id', 'parent_id', 'kind', 'limits', 'session', 'binding', 'context_hash')}
            self.nodes[nid]['permissions_hash'] = sha(canonical(value.get('permissions')))
            self.nodes[nid]['digest'] = digest
            self.totals[nid] = {key: Decimal(0) for key in value.get('limits', {})}
            self.action_seconds[nid] = Decimal(0)
            self.states[nid] = value['initial_state']
            self.event_heads[nid] = (0, 'GENESIS')
            self.last_activity_at = max(self.last_activity_at or 0, value['created_at'])
            self.batch_opened_at = min(self.batch_opened_at or value['created_at'], value['created_at'])
            self.children.setdefault(parent, set()).add(nid)
            self.active.setdefault(parent, set())
            if value['initial_state'] not in TERMINAL:
                self.active[parent].add(nid)
                self.live_ids.add(nid)
            if not parent and value.get('limits'):
                self.root_ids.add(nid)
        elif source == 'charges':
            cid, nid, kind, resource_id, raw = values
            if nid not in self.nodes or cid in self.charges:
                raise InvalidUpdate('Unknown or duplicate accounting charge')
            cost = json.loads(raw)
            self.charges[cid] = {'node_id': nid, 'kind': kind, 'resource_id': resource_id,
                                 'declared': cost, 'used': cost, 'settled': False}
            self.recent_charges.append(cid)
            self.pending.add(cid)
            self.apply_cost(nid, kind, cost, 1)
        elif source == 'settlements':
            cid, raw, digest = values
            value = json.loads(raw)
            if (sha('orchestration-settlement-v1:' + raw) != digest or value.get('charge_id') != cid
                    or cid not in self.charges or self.charges[cid]['settled']):
                raise InvalidUpdate('Corrupt accounting settlement source')
            charge = self.charges[cid]
            if set(value['used']) != set(charge['declared']):
                raise InvalidUpdate('Settlement accounting dimensions changed')
            # Replace a reservation with its one authoritative settlement.
            self.apply_cost(charge['node_id'], charge['kind'], charge['used'], -1)
            self.apply_cost(charge['node_id'], charge['kind'], value['used'], 1)
            charge.update(used=value['used'], settled=True)
            self.pending.remove(cid)
            if value.get('at') is not None:
                at = float(_number(value['at']))
                self.last_activity_at = max(self.last_activity_at or 0, at)
                self.batch_opened_at = min(self.batch_opened_at or at, at)
        else:
            nid, seq, raw, digest = values
            value = json.loads(raw)
            previous_seq, previous = self.event_heads.get(nid, (None, None))
            from orchestration import EDGES
            current = self.states.get(nid)
            if (sha('orchestration-event-v1:' + raw) != digest or value.get('node_id') != nid
                    or value.get('seq') != seq or previous_seq is None or seq != previous_seq + 1
                    or value.get('previous') != previous or value.get('from') != current
                    or current in TERMINAL
                    or (value['to'] != current and value['to'] not in EDGES.get(current, set()))):
                raise InvalidUpdate('Corrupt accounting transition source')
            self.states[nid] = value['to']
            self.event_heads[nid] = seq, digest
            parent = self.nodes[nid]['parent_id']
            if value['to'] in TERMINAL:
                self.active.setdefault(parent, set()).discard(nid)
                self.live_ids.discard(nid)
            else:
                self.active.setdefault(parent, set()).add(nid)
                self.live_ids.add(nid)
            self.last_activity_at = max(self.last_activity_at or 0, value['at'])
            self.batch_opened_at = min(self.batch_opened_at or value['at'], value['at'])
            if value['kind'] == 'DISPATCH':
                data = value['data']
                cid = data['charge_id']
                charge = self.charges.get(cid)
                if (not charge or charge['node_id'] != nid or cid in self.dispatches
                        or data['resource_id'] != charge['resource_id'] or data['kind'] != charge['kind']
                        or not data.get('frames') or data['frames'][0].get('reserved_next') != charge['declared']):
                    raise InvalidUpdate('Accounting charge differs from its dispatch commitment')
                self.dispatches[cid] = digest
        self.marks[source] = rowid
        self.heads[source] = sha('accounting-source-v1:' + self.heads[source] + canonical(row))
        self.anchors[source] = tuple(row)

    def consume(self, s, limits=None):
        for source, (table, columns) in SOURCES.items():
            maximum = limits[source] if limits is not None else 2**63 - 1
            for row in s.db.execute('SELECT rowid,' + columns + ' FROM ' + table
                                    + ' WHERE rowid>? AND rowid<=? ORDER BY rowid',
                                    (self.marks[source], maximum)):
                self.add(source, row)
            if limits is not None and self.marks[source] != maximum:
                raise InvalidUpdate('Accounting checkpoint source watermark is unavailable')

    def basis(self):
        roots = {nid: {'used': _plain(self.totals[nid]),
                       'action_seconds': _plain({'seconds': self.action_seconds[nid]})['seconds'],
                       'scope_digest': n['digest']}
                 for nid in sorted(self.root_ids) for n in (self.nodes[nid],)}
        return {'watermarks': dict(self.marks), 'source_heads': dict(self.heads), 'roots': roots,
                'pending_count': len(self.pending), 'pending_hash': sha(canonical(sorted(self.pending)))}

    def retained(self):
        """Only active owners, pending costs, roots and a bounded status tail."""
        keep = set(self.root_ids) | set(self.live_ids)
        for cid in set(self.pending) | set(self.recent_charges):
            if cid in self.charges:
                keep.add(self.charges[cid]['node_id'])
        for nid in list(keep):
            keep.update(self.ancestors(nid))
        return keep

    def pending_entries(self):
        result = {}
        for cid in sorted(self.pending):
            charge = self.charges[cid]
            owner = self.nodes[charge['node_id']]
            result[cid] = dict(charge, dispatch_receipt=self.dispatches.get(cid),
                owner_digest=owner['digest'],
                owner={key: owner.get(key) for key in
                       ('id', 'digest', 'session', 'binding', 'context_hash', 'permissions_hash')})
        return result

    def recovery_state(self):
        keep = self.retained()
        charges = set(self.pending) | set(self.recent_charges)
        return {
            'nodes': {nid: self.nodes[nid] for nid in sorted(keep)},
            'states': {nid: self.states[nid] for nid in sorted(keep)},
            'event_heads': {nid: list(self.event_heads[nid]) for nid in sorted(keep)},
            # Decimal strings preserve every accumulated budget unit exactly.
            'totals': {nid: {key: str(value) for key, value in self.totals[nid].items()}
                       for nid in sorted(keep)},
            'action_seconds': {nid: str(self.action_seconds[nid]) for nid in sorted(keep)},
            'charges': {cid: self.charges[cid] for cid in sorted(charges)},
            'dispatches': {cid: self.dispatches[cid] for cid in sorted(charges) if cid in self.dispatches},
            'recent_charges': list(self.recent_charges),
            'source_anchors': {key: list(row) for key, row in self.anchors.items()},
            'last_activity_at': self.last_activity_at,
        }

    def compact(self):
        keep = self.retained()
        for mapping in (self.nodes, self.states, self.event_heads, self.totals, self.action_seconds):
            for nid in list(mapping):
                if nid not in keep:
                    del mapping[nid]
        charges = set(self.pending) | set(self.recent_charges)
        self.charges = {cid: value for cid, value in self.charges.items() if cid in charges}
        self.dispatches = {cid: value for cid, value in self.dispatches.items() if cid in charges}
        self.children = {}
        self.active = {}
        for nid in self.live_ids:
            self.active.setdefault(self.nodes[nid]['parent_id'], set()).add(nid)
        self.compacted = True


def _checkpoint_rows(s):
    return list(s.db.execute('SELECT seq,record_json,digest FROM orchestration_accounting_checkpoints ORDER BY seq'))


def _header(value):
    return {key: item for key, item in value.items()
            if key not in ('recovery_state', 'pending_entries')}


def _payload_hash(value):
    return sha(canonical({key: value[key] for key in ('recovery_state', 'pending_entries')}))


def _seal_digest(value):
    if value.get('version') == 1:
        return sha('accounting-checkpoint-v1:' + canonical(value))
    return sha('accounting-checkpoint-v2:' + canonical(_header(value)))


def _headers(s):
    return list(s.db.execute('SELECT seq,record_json,digest FROM orchestration_accounting_seal_headers ORDER BY seq'))


def _check_inventory_head(s):
    body = s.db.execute('SELECT seq,digest FROM orchestration_accounting_checkpoints ORDER BY seq DESC LIMIT 1').fetchone()
    header = s.db.execute('SELECT seq,digest FROM orchestration_accounting_seal_headers ORDER BY seq DESC LIMIT 1').fetchone()
    if body != header:
        raise InvalidUpdate('Accounting checkpoint differs from authoritative source history (body/header inventory)')


def _check_header_chain(s, rows, start=0, previous='GENESIS'):
    for ordinal, (seq, raw, digest) in enumerate(rows, start + 1):
        value = json.loads(raw)
        if seq != ordinal or value.get('seq') != seq or value.get('previous') != previous:
            raise InvalidUpdate('Accounting seal header chain corrupted')
        if value.get('version') == 1:
            body = s.db.execute('SELECT record_json,digest FROM orchestration_accounting_checkpoints WHERE seq=?', (seq,)).fetchone()
            if (not body or body[1] != digest or value.get('legacy_digest') != digest
                    or sha('accounting-checkpoint-v1:' + body[0]) != digest):
                raise InvalidUpdate('Legacy accounting seal header differs')
        elif value.get('version') != VERSION or sha('accounting-checkpoint-v2:' + raw) != digest:
            raise InvalidUpdate('Accounting seal header digest corrupted')
        previous = digest


def _check_body(s, row, config):
    seq, raw, digest = row
    value = json.loads(raw)
    if (value.get('version') not in SUPPORTED_VERSIONS or value.get('seq') != seq
            or value.get('batch_size') != config['batch_size']):
        raise InvalidUpdate('Accounting checkpoint profile differs')
    expected = sha('accounting-checkpoint-v1:' + raw) if value['version'] == 1 else _seal_digest(value)
    if expected != digest:
        raise InvalidUpdate('Accounting checkpoint differs from authoritative source history (digest corrupted)')
    if value['version'] == VERSION:
        if value.get('payload_hash') != _payload_hash(value):
            raise InvalidUpdate('Accounting recovery payload differs')
        header = s.db.execute('SELECT record_json,digest FROM orchestration_accounting_seal_headers WHERE seq=?', (seq,)).fetchone()
        if header != (canonical(_header(value)), digest):
            raise InvalidUpdate('Accounting checkpoint header differs')
        cursor = value['audit_cursor']
        audit = s.db.execute('SELECT event,payload_json,previous_hash,event_hash FROM page_audit WHERE seq=?',
                             (cursor['seq'],)).fetchone()
        # Older schema calls the preceding hash prev_hash; select * is stable.
        if audit is None:
            raise InvalidUpdate('Accounting checkpoint audit anchor is missing')
        expected_payload = canonical({'seq': seq, 'digest': digest})
        if (audit[0] != 'ORCHESTRATION_ACCOUNTING_CHECKPOINT' or audit[1] != expected_payload
                or audit[2] != cursor['previous']
                or audit[3] != sha(canonical([cursor['seq'], audit[0], audit[1], audit[2]]))):
            raise InvalidUpdate('Accounting checkpoint audit anchor differs')
    return value


def _verify_body(s, projection, row):
    value = _check_body(s, row, projection.config)
    projection.consume(s, value['watermarks'])
    if any(value.get(key) != expected for key, expected in projection.basis().items()):
        raise InvalidUpdate('Accounting checkpoint differs from authoritative source history')
    if set(projection.charges) - set(projection.dispatches):
        raise InvalidUpdate('Accounting checkpoint contains an uncommitted dispatch')
    if value['version'] == VERSION:
        if (value.get('pending_entries') != projection.pending_entries()
                or value.get('recovery_state') != projection.recovery_state()):
            raise InvalidUpdate('Accounting recovery snapshot differs from authoritative history')
    projection.sealed = dict(projection.marks)
    projection.batch_opened_at = None
    return value


def _verify_checkpoint_rows(s, projection, rows):
    previous = 'GENESIS'
    for expected, (seq, raw, digest) in enumerate(rows, 1):
        value = json.loads(raw)
        if (seq != expected or value.get('seq') != seq or value.get('version') not in SUPPORTED_VERSIONS
                or value.get('previous') != previous or value.get('batch_size') != projection.config['batch_size']
                or (value['version'] == 1 and sha('accounting-checkpoint-v1:' + raw) != digest)):
            raise InvalidUpdate('Accounting checkpoint chain corrupted')
        _verify_body(s, projection, (seq, raw, digest))
        previous = digest
    # The checkpoint table cannot quietly be dropped and recreated empty.
    anchors = [json.loads(raw) for raw, in s.db.execute(
        "SELECT payload_json FROM page_audit WHERE event='ORCHESTRATION_ACCOUNTING_CHECKPOINT' ORDER BY seq")]
    expected_anchors = [{'seq': seq, 'digest': digest} for seq, _, digest in rows]
    if anchors != expected_anchors:
        raise InvalidUpdate('Accounting checkpoint audit anchor differs')
    header_rows = _headers(s)
    _check_header_chain(s, header_rows)
    if [(seq, digest) for seq, _, digest in header_rows] != [(seq, digest) for seq, _, digest in rows]:
        raise InvalidUpdate('Accounting seal header index differs')
    projection.checkpoint_rows = [(seq, None, digest) for seq, _, digest in rows]
    projection.checkpoint_anchor = projection.checkpoint_rows[-1] if rows else None


def _rebuild_impl(s, compact=True):
    if not s.audit_valid(deep=True):
        raise InvalidUpdate('Accounting audit chain corrupted')
    for table, _ in SOURCES.values():
        minimum = s.db.execute('SELECT MIN(rowid) FROM ' + table).fetchone()[0]
        if minimum is not None and minimum <= 0:
            raise InvalidUpdate('Accounting source cursor is outside its supported range')
    projection = _Projection(s)
    _verify_checkpoint_rows(s, projection, _checkpoint_rows(s))
    projection.consume(s)
    if compact:
        projection.compact()
    s._accounting_cache = projection
    return projection


def _rebuild(s, compact=True):
    _blocked(s)
    from accounting_metrics import measure
    owned = not s.db.in_transaction
    if owned:
        s.db.execute('BEGIN')
    try:
        with measure(s, 'accounting_recovery_strict'):
            result = _rebuild_impl(s, compact)
        if owned:
            s.db.execute('COMMIT')
        return result
    except Exception as exc:
        if owned and s.db.in_transaction:
            s.db.execute('ROLLBACK')
        block_admission(s, str(exc))
        raise


def _restore(s, row):
    """Restore only a caller-trusted V2 seal; never infer trust from SQLite."""
    projection = _Projection(s)
    value = _check_body(s, row, projection.config)
    if value['version'] != VERSION:
        raise InvalidUpdate('A trusted recovery anchor requires a V2 accounting seal')
    state = value['recovery_state']
    projection.nodes = state['nodes']
    projection.states = state['states']
    projection.event_heads = {nid: tuple(head) for nid, head in state['event_heads'].items()}
    projection.totals = {nid: {key: Decimal(number) for key, number in numbers.items()}
                         for nid, numbers in state['totals'].items()}
    projection.action_seconds = {nid: Decimal(number) for nid, number in state['action_seconds'].items()}
    projection.charges = state['charges']
    projection.dispatches = state['dispatches']
    projection.pending = set(value['pending_entries'])
    projection.recent_charges = deque(state['recent_charges'], maxlen=2 * projection.config['batch_size'])
    projection.marks = dict(value['watermarks'])
    projection.heads = dict(value['source_heads'])
    projection.anchors = {key: tuple(anchor) for key, anchor in state['source_anchors'].items()}
    projection.sealed = dict(projection.marks)
    projection.last_activity_at = state['last_activity_at']
    projection.root_ids = {nid for nid, n in projection.nodes.items() if not n['parent_id'] and n.get('limits')}
    projection.live_ids = {nid for nid, status in projection.states.items() if status not in TERMINAL}
    for nid, n in projection.nodes.items():
        source = s.db.execute('SELECT record_json,digest FROM orchestration_nodes WHERE id=?', (nid,)).fetchone()
        if not source or source[1] != n['digest'] or sha('orchestration-node-v1:' + source[0]) != n['digest']:
            raise InvalidUpdate('Recovered node scope differs from trusted seal')
        seq, event_digest = projection.event_heads[nid]
        if seq:
            event = s.db.execute('SELECT record_json,digest FROM orchestration_events WHERE node_id=? AND seq=?', (nid, seq)).fetchone()
            if (not event or event[1] != event_digest or sha('orchestration-event-v1:' + event[0]) != event_digest
                    or json.loads(event[0])['to'] != projection.states[nid]):
                raise InvalidUpdate('Recovered state head differs from trusted seal')
        projection.active.setdefault(n['parent_id'], set())
        if nid in projection.live_ids:
            projection.active[n['parent_id']].add(nid)
        for values in (projection.totals[nid], {'seconds': projection.action_seconds[nid]}):
            if any(not number.is_finite() or number < 0 for number in values.values()):
                raise InvalidUpdate('Invalid trusted accounting total')
        list(projection.ancestors(nid))
    if any(value.get(key) != expected for key, expected in projection.basis().items()):
        raise InvalidUpdate('Recovered accounting totals differ from seal')
    if value['pending_entries'] != projection.pending_entries():
        raise InvalidUpdate('Recovered pending reservations differ from seal')
    for cid in projection.pending:
        charge = projection.charges[cid]
        source = s.db.execute('SELECT node_id,kind,resource_id,cost_json FROM orchestration_charges WHERE id=?', (cid,)).fetchone()
        if source != (charge['node_id'], charge['kind'], charge['resource_id'], canonical(charge['declared'])):
            raise InvalidUpdate('Pending charge differs from trusted seal')
    projection.recovery_mode = 'trusted_seal'
    projection.compacted = True
    return projection


def _refresh(s, projection):
    """Verify canonical new work before accepting any newly appended seal."""
    from accounting_metrics import measure
    with measure(s, 'accounting_refresh'):
        _check_inventory_head(s)
        known = len(projection.checkpoint_rows)
        rows = list(s.db.execute('SELECT seq,record_json,digest FROM orchestration_accounting_seal_headers WHERE seq>? ORDER BY seq', (known,)))
        bodies = list(s.db.execute('SELECT seq,digest FROM orchestration_accounting_checkpoints WHERE seq>? ORDER BY seq', (known,)))
        if bodies != [(seq, digest) for seq, _, digest in rows]:
            raise InvalidUpdate('New accounting checkpoint body/header inventory differs')
        previous = projection.checkpoint_anchor[2] if projection.checkpoint_anchor else 'GENESIS'
        _check_header_chain(s, rows, known, previous)
        for seq, _, digest in rows:
            row = s.db.execute('SELECT seq,record_json,digest FROM orchestration_accounting_checkpoints WHERE seq=?', (seq,)).fetchone()
            if row is None or row[2] != digest:
                raise InvalidUpdate('New accounting seal payload is missing')
            _verify_body(s, projection, row)
            projection.checkpoint_rows.append((seq, None, digest))
            projection.checkpoint_anchor = projection.checkpoint_rows[-1]
            projection.compact()
        projection.consume(s)
        projection.data_version = s.db.execute('PRAGMA data_version').fetchone()[0]
        return projection


def recover(s, trusted_anchor=None, *, audit_prefix=False):
    """Strict replay by default, or an explicitly caller-trusted seal + tail.

    An older trusted anchor authenticates only its prefix. Newer seals are
    re-derived from their canonical source suffix; they are never trusted merely
    because their previous hash names the anchor. audit_prefix=True additionally
    selects the audit cursor authenticated by that SAME supplied V2 seal; the
    default retains strict page-audit replay. No profile is persisted in SQLite.
    """
    if type(audit_prefix) is not bool or audit_prefix and trusted_anchor is None:
        raise InvalidUpdate('Audit-prefix recovery requires an explicit trusted anchor')
    _blocked(s)
    if trusted_anchor is not None:
        set_trusted_anchor(s, trusted_anchor)
    else:
        s._accounting_trusted_anchor = None
        s._accounting_audit_prefix_enabled = False
        s._accounting_audit_prefix_profile = None
    owned = not s.db.in_transaction
    if owned:
        s.db.execute('BEGIN')
    try:
        if trusted_anchor is None:
            projection = _rebuild(s)
        else:
            from accounting_metrics import measure
            with measure(s, 'accounting_recovery_anchored'):
                _check_inventory_head(s)
                rows = _headers(s)
                _check_header_chain(s, rows)
                seq, digest = trusted_anchor['seq'], trusted_anchor['digest']
                if seq > len(rows) or rows[seq - 1][2] != digest:
                    raise InvalidUpdate('Trusted accounting seal chain differs')
                row = s.db.execute('SELECT seq,record_json,digest FROM orchestration_accounting_checkpoints WHERE seq=?', (seq,)).fetchone()
                projection = _restore(s, row)
                projection.checkpoint_rows = [(ordinal, None, head) for ordinal, _, head in rows[:seq]]
                projection.checkpoint_anchor = projection.checkpoint_rows[-1]
                _refresh(s, projection)
                s._accounting_cache = projection
        profile = _select_audit_prefix(s) if audit_prefix else None
        if owned:
            s.db.execute('COMMIT')
        s._accounting_audit_prefix_profile = profile
        s._accounting_audit_prefix_enabled = audit_prefix
        return status(s)
    except BaseException as exc:
        s._accounting_cache = None
        s._accounting_audit_prefix_enabled = False
        s._accounting_audit_prefix_profile = None
        if owned:
            s.db.execute('ROLLBACK')
        if isinstance(exc, Exception):
            block_admission(s, str(exc), trusted_anchor)
        raise


def _projection_unlocked(s):
    _blocked(s)
    _check_inventory_head(s)
    cached = getattr(s, '_accounting_cache', None)
    if cached is not None and cached.connection is s.db and cached.identity != _identity(s):
        raise InvalidUpdate('Accounting database identity changed; reopen the ledger')
    schema = s.db.execute('PRAGMA schema_version').fetchone()[0]
    data_version = s.db.execute('PRAGMA data_version').fetchone()[0]
    if cached is None or cached.connection is not s.db:
        anchor = getattr(s, '_accounting_trusted_anchor', None)
        if anchor is not None:
            recover(s, anchor, audit_prefix=getattr(s, '_accounting_audit_prefix_enabled', False))
            return s._accounting_cache
        return _rebuild(s)
    if cached.schema != schema:
        return _rebuild(s)
    if cached.config != _config(s):
        raise InvalidUpdate('Accounting configuration changed')
    # Immutable source triggers protect prefixes while a connection stays open.
    # Schema changes force deep replay. Last-row anchors also detect rollback of
    # uncommitted cached state, including rowid reuse on the same connection.
    for source, anchor in cached.anchors.items():
        table, columns = SOURCES[source]
        current = s.db.execute('SELECT rowid,' + columns + ' FROM ' + table + ' WHERE rowid=?',
                               (anchor[0],)).fetchone()
        if current != anchor:
            return _rebuild(s)
    last = s.db.execute('SELECT seq,NULL,digest FROM orchestration_accounting_checkpoints ORDER BY seq DESC LIMIT 1').fetchone()
    if last != cached.checkpoint_anchor:
        return _refresh(s, cached) if cached.recovery_mode == 'trusted_seal' else _rebuild(s)
    if cached.data_version != data_version:
        return _refresh(s, cached) if cached.recovery_mode == 'trusted_seal' else _rebuild(s)
    cached.consume(s)
    return cached


def _projection(s):
    # Read several append-only tables from one SQLite snapshot. Writer callers
    # already hold their effect transaction; standalone status reads own only a
    # short read transaction and never leave it open for a later dispatch.
    owned = not s.db.in_transaction
    if owned:
        s.db.execute('BEGIN')
    try:
        result = _projection_unlocked(s)
        if owned:
            s.db.execute('COMMIT')
        return result
    except BaseException as exc:
        s._accounting_cache = None
        if owned:
            s.db.execute('ROLLBACK')
        if isinstance(exc, Exception):
            block_admission(s, str(exc))
        pending = getattr(s, '_accounting_pending_finding', None)
        if pending is not None and not s.db.in_transaction:
            block_admission(s, *pending)
        raise


def summary(s, nid):
    value = _projection(s)
    if nid not in value.nodes and value.compacted:
        value = _rebuild(s, compact=False)
    if nid not in value.nodes:
        raise InvalidUpdate('Unknown accounting node')
    return {'used': _plain(value.totals[nid]),
            'action_seconds': _plain({'seconds': value.action_seconds[nid]})['seconds'],
            'active_children': sorted(value.active.get(nid, set()))}


def current_state(s, nid):
    value = _projection(s)
    if nid not in value.states and value.compacted:
        value = _rebuild(s, compact=False)
    if nid not in value.states:
        raise InvalidUpdate('Unknown accounting state')
    return value.states[nid]


def event_head(s, nid):
    value = _projection(s)
    if nid not in value.states and value.compacted:
        value = _rebuild(s, compact=False)
    if nid not in value.states:
        raise InvalidUpdate('Unknown accounting state')
    seq, digest = value.event_heads[nid]
    return value.states[nid], seq, digest


def checkpoints(s):
    _projection(s)
    rows = _checkpoint_rows(s)
    headers = _headers(s)
    try:
        _check_header_chain(s, headers)
        if [(seq, digest) for seq, _, digest in rows] != [(seq, digest) for seq, _, digest in headers]:
            raise InvalidUpdate('Accounting checkpoint body/header inventory differs')
        return [dict(_check_body(s, row, _config(s)), digest=row[2]) for row in rows]
    except Exception as exc:
        block_admission(s, str(exc))
        raise


def status(s):
    value = _projection(s)
    return {'version': VERSION, 'batch_size': value.config['batch_size'],
            'checkpoint_count': len(value.checkpoint_rows),
            'head': value.checkpoint_anchor[2] if value.checkpoint_anchor else 'GENESIS',
            'tail_open': value.marks != value.sealed,
            'tail_requests': value.marks['charges'] - value.sealed['charges'],
            'tail_changes': sum(value.marks[key] - value.sealed[key] for key in ('charges', 'settlements')),
            'pending_charges': len(value.pending), 'recovery_mode': value.recovery_mode,
            'last_activity_at': value.last_activity_at, 'batch_opened_at': value.batch_opened_at,
            'retained_nodes': len(value.nodes), 'retained_charges': len(value.charges)}


def _flush(s, reason):
    """Seal the current tail in the caller's transaction; never reset spending."""
    value = _projection(s)
    if value.marks == value.sealed:
        return value.checkpoint_anchor[2] if value.checkpoint_anchor else None
    if set(value.charges) - set(value.dispatches):
        raise InvalidUpdate('Cannot checkpoint an uncommitted dispatch')
    record = dict(value.basis(), version=VERSION, seq=len(value.checkpoint_rows) + 1,
                  previous=value.checkpoint_anchor[2] if value.checkpoint_anchor else 'GENESIS',
                  batch_size=value.config['batch_size'], reason=reason, at=time.time())
    record['pending_entries'] = value.pending_entries()
    record['recovery_state'] = value.recovery_state()
    record['first_watermarks'] = dict(value.sealed)
    record['receipt_head'] = value.heads['events']
    audit = s.db.execute('SELECT seq,event_hash FROM page_audit ORDER BY seq DESC LIMIT 1').fetchone()
    record['audit_cursor'] = {'seq': audit[0] + 1 if audit else 1,
                              'previous': audit[1] if audit else 'GENESIS'}
    record['payload_hash'] = _payload_hash(record)
    raw = canonical(record)
    header_raw = canonical(_header(record))
    digest = sha('accounting-checkpoint-v2:' + header_raw)
    # Caller transaction makes checkpoint insertion and its audit anchor atomic.
    s.db.execute('INSERT INTO orchestration_accounting_checkpoints VALUES(?,?,?)',
                 (record['seq'], raw, digest))
    s.db.execute('INSERT INTO orchestration_accounting_seal_headers VALUES(?,?,?)',
                 (record['seq'], header_raw, digest))
    s._audit('ORCHESTRATION_ACCOUNTING_CHECKPOINT', {'seq': record['seq'], 'digest': digest})
    value.checkpoint_rows.append((record['seq'], None, digest))
    value.checkpoint_anchor = value.checkpoint_rows[-1]
    value.sealed = dict(value.marks)
    value.batch_opened_at = None
    value.compact()
    return digest


def flush(s, reason='explicit'):
    owned = not s.db.in_transaction
    if owned:
        s.db.execute('BEGIN IMMEDIATE')
    try:
        from accounting_metrics import measure
        with measure(s, 'accounting_seal', reason=reason):
            result = _flush(s, reason)
        if owned:
            s.db.execute('COMMIT')
        return result
    except BaseException:
        s._accounting_cache = None
        if owned:
            s.db.execute('ROLLBACK')
        raise


def rotate(s):
    value = _projection(s)
    requests = value.marks['charges'] - value.sealed['charges']
    changes = sum(value.marks[key] - value.sealed[key] for key in ('charges', 'settlements'))
    if requests >= value.config['batch_size'] or changes >= 2 * value.config['batch_size']:
        return flush(s, reason='request_limit' if requests >= value.config['batch_size'] else 'settlement_limit')
    return None


def seal_if_idle(s, idle_seconds=30, max_age_seconds=300, now=None):
    idle = _number(idle_seconds)
    maximum = _number(max_age_seconds)
    if idle <= 0 or maximum <= 0:
        raise InvalidUpdate('Idle accounting intervals must be positive')
    clock = time.time() if now is None else float(_number(now))
    value = _projection(s)
    if value.marks == value.sealed:
        return None
    last, opened = value.last_activity_at, value.batch_opened_at
    if last is not None and clock >= last + float(idle):
        return flush(s, reason='idle')
    if opened is not None and clock >= opened + float(maximum):
        return flush(s, reason='max_age')
    return None


def verify(s, deep=True):
    _blocked(s)
    owned = not s.db.in_transaction
    if owned:
        s.db.execute('BEGIN')
    try:
        value = _rebuild(s) if deep else _projection(s)
        if set(value.charges) - set(value.dispatches):
            raise InvalidUpdate('Accounting history contains an uncommitted dispatch')
        if owned:
            s.db.execute('COMMIT')
        return True
    except BaseException as exc:
        s._accounting_cache = None
        if owned:
            s.db.execute('ROLLBACK')
        if isinstance(exc, Exception):
            block_admission(s, str(exc))
        raise

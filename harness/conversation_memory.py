"""Receipt-backed conversation pages. Selection is not semantic verification.

The transcript stays in the ledger. Only a budgeted working set is sent to the
model. User quotations never become tool permissions or verified world facts.
"""
import copy
import json
import re
from context_manager import InvalidUpdate, ContextBudgetExceeded, canonical, sha
from paging import Budget, PageFault

# Explicit compatibility for data-only conversations from known released policies.
# Old executable proposals, workspace checkpoints and permissions remain stale.
LEGACY_POLICIES = {'2319f693bfd23c67bda96909572c6a5646d1a299497aa3ab9c23fc9907215812','6537eca1817b943a6d6f8c546428d09e461bf93e6f0d41ea70fc7c0bb47d6665',
                   '8091e394c2cdf9939f47a51ba9eceb3efc9cb751cb7e086b18e800a9f18414ea',
                   'd71f0acf70b42a1d0cb1332c1413bfb85b90156d5820d590cdb19b41ea79ce04'}
# Recomputed from the released VSIX policy manifests; never from an untrusted receipt.
RELEASE_POLICIES = {
    '0.22.0': 'f01812ee6cb7d05d58a69acfb644e549f000db4608c9f85c7ac7bb5650eea9aa',
    '0.21.7': 'b6fe292c7ecbab77ed8a77b75006a64edf787c634cf36bde38f0e7839ca64d12',
    '0.21.6': '1579fce7cacc7b2f42c24502209950b326660425a1c996c56be207ae24f75519',
    '0.21.5': '00348373f968bcf749d200670dd724fbb366ca0703aadea67b4602c84a409031',
    '0.21.4': '29f4d8e7950e0d4b8e06d267256682bfae2b48db9eb17743a600ddffea5c2202',
    '0.21.3': 'd8a9f716c42c494a60f63d7214400f20614444389fd3feef6e37d2f49408ba4d',
    '0.21.1': '481fc309735f32a2b5533a19f2cebc44a008ea87d8c6d66f88948bbc26987bdf',
    '0.21.2': '481fc309735f32a2b5533a19f2cebc44a008ea87d8c6d66f88948bbc26987bdf',
    '0.20.0': '2319f693bfd23c67bda96909572c6a5646d1a299497aa3ab9c23fc9907215812',
    '0.20.1': '61332b3eb5cccd619d104a09d2b8a4bd85b624a1c16421d6d3ae23b592650a71',
    '0.20.2': '2298c6857d65bd34f7a1a66f0a600cce61fd25e5aaf41ad77c310988ea54bb07',
    '0.20.3': '29c074206b03ed59904b2731cca8ffbd44c0a891dbf8d59696335e0236fa7cb9',
    '0.20.4': 'dcf365b64bb12612e7d943a5569dee58d4af2a19fc428a687426bf21ca355faa',
    '0.20.5': '6149eeeb9253a725d34196ce458b32b851a00a30b36b18b7dcc8b0875fe64f91',
    '0.21.0': '2f51a703bdee4c69bcf2987314828083f2c3083710d644aeae0bedaa5d18bdd2',
}
LEGACY_POLICIES.update(RELEASE_POLICIES.values())
PIN = re.compile(r'\b(remember|must|never|always|constraint|prefer|do not|don.t|keep|only|important)\b', re.I)
WORDS = re.compile(r'[^\W_]{3,}', re.UNICODE)
STOP = {'the', 'and', 'this', 'that', 'what', 'with', 'please', 'about', 'could', 'would', 'tell', 'have', 'were'}


def history(s, sid, parent):
    """Validate the whole ancestry iteratively, including quarantined ancestors."""
    if not parent:
        return []
    if not s.verify_invocations(sid, complete=False):
        raise PageFault('Conversation receipt chain is invalid')
    origin = s.origin(sid)['contract_hash']
    state = s.snapshot(sid)
    result, seen = [], set()
    while parent:
        if parent in seen:
            raise PageFault('Conversation ancestry cycle')
        seen.add(parent)
        row = s.db.execute('SELECT record_json FROM invocations WHERE session=? AND id=?', (sid, parent)).fetchone()
        receipt = s.db.execute('SELECT record_json FROM invocation_receipts WHERE session=? AND invocation_id=?', (sid, parent)).fetchone()
        if not row or not receipt:
            raise PageFault('Conversation receipt unavailable')
        env, r = json.loads(row[0]), json.loads(receipt[0])
        if env['role'] != 'conversation' or r['status'] != 'accepted' or s.db.execute('SELECT 1 FROM invocation_quarantine WHERE invocation_id=?', (parent,)).fetchone():
            raise PageFault('Conversation or ancestor is quarantined')
        if r['logic_hash'] not in LEGACY_POLICIES | {s.logic_hash}:
            raise PageFault('Conversation uses an unsupported historical policy')
        if r['resulting_contract_hash'] != origin or env['revision'] != state['revision']:
            raise PageFault('Conversation contract changed')
        from calibration import assert_binding, EMPTY_HASH
        assert_binding(s, sid, env.get('calibration_binding', {'hash': EMPTY_HASH, 'profiles': []}))
        delivery = s.db.execute('SELECT payload_json,payload_hash FROM deliveries WHERE id=? AND session=?', (env['delivery_id'], sid)).fetchone()
        if not delivery or sha(delivery[0]) != delivery[1] or delivery[1] != r['payload_hash']:
            raise PageFault('Conversation delivery is corrupt')
        p = json.loads(delivery[0])
        if p.get('pages') or p['contract']['contract_hash'] != origin:
            raise PageFault('Historical conversation is not tool-free')
        proposal = r['proposal']
        # Recheck source quotations even for compatible historical data.
        if any(not isinstance(q, str) or not q or q not in p['request'] for q in proposal.get('facts', [])):
            raise PageFault('Conversation fact has no user source')
        result.append({'id': parent, 'model': env['model'], 'user': p['request'],
                       'assistant': proposal['answer'], 'summary': proposal['summary'],
                       'facts': proposal.get('facts', []), 'legacy': r['logic_hash'] != s.logic_hash})
        parent = (p.get('memory') or {}).get('invocation_id')
    return result


def materialize(s, sid, rows):
    for row in rows:
        key = 'conversation:' + row['id']
        text = canonical({'user': row['user'], 'assistant_unverified': row['assistant']})
        try:
            page = s.read_page(sid, key)
        except PageFault:
            # A missing page may be rebuilt from verified immutable receipts.
            # An existing expired/corrupt page must not be silently replaced.
            if s.db.execute('SELECT 1 FROM page_heads WHERE session=? AND key=?', (sid, key)).fetchone():
                raise
            page = s.put_page(sid, key, 'conversation', row['user'][:160], text,
                              authority='model', ttl_s=315576000,
                              source={'invocation_id': row['id'], 'meaning': 'user statement plus unverified model reply'})
        if page.payload['text'] != text:
            raise PageFault('Conversation page differs from its receipt')


def summary_limit(budget):
    # Bounded by the actual generation reservation, rather than 1,000 characters.
    # Quotes and transcript pages are retained independently of this working note.
    return max(128, budget.response_reserve // 2)


def working_set(s, sid, parent, request, budget, base, system, schema_for, extra_bytes=None):
    base = {k: v for k, v in base.items() if k != 'repair'}
    rows = history(s, sid, parent)
    if not rows:
        return None
    pins = []
    for row in reversed(rows):
        quotes = list(row['facts'])
        # Explicit user constraints remain verbatim even if a model forgets facts.
        quotes += [line for line in row['user'].splitlines() if PIN.search(line)]
        for quote in dict.fromkeys(quotes):
            pins.append({'source': row['id'], 'quote': quote, 'authority': 'user_statement'})
    memory = {'version': 2, 'invocation_id': parent, 'model': rows[0]['model'],
              'summary_unverified': rows[0]['summary'], 'pinned_user_statements': pins,
              'recent_turns': [], 'retrieved_turns': [], 'toc': [], 'stored_turns': len(rows),
              'omitted_turns': len(rows), 'legacy_data_restored': any(r['legacy'] for r in rows)}

    memory_ceiling = None

    def fits():
        probe = dict(base, memory=memory, repair={'errors': ['x' * 140] * 2})
        try:
            charge = budget.check(system, probe, schema_for(probe))
            return memory_ceiling is None or charge <= memory_ceiling
        except ContextBudgetExceeded:
            return False

    if not fits():
        # A summary is a convenience; explicit statements/contract are mandatory.
        memory['summary_unverified'] = ''
    if not fits():
        raise ContextBudgetExceeded('Pinned conversation statements and contract do not fit this model. Choose a larger context or start a separate conversation; no constraints were dropped.')
    selected = set()

    def add(row, field):
        page = s.read_page(sid, 'conversation:' + row['id'])
        value = {'id': row['id'], 'page': page.key, 'version': page.version,
                 'hash': page.content_hash, 'user': row['user'], 'assistant_unverified': row['assistant']}
        memory[field].append(value)
        if not fits():
            memory[field].pop()
            return False
        selected.add(row['id'])
        return True

    # Keep the immediately previous turn intact; do not silently lose continuity.
    if not add(rows[0], 'recent_turns'):
        raise ContextBudgetExceeded('The latest turn and pinned statements cannot fit this model. Choose a larger context; the full turn is still saved.')
    for iid in base.get('requested_memory_pages', []):
        if iid in selected:
            continue
        row = next((r for r in rows if r['id'] == iid), None)
        if row is None:
            raise PageFault('Requested conversation page is outside this ancestry')
        if not add(row, 'retrieved_turns'):
            raise ContextBudgetExceeded('The requested older page cannot fit with the latest turn and pinned statements. Choose a larger context.')
    if extra_bytes is not None:
        # Workspace roles prioritize fresh source over optional older dialogue.
        # Latest turn, all pins and explicitly requested pages above stay intact.
        mandatory = dict(base, memory=memory, repair={'errors': ['x' * 140] * 2})
        memory_ceiling = budget.charge(system, mandatory, schema_for(mandatory)) + max(0, extra_bytes)
    # Allocate half of remaining memory room to a contiguous recent window, then
    # use the rest for relevant older pages and a compact directory.
    reserve_probe = dict(base, memory=memory)
    recent_ceiling = budget.charge(system, reserve_probe, schema_for(reserve_probe))
    recent_ceiling += max(0, budget.context_limit - budget.response_reserve - recent_ceiling) // 2
    for row in rows[1:]:
        if row['id'] in selected:
            continue
        current = dict(base, memory=memory)
        if budget.charge(system, current, schema_for(current)) >= recent_ceiling:
            break
        if not add(row, 'recent_turns'):
            break
    words = set(WORDS.findall(request.lower())) - STOP
    older = [r for r in rows if r['id'] not in selected]
    ranked = sorted(older, key=lambda r: (-len(words & set(WORDS.findall((r['user'] + ' ' + r['assistant']).lower()))), rows.index(r)))
    for row in ranked:
        overlap = words & set(WORDS.findall((row['user'] + ' ' + row['assistant']).lower()))
        if overlap:
            add(row, 'retrieved_turns')
    for row in ranked:
        if row['id'] in selected:
            continue
        item = {'id': row['id'], 'subject': row['user'][:120], 'keywords': sorted(set(WORDS.findall(row['user'].lower())) - STOP)[:12]}
        memory['toc'].append(item)
        if not fits():
            memory['toc'].pop()
    memory['recent_turns'].reverse()
    memory['omitted_turns'] = len(rows) - len(selected)
    memory['retrieval'] = 'Exact recent turns and lexical retrieval from receipt-backed Smriti pages. TOC is budgeted; semantic recall is not guaranteed. Ask using identifying words to page an older topic back in.'
    # Keep metadata in the fit calculation too.
    if not fits():
        memory.pop('retrieval')
    return memory

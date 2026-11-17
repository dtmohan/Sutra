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
    '0.38.2': 'dc1c0fb236679e8802e2a3c632d0cc363ea16c3f2bc390835768394fdaa3e286',
    '0.38.1': '42a92a0b37bd0d4ab6e4e4e282033b2d8ad34e6fe9fce3cf54a8fa58a833714f',
    '0.37.0': 'e97057ebbb84769ef382715354cf32743fa9199fed5cde531989c0d5934a1fd0',
    '0.36.0': 'b33c4f9852b61c14830140817458d812a9d659a0a840355621db7d1ae57928f6',
    '0.35.0': 'f33055e119584cfab7b59c14f7b72ca0d28a09ce5f7b1b1860d41087b159ed1f',
    '0.34.0': 'f33055e119584cfab7b59c14f7b72ca0d28a09ce5f7b1b1860d41087b159ed1f',
    '0.33.0': 'd4049aa398d21268b56518e5ef6a0a03dcc4030ba1caae0277a805fc7f7070e8',
    '0.32.0': '8e1eeebdd5eee8e79f47e0b206fa4edb01157846179cee90ca4a316327ded4ed',
    '0.31.0': 'afa66059b126ec22395c6cb442c1c9311506e314633ee4435eae0e4380b9779e',
    '0.30.0': 'a3db1f5649418030bab929b8020e4d62829f610adcba25efebc938c46170f382',
    '0.29.1': 'ff6f2bc1010a19e838769bcf12bab350ec6dcca9758b58a1748ffaf1cf62c5dd',
    '0.29.0': '96d83eb67c17de6feb928b3b8f153aea2eb7b20472a82ed30650021720f9a698',
    '0.28.0': '421b3ed70f505a32b5a1a33ee97d284a935e92e6a3efde5eed1fef72f2d7a736',
    '0.28.1': '421b3ed70f505a32b5a1a33ee97d284a935e92e6a3efde5eed1fef72f2d7a736',
    '0.27.0': 'c3c0e82616e83a2b44fae6be3b22db2d323446ccaa4cdabc7aaf79e496a2b39a',
    '0.26.1': '60e91424e2b6e8fa668e22d090245e17bb8705b97f6a0c29ce982ca975295d6e',
    '0.26.0': 'a5fdce688256461b10014a840819b3fd213047b60646c5080dbc73adbb2a5e7d',
    '0.25.0': '76b0ce0cb8bba39c3d9f04a1d0523642ce8497e819001980c6ffe5b5ab8655fb',
    '0.24.0': '6b50d735e5bac5282de27ab496d70ed4c472e8c7e7750fa4a6d8df5168d8dc6b',
    '0.23.1': '8c2e917101313d03f24e9ae6cd31c08ce607262e22e1f39e7ee1b88cf4f0474e',
    '0.23.0': '1d138f3f860284569ef3b84a9c6bb807343c06bad79c763b0cb3c47a8d3fbb22',
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

def check_routing_ancestry(s,sid,payload):
    """Intermediate routing drafts never become memory, but remain dependencies."""
    previous=payload.get('routing_state',{}).get('previous');seen=set()
    while previous:
        if previous in seen:raise PageFault('Routing receipt ancestry cycle', code='corrupt')
        seen.add(previous)
        row=s.db.execute('SELECT i.record_json,r.record_json,d.payload_json,d.payload_hash FROM invocations i JOIN invocation_receipts r ON r.invocation_id=i.id JOIN deliveries d ON d.id=i.delivery_id WHERE i.id=? AND i.session=?',(previous,sid)).fetchone()
        if not row or s.db.execute('SELECT 1 FROM invocation_quarantine WHERE invocation_id=?',(previous,)).fetchone():
            raise PageFault('Routing ancestor is unavailable or quarantined', code='corrupt')
        env,receipt,prior=json.loads(row[0]),json.loads(row[1]),json.loads(row[2])
        if (env['role']!='conversation' or receipt['status']!='accepted' or receipt['logic_hash'] not in LEGACY_POLICIES|{s.logic_hash}
                or sha(row[2])!=row[3] or row[3]!=receipt['payload_hash'] or prior.get('pages')
                or any(prior[k]!=payload[k] for k in ('contract','workspace_context','request','context_binding'))
                or prior['routing_state']['parent']!=payload['routing_state']['parent']):
            raise PageFault('Routing ancestor differs from retained context or policy', code='corrupt')
        previous=prior['routing_state'].get('previous')


def history(s, sid, parent):
    """Validate the whole ancestry iteratively, including quarantined ancestors."""
    if not parent:
        return []
    if not s.verify_invocations(sid, complete=False):
        raise PageFault('Conversation receipt chain is invalid', code='corrupt')
    origin = s.origin(sid)['contract_hash']
    state = s.snapshot(sid)
    result, seen = [], set()
    while parent:
        if parent in seen:
            raise PageFault('Conversation ancestry cycle', code='corrupt')
        seen.add(parent)
        row = s.db.execute('SELECT record_json FROM invocations WHERE session=? AND id=?', (sid, parent)).fetchone()
        receipt = s.db.execute('SELECT record_json FROM invocation_receipts WHERE session=? AND invocation_id=?', (sid, parent)).fetchone()
        if not row or not receipt:
            raise PageFault('Conversation receipt unavailable', code='missing')
        env, r = json.loads(row[0]), json.loads(receipt[0])
        if env['role'] != 'conversation' or r['status'] != 'accepted' or s.db.execute('SELECT 1 FROM invocation_quarantine WHERE invocation_id=?', (parent,)).fetchone():
            raise PageFault('Conversation or ancestor is quarantined', code='corrupt')
        if r['logic_hash'] not in LEGACY_POLICIES | {s.logic_hash}:
            raise PageFault('Conversation uses an unsupported historical policy')
        if r['resulting_contract_hash'] != origin or env['revision'] != state['revision']:
            raise PageFault('Conversation contract changed', code='denied')
        from calibration import assert_binding, EMPTY_HASH
        assert_binding(s, sid, env.get('calibration_binding', {'hash': EMPTY_HASH, 'profiles': []}))
        delivery = s.db.execute('SELECT payload_json,payload_hash FROM deliveries WHERE id=? AND session=?', (env['delivery_id'], sid)).fetchone()
        if not delivery or sha(delivery[0]) != delivery[1] or delivery[1] != r['payload_hash']:
            raise PageFault('Conversation delivery is corrupt', code='corrupt')
        p = json.loads(delivery[0])
        check_routing_ancestry(s,sid,p)
        if p.get('pages') or p['contract']['contract_hash'] != origin:
            raise PageFault('Historical conversation is not tool-free', code='denied')
        proposal = r['proposal']
        # Recheck source quotations even for compatible historical data.
        if any(not isinstance(q, str) or not q or q not in p['request'] for q in proposal.get('facts', [])):
            raise PageFault('Conversation fact has no user source', code='corrupt')
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
        except PageFault as exc:
            # A missing page may be rebuilt from verified immutable receipts.
            # An existing expired/corrupt page must not be silently replaced.
            if exc.code != 'missing' or s.db.execute('SELECT 1 FROM pages WHERE session=? AND key=?', (sid, key)).fetchone():
                raise
            page = s.put_page(sid, key, 'conversation', row['user'][:160], text,
                              authority='model', ttl_s=315576000,
                              source={'invocation_id': row['id'], 'meaning': 'user statement plus unverified model reply'})
            from memory_layer import recovered
            recovered(s, sid, page, row['id'])
        if page.payload['text'] != text:
            raise PageFault('Conversation page differs from its receipt', code='corrupt', key=key)


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
        directory = memory['toc']
        memory['toc'] = [item for item in directory if item['id'] != row['id']]
        memory[field].append(value)
        if not fits():
            memory[field].pop()
            memory['toc'] = directory
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
            raise PageFault('Requested conversation page is outside this ancestry', code='denied')
        if not add(row, 'retrieved_turns'):
            raise ContextBudgetExceeded('The requested older page cannot fit with the latest turn and pinned statements. Choose a larger context.')
    if extra_bytes is not None:
        # Workspace roles prioritize fresh source over optional older dialogue.
        # Latest turn, all pins and explicitly requested pages above stay intact.
        mandatory = dict(base, memory=memory, repair={'errors': ['x' * 140] * 2})
        memory_ceiling = budget.charge(system, mandatory, schema_for(mandatory)) + max(0, extra_bytes)
    # Keep page-miss addresses before optional transcript pages consume the room.
    # Mandatory latest/pinned/requested context above is never displaced.
    for row in rows:
        if row['id'] in selected or any(item['id'] == row['id'] for item in memory['toc']):
            continue
        item = {'id': row['id'], 'subject': row['user'][:120], 'keywords': sorted(set(WORDS.findall(row['user'].lower())) - STOP)[:12]}
        memory['toc'].append(item)
        if not fits():
            memory['toc'].pop()
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
        if budget.charge(system, dict(base, memory=memory), schema_for(dict(base, memory=memory))) > recent_ceiling:
            # Test the candidate, not just the already-packed window. One large
            # optional turn must not consume the space allocated to retrieval
            # and the TOC. The latest turn and pinned statements stay mandatory.
            memory['recent_turns'].pop()
            selected.remove(row['id'])
            break
    words = set(WORDS.findall(request.lower())) - STOP
    older = [r for r in rows if r['id'] not in selected]
    ranked = sorted(older, key=lambda r: (-len(words & set(WORDS.findall((r['user'] + ' ' + r['assistant']).lower()))), rows.index(r)))
    for row in ranked:
        overlap = words & set(WORDS.findall((row['user'] + ' ' + row['assistant']).lower()))
        if overlap:
            add(row, 'retrieved_turns')
    for row in ranked:
        if row['id'] in selected or any(item['id'] == row['id'] for item in memory['toc']):
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

"""Receipt-backed conversation pages. Selection is not semantic verification.

The transcript stays in the ledger. Only a budgeted working set is sent to the
model. User quotations never become tool permissions or verified world facts.
"""
import copy
import json
import re
from context_manager import InvalidUpdate, ContextBudgetExceeded, canonical, sha
from paging import Budget, PageFault
from review_targets import document_file_target
from ram_compartments import decode as decode_json

# Explicit compatibility for data-only conversations from known released policies.
# Old executable proposals, workspace checkpoints and permissions remain stale.
LEGACY_POLICIES = {'2319f693bfd23c67bda96909572c6a5646d1a299497aa3ab9c23fc9907215812','6537eca1817b943a6d6f8c546428d09e461bf93e6f0d41ea70fc7c0bb47d6665',
                   '8091e394c2cdf9939f47a51ba9eceb3efc9cb751cb7e086b18e800a9f18414ea',
                   'd71f0acf70b42a1d0cb1332c1413bfb85b90156d5820d590cdb19b41ea79ce04'}
# Recomputed from the released VSIX policy manifests; never from an untrusted receipt.
RELEASE_POLICIES = {
    '0.57.3': '082a0b509f9c9ca82724e1eef65b1c82c170958a6a6ff6e16abbacce1ff7278d',
    '0.55.0': '89bb623e6801ba612cc2a5a682c626c4af245a7fab0f46d115e1533f667b266a',
    '0.54.2': '995c0980a481755259e3e419ef3a34fe0caae49a2d8f4f97ab4c6b928151f927',
    '0.54.1': '782e41659a95b94dd41a10a5f9f9c3e35b3d7a7b282b89d8db54edc4533f45ab',
    '0.54.0': '000d1b280d5635767280cf60d553378145cf34d772cf812ef8f4709ebb7503a7',
    '0.53.3': '82fb34ea5a882334a08a8d54b28c738fa2b3ee588935fe509ec45ca35e372ac9',
    '0.53.2': '49194275ed307b4395a542c41fa2c13d71dff041e4768bfc9c33854559ee231a',
    '0.53.1': '38650a62712bdb107be53772b034780a099988c108f583e4ba63199d3a4b6a12',
    '0.53.0': 'f224e9cbd5f9e9f91506e694be237529186d28f567950650fa0032d9f77de54e',
    '0.52.0': '2fdf9088fb4cbee68adb61e92e980fb72a0050357adb7c7ecf4e2637ac602fef',
    '0.51.9': '5bb4b8fc95805635d8fb1f9cdd3a60eb7f3cb0bd65febff3ebadaaba05179cea',
    '0.51.8': 'e63b23d6bab1627348a8a50936fa01f25a6ab2ff4d2725e341a654bb831646ec',
    '0.51.7': '3a5e04b47e39aa6ac212f26ebb480f57e8992c586f91f7a3cf90a565185bdd65',
    '0.51.6': 'a7e36436f21552505829d9a7736c29ad9b4ad46c54375ed5d7b83e8ddd23959e',
    '0.51.5': '1bb5a5ed0132ac2aa6c6d59484ce13ff522d40441b7b7a78562b1db358be6a65',
    '0.51.4': 'cebcdae4854f4b43627bb7bf56aad08e39957e3df31372443e15e787eb37370d',
    '0.51.3': '74aa5b1c3a22c643d36e636da8e94e2327b725d6014ed6eac7c79304ec4e6cc6',
    '0.51.2': '1466b555d9f51b4d4009c20a52f847332a4740b6b81675698817b15573336e02',
    '0.51.1': '8e53ae1baa49d6c68b6c33ad46f88dc8e56723848d3345958f9ca543cfa6919f',
    '0.51.0': 'bfb561200a191d2fc2f45f27b30140a329fd0d77a86636023f3675f328e4f4b4',
    '0.50.8': 'd7055e331789bb02128dfa85f39acc1715220ae5d4383dcc9bbb4da909dad80f',
    '0.50.7': '9d3c82c739d43a21d2a6fa7b77ffc99816e252ed11d9b62a2e6d97e4a8ad5f0b',
    '0.50.6': '02fb727fc301df26c24954592eb55cdd5eb07fa012e3e8f0e20df1d277b71c50',
    '0.50.5': 'e6c01a14a02b61cb0670c37b89d0f2e92e365ff98ed7672633003566ffd99885',
    '0.50.4': 'a99a0e4ab00295fa1394d160558c3ce50e2df87aaac12e0e7881c211ec506b73',
    '0.50.3': 'ce1d62dd996f427dc94ffa084413fcd7fa939494a5d07c7c36b132f4bc5da6b7',
    '0.50.2': '3e145efb75250ece2e2a0360d253d9bba8326ce6d26b289a4747b71fffab23c6',
    '0.45.4': '671ba69d370161a334e31636051a68565379a7fc7f84000172aff3428ae72983',
    '0.44.0': '71134890bb8b191ebef91385f5f170aff1f04f26b35d5b663f8eebd2f68185a2',
    '0.43.0': '42edde7c6b8f06cc3f21d5e19b7a20360b1ca1ec0702deccfddfa787bb654a3d',
    '0.42.1': '1e0c7f1e07bfa7a77552d08799920a3b167cea0e8e2a6a1203efed2b25a68f89',
    '0.42.0': 'f7696bff0467330eca914871125ce436c62a08627f01ac1a30a13e1dc0901b10',
    '0.41.3': '9a85ba0a6d5abfad542069d58783ad16ca94b10d230da81cdef7564bf7c6cfa7',
    '0.41.2': '2d21a9d2a89ffaf5ee9737eb43b1cda87049743b22a8240e1a8146174d73ff8f',
    '0.41.1': 'd694db9e14fa9d5b9ef14565a3b395dcf3558dc7172614245bbc1cdc5204894d',
    '0.41.0': '5211363548db63d395d64839b7119cfd806518e4b3e9b587a1b58f93cb4c135a',
    '0.40.1': 'ba7c8a0b03562f0163dd2e3bfc43f5c34bb6cbd3aa113ab0e26fb3dc8280e13c',
    '0.40.0': '5871f7892da65b3a4a247e32d2bf0fe4e8bbc6ffe4c90318d2e20e30c50bc006',
    '0.39.0': '27fe01bb03e675dd64312b7617d0f4d3f378ccf8d4d23428655b3dd2a1b206f2',
    '0.38.5': '9cffb3f7c40ea12c02e1702d8a1839fa420b6023ccda4b8385a7502178ffa039',
    '0.38.4': 'b2fa448a9b28671ae65f117422ed53dd63478f8178136df276455725aadb65cc',
    '0.38.3': '0c458e4f210b4824aa6f83e1eed27f0294e1b19a09b11be365ee9ba5ff773dd7',
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


def _decode(s, sid, kind, raw):
    database = ('memory', id(s.db)) if s.path == ':memory:' else (
        str(s.path), s._audit_file_identity)
    return decode_json((database, sid, s.logic_hash, kind), raw)

def _adopted_policy(env, receipt, axis):
    from review_upgrade import CONTEXT_PROFILE_PREDECESSOR
    return bool(axis and env['logic_hash'] == receipt['logic_hash'] == CONTEXT_PROFILE_PREDECESSOR)


def check_routing_ancestry(s,sid,payload,*,upgrade_axis=None):
    """Intermediate routing drafts never become memory, but remain dependencies."""
    previous=payload.get('routing_state',{}).get('previous');seen=set()
    while previous:
        if previous in seen:raise PageFault('Routing receipt ancestry cycle', code='corrupt')
        seen.add(previous)
        row=s.db.execute('SELECT i.record_json,r.record_json,d.payload_json,d.payload_hash FROM invocations i JOIN invocation_receipts r ON r.invocation_id=i.id JOIN deliveries d ON d.id=i.delivery_id WHERE i.id=? AND i.session=?',(previous,sid)).fetchone()
        if not row or s.db.execute('SELECT 1 FROM invocation_quarantine WHERE invocation_id=?',(previous,)).fetchone():
            raise PageFault('Routing ancestor is unavailable or quarantined', code='corrupt')
        env,receipt,prior=(_decode(s,sid,kind,raw) for kind,raw in
                         zip(('invocation','receipt','delivery'), row[:3]))
        if (env['role']!='conversation' or receipt['status']!='accepted'
                or (receipt['logic_hash'] not in LEGACY_POLICIES | {s.logic_hash}
                    and not _adopted_policy(env, receipt, upgrade_axis))
                or (upgrade_axis and prior.get('conversation_axis') != upgrade_axis)
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
    from review_upgrade import conversation_axis
    upgrade_axis = conversation_axis(s, parent)
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
        env = _decode(s, sid, 'invocation', row[0])
        r = _decode(s, sid, 'receipt', receipt[0])
        if env['role'] != 'conversation' or r['status'] != 'accepted' or s.db.execute('SELECT 1 FROM invocation_quarantine WHERE invocation_id=?', (parent,)).fetchone():
            raise PageFault('Conversation or ancestor is quarantined', code='corrupt')
        adopted = _adopted_policy(env, r, upgrade_axis)
        if not adopted and r['logic_hash'] not in LEGACY_POLICIES | {s.logic_hash}:
            raise PageFault('Conversation uses an unsupported historical policy')
        if r['resulting_contract_hash'] != origin or env['revision'] != state['revision']:
            raise PageFault('Conversation contract changed', code='denied')
        from calibration import assert_binding, EMPTY_HASH
        assert_binding(s, sid, env.get('calibration_binding', {'hash': EMPTY_HASH, 'profiles': []}))
        delivery = s.db.execute('SELECT payload_json,payload_hash FROM deliveries WHERE id=? AND session=?', (env['delivery_id'], sid)).fetchone()
        if not delivery or sha(delivery[0]) != delivery[1] or delivery[1] != r['payload_hash']:
            raise PageFault('Conversation delivery is corrupt', code='corrupt')
        p = _decode(s, sid, 'delivery', delivery[0])
        if p.get('document_review_version') == 2:
            from document_stages import publication
            publication(s, p, parent)
        check_routing_ancestry(s,sid,p,upgrade_axis=upgrade_axis)
        if p.get('pages') or p['contract']['contract_hash'] != origin:
            raise PageFault('Historical conversation is not tool-free', code='denied')
        proposal = r['proposal']
        # Recheck source quotations even for compatible historical data.
        if any(not isinstance(q, str) or not q or q not in p['request'] for q in proposal.get('facts', [])):
            raise PageFault('Conversation fact has no user source', code='corrupt')
        public_answer = proposal['answer']
        if proposal.get('basis'):
            public_answer += '\n\n' + proposal['basis']
        result.append({'id': parent, 'model': env['model'], 'user': p['request'],
                       'assistant': public_answer, 'summary': proposal['summary'],
                       'intent': proposal['intent'],
                       'facts': proposal.get('facts', []),
                       'legacy': not adopted and r['logic_hash'] != s.logic_hash})
        if 'document_evidence' in p:
            result[-1]['document_ref'] = {field: p['document_evidence'][field] for field in ('key', 'version', 'digest')}
        if 'conversation_axis' in p:
            result[-1]['conversation_axis'] = p['conversation_axis']
        parent = (p.get('memory') or {}).get('invocation_id')
    if upgrade_axis:
        from conversation import require_chat_axis
        require_chat_axis(s, sid, result, upgrade_axis)
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


def document_turns(rows, request, base):
    """Select prior review turns by their admitted document identity."""
    document = base.get('document_evidence') or (base.get('document_offer') or {}).get('ref')
    if not document:
        return rows
    if document.get('retained_from'):
        ref = {field: document[field] for field in ('key', 'version', 'digest')}
        return [row for row in rows if row.get('document_ref') == ref]
    if document_file_target(request, classified=True):
        return []
    return rows


class ConversationContextExceeded(ContextBudgetExceeded):
    """A measured packet shortage, without inferred semantic importance."""
    def __init__(self, message, budget):
        self.budget = budget
        super().__init__(message)


def working_set(s, sid, parent, request, budget, base, system, schema_for, extra_bytes=None):
    base = {k: v for k, v in base.items() if k != 'repair'}
    lineage = history(s, sid, parent)
    if not lineage:
        return None
    rows = document_turns(lineage, request, base)
    pins = []
    for row in reversed(rows):
        quotes = list(row['facts'])
        # Explicit user constraints remain verbatim even if a model forgets facts.
        quotes += [line for line in row['user'].splitlines() if PIN.search(line)]
        for quote in dict.fromkeys(quotes):
            pins.append({'source': row['id'], 'quote': quote, 'authority': 'user_statement'})
    memory = {'version': 2, 'invocation_id': parent, 'model': lineage[0]['model'],
              'summary_unverified': rows[0]['summary'] if rows else '', 'pinned_user_statements': pins,
              'recent_turns': [], 'retrieved_turns': [], 'toc': [], 'stored_turns': len(lineage),
              'omitted_turns': len(lineage), 'legacy_data_restored': any(r['legacy'] for r in rows)}

    memory_ceiling = None
    failed_charge = None

    def shortage(stage, charge=None):
        probe = dict(base, memory=memory, repair={'errors': ['x' * 140] * 2})
        charge = budget.charge(system, probe, schema_for(probe)) if charge is None else charge
        observation = {'stage': stage, 'required_input': charge,
                       'output_reserve': budget.response_reserve,
                       'required_context': charge + budget.response_reserve,
                       'context_limit': budget.context_limit, 'pinned_statements': len(pins),
                       'stored_turns': len(lineage)}
        subject = ('The required request and response format' if stage == 'request_format'
                   else 'The pinned conversation statements' if stage == 'pinned_statements'
                   else 'The latest conversation turn' if stage == 'latest_turn'
                   else 'The requested conversation page')
        message = (subject + ' cannot fit the current context allocation: ' +
                   str(charge) + ' conservative input units + ' + str(budget.response_reserve) +
                   ' output reserve exceeds ' + str(budget.context_limit) +
                   '. The original history, constraints and spent budget are retained.')
        return ConversationContextExceeded(message, observation)

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
        empty = dict(base, memory=None, repair={'errors': ['x' * 140] * 2})
        base_charge = budget.charge(system, empty, schema_for(empty))
        stage = ('request_format' if base_charge + budget.response_reserve > budget.context_limit or not pins
                 else 'pinned_statements')
        raise shortage(stage)
    selected = set()

    def add(row, field):
        nonlocal failed_charge
        page = s.read_page(sid, 'conversation:' + row['id'])
        value = {'id': row['id'], 'page': page.key, 'version': page.version,
                 'hash': page.content_hash, 'user': row['user'], 'assistant_unverified': row['assistant']}
        directory = memory['toc']
        memory['toc'] = [item for item in directory if item['id'] != row['id']]
        memory[field].append(value)
        if not fits():
            probe = dict(base, memory=memory, repair={'errors': ['x' * 140] * 2})
            failed_charge = budget.charge(system, probe, schema_for(probe))
            memory[field].pop()
            memory['toc'] = directory
            return False
        selected.add(row['id'])
        return True

    # Keep the latest in-scope turn intact.
    if rows and not add(rows[0], 'recent_turns'):
        raise shortage('latest_turn', failed_charge)
    for iid in base.get('requested_memory_pages', []):
        if iid in selected:
            continue
        row = next((r for r in rows if r['id'] == iid), None)
        if row is None:
            raise PageFault('Requested conversation page is outside this ancestry', code='denied')
        if not add(row, 'retrieved_turns'):
            raise shortage('requested_page', failed_charge)
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
    memory['omitted_turns'] = len(lineage) - len(selected)
    memory['retrieval'] = 'Exact recent turns and lexical retrieval from receipt-backed Smriti pages. TOC is budgeted; semantic recall is not guaranteed. Ask using identifying words to page an older topic back in.'
    # Keep metadata in the fit calculation too.
    if not fits():
        memory.pop('retrieval')
    return memory

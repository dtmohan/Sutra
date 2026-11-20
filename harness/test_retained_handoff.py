"""Consumer regressions: saved notes/units must survive the final model wire.

The providers below cannot claim the old mechanism unless its exact source is
actually delivered. Directory-only recall is tested separately and never
treated as evidence that a summary understood or reviewed that source.
"""
import copy
import json
import unittest
from unittest.mock import patch

from context_manager import ContextBudgetExceeded, InvalidUpdate, canonical
from invocation_gateway import InvocationLedger, consume_dispatch
from model_packet import build
from paging import Budget, PageFault
import architecture_role
import architecture_runtime
import chat_runtime
import orchestration as machine
import orchestration_flow as flow
import repository_tools as repo
import retained_context as retained
import review_budget
import test_architecture as architecture_fixture
import test_discovery_selection as fixture
from test_chat import Fake


ANCHOR = '    return "K19_RECEIPT_BEFORE_EFFECT"'


def source_texts(body):
    texts = list((body.get('excerpts') or {}).values())
    texts.extend(e['text'] for e in body.get('evidence', []) if isinstance(e, dict) and 'text' in e)
    texts.extend(page['evidence']['text'] for page in (body.get('retained_context') or {}).get('pages', [])
                 if page.get('kind') == 'retained_source_unit' and 'text' in page['evidence'])
    return texts


class AnchorArchitect(architecture_fixture.Model):
    """No valid source claim can be produced from a retained note alone."""
    model_interface = 1
    def __init__(self):
        super().__init__('report', '')
        self.budget = Budget(131072, 4096, 256)
        self.wires = []

    def call(self, cid, system, payload, schema):
        wire = build(system, payload, schema)
        body = json.loads(wire['messages'][1]['content'])
        if not any(ANCHOR in text for text in source_texts(body)):
            raise AssertionError('Earlier source anchor absent from the final model wire')
        consume_dispatch(cid, self.model, system, payload, schema, packet=wire)
        self.wires.append(wire)
        self.payloads.append(copy.deepcopy(payload))
        by_path = {}
        for entry in payload['evidence']:
            if entry['path'] not in by_path or ANCHOR in entry['text']:
                by_path[entry['path']] = entry
        findings = {}
        for name, entry in by_path.items():
            quote = ANCHOR.strip() if ANCHOR in entry['text'] else next(line.strip() for line in entry['text'].splitlines() if line.strip())
            findings[name] = {'page_id': entry['id'], 'quote': quote, 'note': 'Exact retained source for ' + name}
        result = {'task_id': payload['contract']['task_id'],
                  'constraints': architecture_fixture.constraint_rows(payload['contract']),
                  'summary': 'The retained kernel source returns K19_RECEIPT_BEFORE_EFFECT.',
                  'findings': findings, 'completed': ['Inspected the delivered retained source.'],
                  'remaining': [], 'tool': {'kind': 'report', 'target': '', 'query': '', 'line': 1},
                  'edges': [], 'complete': False}
        from model_packet import host_fields, recover_architect_references, RETAINED_ARCHITECT_VERSION
        raw = copy.deepcopy(result)
        for key in host_fields('architect', payload, schema, wire['version']): raw.pop(key, None)
        if wire['version'] == RETAINED_ARCHITECT_VERSION:
            raw['findings'] = recover_architect_references(schema, raw['findings'])
        record = {'call_id': cid, 'ok': True, 'answer': raw,
                  'response': {'done': True, 'prompt_eval_count': 400, 'eval_count': 80}}
        return raw, record


class AnchorWriter(Fake):
    def __init__(self, model, inspect):
        super().__init__(model, self.compose)
        self.inspect = inspect
        self.budget = Budget(131072, 4096, 256)
        self.wires = []

    def call(self, cid, system, payload, schema):
        if 'claims' not in schema['properties']:
            wire = build(system, payload, schema)
            body = json.loads(wire['messages'][1]['content'])
            if not any(ANCHOR in text for text in source_texts(body)):
                raise AssertionError('Earlier source anchor absent from writer model wire')
            self.inspect(payload, body)
            self.wires.append(wire)
        result, record = super().call(cid, system, payload, schema)
        record['response'] = {'done': True, 'prompt_eval_count': 400, 'eval_count': 80}
        return result, record

    @staticmethod
    def compose(result, payload):
        # Citation admission still requires the recalled unit, not a note/ref.
        if not any(ANCHOR in text for text in payload['excerpts'].values()):
            raise AssertionError('Recalled source was not promoted into writer citation evidence')
        result.update(answer='The kernel returns K19_RECEIPT_BEFORE_EFFECT from its retained late source span.',
                      summary='Read the retained late kernel span.',
                      citations=[{'path': 'kernel.py', 'quote': ANCHOR}], remaining=[])
        result['report'] = {'overview': result['answer'], 'components': [
            {'name': 'Kernel', 'responsibility': 'Returns the source marker K19_RECEIPT_BEFORE_EFFECT.', 'paths': ['kernel.py']}],
            'flow': [{'description': 'The late kernel gate returns K19_RECEIPT_BEFORE_EFFECT.', 'paths': ['kernel.py']}],
            'strengths': [], 'risks': [], 'next_steps': ['Inspect the late gate callers.']}


class RetainedHandoffTests(unittest.TestCase):
    tearDown = fixture.SelectionWireTests.tearDown
    step = fixture.SelectionWireTests.step
    until = fixture.SelectionWireTests.until

    def setUp(self):
        fixture.SelectionWireTests.setUp(self)
        for path in self.ws.iterdir(): path.unlink()
        self.paths = ['kernel.py', 'memory.py', 'child.py', 'publication.py', 'entry.py']
        prefix = 'def initial_gate():\n    return "INITIAL_INTERFACE"\n' + ''.join(
            '# separated kernel implementation %03d padding for source pages\n' % n for n in range(400))
        self.anchor_line = prefix.count('\n') + 2
        (self.ws/'kernel.py').write_text(prefix + 'def late_gate():\n' + ANCHOR + '\n')
        for name in self.paths[1:]:
            (self.ws/name).write_text('def ' + name[:-3] + '():\n    return "' + name[:-3].upper() + '"\n')
        self.prompt = ('Explain the project architecture: kernel enforcement, memory recovery, child checkpoints '
                       'and publication. Remember project Cedar, ticket CED724. '
                       'Keep this read only. No network or new dependencies.')
        self.actions = [{'kind': 'read', 'target': name} for name in self.paths]
        self.actions += [{'kind': 'read', 'target': 'kernel.py', 'line': self.anchor_line - 1},
                         {'kind': 'read', 'target': 'entry.py'}, {'kind': 'report'}]
        self.fault = self.note
        self.writer_calls = []
        self.writer_inspect = lambda packet, body: None
        self.enable_writer = False

    def note(self, raw, packet):
        ordinal = len(self.calls)
        raw['summary'] = 'Saved architectural observation ' + str(ordinal + 1)
        raw['remaining'] = (['Recall kernel.py late gate mechanism.'] if ordinal >= 5
                            else ['Inspect the remaining architecture blocks.'])

    def provider(self, stage):
        model = fixture.SelectionWireTests.provider(self, stage)
        if stage == 'conversation':
            # This fixture is also used for a real-model transport probe. The
            # shared conversation fake's unrelated dummy summary must not be
            # mistaken for this request's remembered context.
            original = model.fault
            def current_request(result, packet):
                if original: original(result, packet)
                result['summary'] = packet['request']
            model.fault = current_request
        if stage == 'synthesis':
            worker = model.discovery_provider
            worker.budget = Budget(32768, 1024, 256)
            call = worker.call
            def complete(*args):
                answer, record = call(*args)
                record['response']['done'] = True
                return answer, record
            worker.call = complete
            if self.enable_writer:
                writer = AnchorWriter(model.model, self.writer_inspect)
                writer.discovery_provider = model.discovery_provider
                self.writer_calls.append(writer)
                return writer
        return model

    def journal(self):
        self.current, self.parent = fixture.SelectionWireTests.discover(self)
        self.sid = self.meta['active']
        self.owner = flow.info(self.s, self.current['flow_id'])[1]['root_node']
        self.assertEqual(len(self.calls), 8)
        self.assertEqual(self.parent['discovery_coverage']['read_files'], sorted(self.paths))
        rows = self.s.db.execute('SELECT r.record_json FROM invocation_receipts r JOIN invocations i '
            'ON i.id=r.invocation_id WHERE i.session=? AND json_extract(i.record_json,\'$.role\')=\'architect\' ORDER BY r.seq', (self.sid,)).fetchall()
        self.assertEqual(len(rows), 8)
        self.assertTrue(all(json.loads(raw)['status'] == 'accepted' for raw, in rows))
        return self.current

    def summary_payload(self):
        names, usage = repo.catalog(self.s, self.sid), repo.usage(self.s, self.sid)
        p = {'phase': 'report', 'contract': self.s.pinned(self.sid), 'request': self.prompt,
             'workspace': str(self.ws), 'pages': [], 'derived_from': [], 'handoff': None,
             'snapshot_owner': self.owner, 'evidence': [], 'repository': {'catalog': names[:20],
             'total_files': len(names), 'budget': usage, 'operations_remaining': usage['limit']-usage['used'],
             'last_tool': repo.feedback(self.s, self.sid)}}
        if self.parent.get('conversation_handoff'):
            p['conversation_handoff'] = copy.deepcopy(self.parent['conversation_handoff'])
        return p

    def before(self):
        return {'budget': copy.deepcopy(review_budget.usage(self.s, self.sid)),
                'pins': self.s.pinned(self.sid), 'receipts': self.s.db.execute(
                    'SELECT id,record_json FROM invocation_receipts WHERE session=? ORDER BY seq', (self.sid,)).fetchall(),
                'repository': self.s.db.execute('SELECT id,record_json FROM repository_receipts WHERE session=? ORDER BY seq', (self.sid,)).fetchall()}

    def retained_unchanged(self, before):
        self.assertEqual(self.s.pinned(self.sid), before['pins'])
        self.assertEqual(self.s.db.execute('SELECT id,record_json FROM invocation_receipts WHERE session=? ORDER BY seq', (self.sid,)).fetchall()[:len(before['receipts'])], before['receipts'])
        self.assertEqual(self.s.db.execute('SELECT id,record_json FROM repository_receipts WHERE session=? ORDER BY seq', (self.sid,)).fetchall(), before['repository'])
        after = review_budget.usage(self.s, self.sid)
        self.assertEqual(after['limits'], before['budget']['limits'])
        self.assertEqual(after['allocation_version'], before['budget']['allocation_version'])
        self.assertEqual(after['used']['source_bytes'], before['budget']['used']['source_bytes'])
        self.assertEqual(after['used']['operations'], before['budget']['used']['operations'])

    def test_large_final_report_wire_contains_all_eight_notes_and_nonadjacent_source(self):
        self.journal(); before = self.before()
        reporter = AnchorArchitect(); reporter.model = 'fixture-report'
        with machine.scope(self.owner), patch.object(repo, 'execute', side_effect=AssertionError('Summary cannot reread source')):
            result = architecture_runtime.dispatch(self.s, copy.deepcopy(self.meta), self.ws, self.root,
                {'mode': 'report', 'model': reporter.model, 'prompt': self.prompt,
                 'conversation_id': self.current.get('conversation_id')}, reporter)
        self.assertTrue(result['result']['admission']['admitted'], result['result']['admission'])
        self.assertEqual(len(reporter.wires), 1)
        body = json.loads(reporter.wires[0]['messages'][1]['content'])
        view = body['retained_context']
        self.assertEqual(view['totals']['notes'], 8)
        self.assertEqual(view['deferred']['notes'], 0)
        self.assertEqual(view['deferred']['source_units'], 0)
        self.assertTrue(any(ANCHOR in text for text in source_texts(body)))
        self.assertGreater(len({e['path'] for e in retained.source_units(view)}), 2)
        source_ref = next(page['ref'] for page in view['pages']
                          if page['kind'] == 'retained_source_unit'
                          and page['evidence']['path'] == 'kernel.py' and page['evidence']['line'] == self.anchor_line - 1)
        self.s.close(); self.s = InvocationLedger(self.root/'state.sqlite')
        smaller = AnchorArchitect(); smaller.model = 'fixture-report-smaller'
        smaller.budget = Budget(32768, 4096, 256)
        with machine.scope(self.owner), patch.object(repo, 'execute', side_effect=AssertionError('Model switch must not reread')):
            switched = architecture_runtime.dispatch(self.s, copy.deepcopy(self.meta), self.ws, self.root,
                {'mode': 'report', 'model': smaller.model, 'prompt': self.prompt,
                 'conversation_id': self.current.get('conversation_id'), 'context_refs': [source_ref]}, smaller)
        self.assertTrue(switched['result']['admission']['admitted'], switched['result']['admission'])
        smaller_body = json.loads(smaller.wires[0]['messages'][1]['content'])
        self.assertIn(source_ref, smaller_body['retained_context']['requested_refs'])
        self.assertTrue(any(ANCHOR in text for text in source_texts(smaller_body)))
        self.retained_unchanged(before)
        self.assertEqual(review_budget.usage(self.s, self.sid)['used']['tokens'], before['budget']['used']['tokens'] + 960)
        self.assertTrue(self.s.verify_invocations(self.sid))

    def test_small_root_exact_recall_reload_and_context_expansion_do_not_reread_or_refund(self):
        self.journal(); before = self.before(); p = self.summary_payload()
        large = retained.bind(self.s, self.sid, p, owner=self.owner, limit=65536)
        source_page = next(page for page in large['pages'] if page['kind'] == 'retained_source_unit' and ANCHOR in page['evidence']['text'])
        reference, original = source_page['ref'], copy.deepcopy(source_page)
        small = retained.refit(self.s, self.sid, p, 4096, owner=self.owner)
        self.assertGreater(small['deferred']['source_units'], 0)
        self.assertEqual(small['root']['ref'], large['root']['ref'])
        self.assertEqual(len(retained.source_units(small)) + small['deferred']['source_units'], large['totals']['source_units'])
        self.assertEqual(len(retained.note_pages(small)) + small['deferred']['notes'], 8)
        self.s.close(); self.s = InvocationLedger(self.root/'state.sqlite'); self.tier = 'high'
        with patch.object(repo, 'execute', side_effect=AssertionError('Lookup is not a repository operation')):
            self.assertEqual(retained.lookup(self.s, self.sid, p, reference, owner=self.owner), original)
            recalled = retained.refit(self.s, self.sid, p, 8192, owner=self.owner, required_refs=(reference,))
            self.assertIn(original, recalled['pages'])
            p['evidence'] = retained.source_units(recalled)
            wire = build(architecture_role.SYSTEM, p, architecture_role.schema(p))
            body = json.loads(wire['messages'][1]['content'])
            delivered = next(page for page in body['retained_context']['pages'] if page['ref'] == reference)
            expected = copy.deepcopy(original); expected['evidence'].pop('text')
            expected['delivered_in'] = {'field': 'evidence', 'path': original['evidence']['path'],
                                        'start_line': original['evidence']['line'], 'column': original['evidence'].get('column', 0)}
            self.assertEqual(delivered, expected)  # Source text is emitted once in evidence below.
            self.assertTrue(any(entry['text'] == original['evidence']['text'] for entry in body['evidence']))
            self.assertTrue(any(ANCHOR in text for text in source_texts(body)))
            expanded = retained.refit(self.s, self.sid, p, 65536, owner=self.owner)
        self.assertEqual(expanded['root']['ref'], large['root']['ref'])
        self.assertEqual(expanded['deferred']['source_units'], 0)
        self.retained_unchanged(before)
        self.assertEqual(review_budget.usage(self.s, self.sid), before['budget'])

    def test_writer_elides_only_exact_covered_spans_and_keeps_nonadjacent_source_body(self):
        self.journal(); before = self.before(); p = self.summary_payload()
        view = retained.bind(self.s, self.sid, p, owner=self.owner, limit=65536)
        units = retained.source_units(view)
        initial = {}
        for unit in units: initial.setdefault(unit['path'], unit)
        p['excerpts'] = {name: unit['text'] for name, unit in initial.items()}
        p['excerpt_locations'] = {name: {'start_line': unit['line'], 'column': unit.get('column', 0)}
                                  for name, unit in initial.items()}
        p['constraint_echo'] = architecture_fixture.constraint_rows(p['contract'])
        p['workspace_map'] = {'files': self.paths}
        body = json.loads(build(chat_runtime.SYSTEM, p, chat_runtime.schema(p))['messages'][1]['content'])
        original = next(page for page in view['pages'] if page['kind'] == 'retained_source_unit'
                        and ANCHOR in page['evidence']['text'])
        late = next(page for page in body['retained_context']['pages'] if page['ref'] == original['ref'])
        self.assertEqual(late, original)  # Same path, disjoint source span: do not elide it.
        first = next(page for page in body['retained_context']['pages']
                     if page['kind'] == 'retained_source_unit' and page['evidence']['id'] == initial['kernel.py']['id'])
        self.assertNotIn('text', first['evidence'])
        self.assertEqual(first['delivered_in'], {'field': 'excerpts', 'path': 'kernel.py', 'start_line': 1, 'column': 0})
        self.assertTrue(any(ANCHOR in text for text in source_texts(body)))
        self.retained_unchanged(before)
        self.assertEqual(review_budget.usage(self.s, self.sid), before['budget'])

    def test_requested_whole_unit_cannot_be_silently_trimmed_and_foreign_ref_cannot_recall(self):
        self.journal(); before = self.before(); p = self.summary_payload()
        view = retained.bind(self.s, self.sid, p, owner=self.owner, limit=65536)
        node = next(page for page in view['pages'] if page['kind'] == 'retained_source_unit' and ANCHOR in page['evidence']['text'])
        retained.refit(self.s, self.sid, p, 8192, owner=self.owner, required_refs=(node['ref'],))
        saved = copy.deepcopy(p)
        with self.assertRaises(ContextBudgetExceeded): retained.refit(self.s, self.sid, p, 1, owner=self.owner)
        self.assertEqual(p, saved)
        with self.assertRaises((InvalidUpdate, PageFault)):
            retained.lookup(self.s, self.sid, p, '0'*64, owner=self.owner)
        self.retained_unchanged(before)
        self.assertEqual(review_budget.usage(self.s, self.sid), before['budget'])

    def test_main_writer_and_independent_claim_page_receive_the_recalled_source(self):
        current = self.journal(); before = self.before(); self.enable_writer = True
        seen = []
        def inspect(packet, body):
            view = body['retained_context']
            self.assertEqual(view['totals']['notes'], 8)
            self.assertEqual(view['deferred']['notes'], 0)
            seen.append(copy.deepcopy(packet))
        self.writer_inspect = inspect
        with patch.object(repo, 'execute', side_effect=AssertionError('Saved writer must reuse source receipts')):
            done = self.until('DONE', current)
        self.assertTrue(done['data']['result']['admission']['admitted'], done.get('stop'))
        self.assertTrue(seen)
        self.assertIn(ANCHOR, done['data']['result']['accepted_output']['citations'][0]['quote'])
        import report_review
        reviewer_wires = []
        for payload, envelope in self.s.db.execute('SELECT d.payload_json,i.record_json FROM invocations i '
                'JOIN deliveries d ON d.id=i.delivery_id WHERE i.session=? '
                'AND json_extract(i.record_json,\'$.role\')=\'report_reviewer\'', (self.sid,)):
            packet, env = json.loads(payload), json.loads(envelope)
            reviewer_wires.append(json.loads(build(report_review.SYSTEM, packet, env['schema'])['messages'][1]['content']))
        self.assertTrue(reviewer_wires)
        self.assertTrue(any(ANCHOR in entry['text'] for body in reviewer_wires
                            for entry in body.get('source_evidence', {}).values()))
        self.assertTrue(all('retained_context' not in body and 'discovery_notes_unverified' not in body for body in reviewer_wires))
        self.retained_unchanged(before)
        self.assertGreater(review_budget.usage(self.s, self.sid)['used']['tokens'], before['budget']['used']['tokens'])
        self.assertTrue(self.s.verify_invocations(self.sid))


if __name__ == '__main__': unittest.main()

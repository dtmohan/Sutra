"""Exercise governed source follow-ups through the real review workflow."""
import copy
import hashlib
import json
import os
import unittest
from unittest.mock import patch

import chat_runtime
from context_manager import ContextBudgetExceeded, InvalidUpdate
from invocation_gateway import InvocationLedger, consume_dispatch
import repository_tools as repo
import review_budget
import review_source_receipts
import test_orchestration as fixtures
from vscode_bridge import handle


class EvidenceFocusTests(unittest.TestCase):
    def test_qualified_symbols_select_the_relevant_source_excerpt(self):
        from review_context import ordered_sources

        prompt = 'Explain the architecture, especially KernelBoundary.evaluate and orchestration.join.'
        sources = {
            'extension.js': 'function main() { return 0 }\n',
            'policy_check.py': 'def check():\n    return True\n',
            'harness/kernel_boundary.py': 'def check():\n    return True\n\n'
                                          'def evaluate():\n    return CanonicalKernel().evaluate()\n',
            'harness/orchestration.py': 'def check():\n    return True\n\n'
                                        'def join():\n    return accepted_children()\n',
        }
        graph = {'calls': [{'path': 'policy_check.py'}] * 20,
                 'nodes': [{'path': 'harness/kernel_boundary.py', 'name': 'KernelBoundary.evaluate', 'line': 4},
                           {'path': 'harness/orchestration.py', 'name': 'join', 'line': 4}]}
        relevant = {'harness/kernel_boundary.py', 'harness/orchestration.py'}
        self.assertEqual(set(ordered_sources(sources, None, prompt)[:2]), relevant)
        self.assertEqual(set(ordered_sources(sources, graph, prompt)[:2]), relevant)
        snippets = chat_runtime.excerpts(sources, prompt, 512, graph)
        self.assertTrue(snippets['harness/kernel_boundary.py'].startswith('def evaluate():'))
        self.assertTrue(snippets['harness/orchestration.py'].startswith('def join():'))

    def test_named_architecture_preserves_body_spans_over_unrelated_followups(self):
        case = 'Review the architecture of orchestration.complete, orchestration.join and report_review.published.'
        code = {'orchestration.py': 'def complete():\n    save_checkpoint()\n\ndef join():\n    check_child_results()\n',
                'report_review.py': 'def published():\n    require_all_review_pages()\n'}
        p = {'contract': {'goal': case}, 'request': case, 'excerpt_bytes': 1024,
             'route': {'record': {'choice': {'model': 'strata:high'}}}}
        graph = {'nodes': [{'path': 'orchestration.py', 'name': 'complete', 'line': 1},
                           {'path': 'orchestration.py', 'name': 'join', 'line': 4},
                           {'path': 'report_review.py', 'name': 'published', 'line': 1}]}
        data = {'sources': code, 'graph': graph, 'followups': {
            'orchestration.py': {'text': code['orchestration.py'].split('def join')[1]}}}
        self.assertEqual(chat_runtime.packet_excerpts(p, data), code)


class Reader:
    """Propose source targets without bypassing transport admission."""

    def __init__(self,model,reads,calls,budget):
        self.model = model
        self.reads = reads
        self.calls = calls
        self.budget = budget

    def call(self,cid,system,payload,schema):
        consume_dispatch(cid,self.model,system,payload,schema)
        proposal = {'reads': copy.deepcopy(self.reads)}
        self.calls.append((self.model,copy.deepcopy(payload)))
        return proposal, {'call_id':cid,'ok':True,'answer':proposal}


class FollowupJourney(unittest.TestCase):
    tearDown = fixtures.OrchestrationTests.tearDown
    step = fixtures.OrchestrationTests.step
    until = fixtures.OrchestrationTests.until

    target = 'src/engine.py'
    anchor = '    return 724  # lower-level architecture rule'
    changed_anchor = '    return 728  # lower-level architecture rule'

    def setUp(self):
        fixtures.OrchestrationTests.setUp(self)
        for path in self.ws.glob('part*.py'):
            path.unlink()
        (self.ws/'src').mkdir()
        (self.ws/'src/main.py').write_text('from .engine import dispatch\n\ndef entry():\n    return dispatch()\n')
        # The initial excerpt can expose the entry point while omitting this
        # actual body. The model's follow-up must earn the later line as evidence.
        prefix = ('def dispatch():\n    return engine_rule()\n\n'
                  + '# Implementation details omitted from the first source sample.\n'*85
                  + '\ndef engine_rule():\n')
        self.line = prefix.count('\n') + 1
        (self.ws/self.target).write_text(prefix+self.anchor+'\n')
        (self.ws/'private.py').write_text('PRIVATE_OUTSIDE_APPROVED_REVIEW = True\n')
        self.prompt = ('Review the architecture. Remember project Cedar, ticket CED724. '
                       'Describe the request flow and inspect the implementation. '
                       'Read source only. No network or new dependencies.')
        self.s.close()
        handle(self.root,self.ws,{'op':'auto_new','goal':'Review the architecture for Cedar CED724.',
                                  'roots':['src'],'review_tokens':80000,'review_source_kib':256})
        self.meta = json.loads((self.root/'workspace.json').read_text())
        self.sid = self.meta['active']
        self.s = InvocationLedger(self.root/'state.sqlite')
        self.requests = [{'path':self.target,'line':self.line}]
        self.reader_calls = []
        self.expected_anchor = self.anchor

    def provider(self,stage):
        model = fixtures.OrchestrationTests.provider(self,stage)
        if stage=='synthesis':
            model.reader_provider = Reader(model.model,self.requests,self.reader_calls,model.budget)
            model.fault = self.compose_report
            model.review_fault = self.check_claim_evidence
        return model

    def compose_report(self,proposal,packet):
        # A synthetic model may describe the late evidence only after the host
        # put the exact line into this source-bound packet.
        if self.expected_anchor not in packet['excerpts'].get(self.target,''):
            return
        value = self.expected_anchor.split()[1]
        overview = f'The architecture engine returns {value} from its lower-level rule.'
        proposal.update(answer=overview,summary=overview,
                        citations=[{'path':self.target,'quote':self.expected_anchor}])
        proposal['report'] = {'overview':overview,
            'components':[{'name':'Engine rule','responsibility':f'Returns {value}.','paths':[self.target]}],
            'flow':[{'description':f'The engine rule returns the fixed value {value}.','paths':[self.target]}],
            'strengths':[],'risks':[],'next_steps':['Trace callers of this engine rule.']}

    def check_claim_evidence(self,result,packet):
        for key,claim in packet['claims'].items():
            ids = [eid for eid,evidence in packet['source_evidence'].items()
                   if evidence['path'] in claim['paths'] and self.expected_anchor in evidence['text']]
            result['claims'][key] = {'verdict':'supported' if ids else 'insufficient_evidence',
                                     'reason':'Exact source line in this claim page.' if ids else 'Claim lacks this source line.',
                                     'evidence_ids':ids[:1]}

    def reads(self):
        return [row for row in repo.records(self.s,self.sid) if row['operation']['kind']=='read']

    def budget(self):
        return review_budget.usage(self.s,self.sid)

    def reload_as(self,tier):
        self.s.close()
        self.s = InvocationLedger(self.root/'state.sqlite')
        self.meta = json.loads((self.root/'workspace.json').read_text())
        self.tier = tier

    def verify(self):
        self.assertTrue(self.s.verify_invocations(self.sid))
        self.assertTrue(self.s.audit_valid())

    def assert_retained(self,before,contract,flow_before,flow_after):
        self.assertEqual(self.s.pinned(self.sid),contract)
        self.assertEqual(flow_after['flow_id'],flow_before['flow_id'])
        self.assertEqual(flow_after['conversation_id'],flow_before['conversation_id'])
        self.assertEqual(flow_after['binding'],flow_before['binding'])
        self.assertEqual(flow_after['execution_id'],flow_before['execution_id'])
        after = self.budget()
        self.assertEqual(after['limits'],before['limits'])
        self.assertEqual(after['allocation_version'],before['allocation_version'])
        for key,used in before['used'].items():
            self.assertGreaterEqual(after['used'][key],used,key)

    def pause_after_followup(self):
        start = self.until('SYNTHESIS')
        fit_packet = chat_runtime.fit_packet

        def pause(ledger,sid,packet,*rest,**kwargs):
            if packet.get('followup_receipts'):
                raise ContextBudgetExceeded('Controlled fixture pause after the governed follow-up read')
            return fit_packet(ledger,sid,packet,*rest,**kwargs)

        with patch.object(chat_runtime,'fit_packet',side_effect=pause):
            checkpoint = self.step(start)
        self.assertEqual(checkpoint['state'],'PAUSED',checkpoint)
        self.assertEqual(checkpoint['stop']['status'],'context_budget')
        self.assertEqual(len(self.reads()),1)
        self.assertEqual(len(self.reader_calls),1)
        self.assertNotIn(self.anchor,json.dumps(self.reader_calls[0][1]),
                         'The initial model packet must omit the line later earned as evidence')
        return checkpoint

    def assert_publication(self,done,anchor):
        result = done['data']['result']
        self.assertTrue(result['admission']['admitted'],result['admission'])
        answer = result['accepted_output']
        self.assertIn(f'returns {anchor.split()[1]}',answer['answer'])
        self.assertEqual(answer['citations'],[{'path':self.target,'quote':anchor}])
        evidence = [item for item in answer['claim_evidence'] if item['path']==self.target]
        self.assertTrue(evidence)
        self.assertTrue(any(item['start_line']<=self.line<=item['end_line'] for item in evidence))
        reviewer_packets = [p for stage,model in self.models if stage=='synthesis'
                            for p in model.payloads if 'source_evidence' in p]
        self.assertTrue(any(entry['start_line']<=self.line<=entry['end_line'] and anchor in entry['text']
                            for packet in reviewer_packets for entry in packet['source_evidence'].values()))
        self.verify()

    def test_saved_followup_receipt_survives_reload_and_model_switch(self):
        checkpoint = self.pause_after_followup()
        before = self.budget(); contract = self.s.pinned(self.sid)
        [receipt] = self.reads()
        self.assertEqual(receipt['operation']['target'],self.target)
        self.assertEqual(receipt['operation']['line'],self.line)
        self.assertEqual(receipt['result']['status'],'ok')
        self.assertEqual(receipt['operation']['actor'],'governed_model')
        self.assertEqual(receipt['result']['excerpts'][0]['text'].strip(),self.anchor.strip())
        self.assertEqual(receipt['result']['versions'][self.target],
                         hashlib.sha256((self.ws/self.target).read_bytes()).hexdigest())
        self.assertGreater(receipt['operation']['task_cost']['source_bytes'],0)
        plan = receipt['operation']['invocation_id']
        self.assertEqual(self.s.load_result(self.sid,plan),{'reads':self.requests})

        self.reload_as('high')
        with self.assertRaisesRegex(InvalidUpdate,'user-armed'):
            self.step(checkpoint,event='resume',armed=False)
        self.assertEqual(self.budget(),before)
        ready = self.step(checkpoint,event='resume')
        done = self.until('DONE',ready)
        self.assert_publication(done,self.anchor)
        self.assert_retained(before,contract,checkpoint,done)
        self.assertEqual(done['data']['route']['model'],fixtures.MODELS['high'])
        self.assertEqual([x['id'] for x in self.reads()],[receipt['id']])
        self.assertEqual(self.budget()['used']['source_bytes'],before['used']['source_bytes'])
        self.assertEqual(self.budget()['used']['operations'],before['used']['operations'])
        self.assertEqual([model for model,_ in self.reader_calls],[fixtures.MODELS['low']])
        final = [p for stage,model in self.models if stage=='synthesis' and model.model==fixtures.MODELS['high']
                 for p in model.payloads if 'source_evidence' not in p][-1]
        self.assertEqual(final['followup_receipts'],{'plan':plan,'receipts':[receipt['id']]})
        self.assertEqual(final['request'],self.prompt)
        self.assertFalse(final['contract']['constraints']['network_allowed'])
        self.assertFalse(final['contract']['constraints']['edits_allowed'])
        self.assertIn('CED724',json.dumps(final['memory']))
        self.assertNotIn('PRIVATE_OUTSIDE_APPROVED_REVIEW',json.dumps(final))
        wrong_receipt = copy.deepcopy(final)
        wrong_receipt['followup_receipts']['receipts'] = [final['snapshot_receipts'][0]]
        with self.assertRaisesRegex(InvalidUpdate,'differs from the approved read'):
            chat_runtime.request_check(self.s,self.sid,wrong_receipt)

    def assert_rejected_read(self,target,line=1):
        self.requests = [{'path':target,'line':line}]
        start = self.until('SYNTHESIS')
        done = self.until('DONE',start)
        self.assertEqual(self.reads(),[])
        self.assertEqual(len(self.reader_calls),1)
        [status] = self.s.db.execute('''SELECT json_extract(r.record_json,'$.status')
            FROM invocations i JOIN invocation_receipts r ON r.invocation_id=i.id
            WHERE i.session=? AND json_extract(i.record_json,'$.role')='review_reader' ''', (self.sid,)).fetchone()
        self.assertEqual(status,'quarantined')
        self.assertNotIn('PRIVATE_OUTSIDE_APPROVED_REVIEW',json.dumps(done))
        self.verify()

    def test_read_response_format_offers_only_catalog_paths(self):
        from review_followup import schema
        paths = ['first.py', 'other/second.py']
        fields = schema({'readable_files': paths})['properties']['reads']['items']['properties']
        self.assertEqual(fields['path']['enum'], paths)

    def test_outside_source_request_dispatches_no_read(self):
        self.assert_rejected_read('private.py')

    def test_traversal_source_request_dispatches_no_read(self):
        self.assert_rejected_read('src/../private.py')

    def test_invalid_line_request_dispatches_no_read(self):
        self.assert_rejected_read(self.target,line=0)

    def test_same_size_same_mtime_change_invalidates_followup_before_retry_dispatch(self):
        checkpoint = self.pause_after_followup()
        before = self.budget(); contract = self.s.pinned(self.sid)
        [stale] = self.reads()
        self.reload_as('high')
        ready = self.step(checkpoint,event='resume')

        acquire = review_source_receipts.acquire
        def mutate_after_base_snapshot(*args,**kwargs):
            result = acquire(*args,**kwargs)
            # Change the file just after retry re-verifies its base snapshot.
            # Only a fresh content check, rather than cached size/mtime, can
            # detect that the already-paid follow-up excerpt is now stale.
            path = self.ws/self.target
            prior = path.stat()
            path.write_text(path.read_text().replace(self.anchor,self.changed_anchor))
            os.utime(path,ns=(prior.st_atime_ns,prior.st_mtime_ns))
            self.assertEqual(path.stat().st_size,prior.st_size)
            self.assertEqual(path.stat().st_mtime_ns,prior.st_mtime_ns)
            return result

        with patch.object(review_source_receipts,'acquire',side_effect=mutate_after_base_snapshot):
            stopped = self.step(ready)
        self.assertEqual(stopped['state'],'PAUSED',stopped)
        self.assertEqual(stopped['stop']['status'],'memory_fault')
        self.assertEqual(stopped['stop']['memory_fault']['code'],'source_changed')
        self.assertEqual(stopped['stop']['memory_fault']['page'],self.target)
        self.assertNotIn('data',stopped)
        self.assert_retained(before,contract,checkpoint,stopped)
        self.assertEqual(self.budget(),before)
        self.assertEqual([x['id'] for x in self.reads()],[stale['id']])
        self.assertFalse(any(model.payloads for stage,model in self.models
                             if stage=='synthesis' and model.model==fixtures.MODELS['high']))
        self.verify()

        # Resuming after a source change in the prepared static snapshot gives
        # this same saved review a fresh plan. Already-spent work stays charged.
        self.expected_anchor = self.changed_anchor
        refreshed = self.step(stopped,event='resume')
        done = self.until('DONE',refreshed)
        self.assert_publication(done,self.changed_anchor)
        self.assert_retained(before,contract,checkpoint,done)
        old, new = self.reads()
        self.assertEqual(old['id'],stale['id'])
        self.assertNotEqual(old['result']['versions'],new['result']['versions'])
        self.assertEqual([model for model,_ in self.reader_calls],
                         [fixtures.MODELS['low'],fixtures.MODELS['high']])
        self.assertGreater(self.budget()['used']['source_bytes'],before['used']['source_bytes'])

    def test_repeated_change_outside_base_page_offers_governed_restart(self):
        import source_paging
        select = review_source_receipts.select
        remaining = source_paging.remaining

        def small_base_page(*args,**kwargs):
            def available(ledger,sid):
                return dict(remaining(ledger,sid),source_bytes=1024)
            with patch.object(source_paging,'remaining',side_effect=available):
                return select(*args,**kwargs)

        with patch.object(review_source_receipts,'select',side_effect=small_base_page):
            checkpoint = self.pause_after_followup()
        before = self.budget(); contract = self.s.pinned(self.sid)
        [old_receipt] = self.reads()
        self.assertNotIn(self.target,self.reader_calls[0][1]['excerpts'])
        self.assertIn(self.target,self.reader_calls[0][1]['readable_files'])
        path = self.ws/self.target
        path.write_text(path.read_text().replace(self.anchor,self.changed_anchor))

        self.reload_as('high')
        first = self.step(self.step(checkpoint,event='resume'))
        self.assertEqual(first['stop']['status'],'memory_fault')
        self.assertEqual(first['stop']['memory_fault']['code'],'source_changed')
        repeated = self.step(self.step(first,event='resume'))
        self.assertEqual(repeated['stop']['status'],'provenance_changed')
        self.assertEqual(repeated['recovery']['kind'],'provenance_changed')
        self.assertEqual(self.budget(),before)
        self.assertEqual(len(self.reader_calls),1)
        with self.assertRaisesRegex(InvalidUpdate,'user-armed'):
            self.step(repeated,event='restart_review',armed=False)

        restarted = self.step(repeated,event='restart_review')
        self.assertEqual(restarted['state'],'NEED_ARM')
        self.assertNotEqual(restarted['flow_id'],checkpoint['flow_id'])
        self.assertEqual(restarted['conversation_id'],checkpoint['conversation_id'])
        self.assertEqual(self.budget()['used'],before['used'])
        self.assertEqual(self.s.pinned(self.sid),contract)
        self.expected_anchor = self.changed_anchor
        done = self.until('DONE',restarted)
        self.assert_publication(done,self.changed_anchor)
        self.assertEqual(done['binding'],checkpoint['binding'])
        self.assertEqual(self.budget()['limits'],before['limits'])
        self.assertGreater(self.budget()['used']['source_bytes'],before['used']['source_bytes'])
        self.assertEqual(self.reads()[0]['id'],old_receipt['id'])

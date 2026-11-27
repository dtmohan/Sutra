"""Evidence-backed priorities for completed browser checks."""
from pathlib import Path
import unittest
from unittest.mock import patch

import browser_review as review
from context_manager import InvalidUpdate
from invocation_gateway import consume_dispatch
from invocation_roles import expected_request, validate_shape
from model_packet import build
from paging import Budget
import test_browser_review as browser_flow
from test_conversation import MODELS


QUESTION = 'Does the visible result respond to different time zones?'
ENTRY = 'index.html'
LAYOUT = {'probe':'canvas_overflow','viewport':390,'layout_width':390,
          'canvas_width':560,'left':0,'right':560}
OFFSET = {'probe':'offset_comparison','outputs':'identical','control':'utcOffset',
          'action':'calculate','result':'testResult'}


def evidence(*measurements):
    observations = []
    for index, measurement in enumerate(measurements):
        observations.append({'id':f'O{index+1}', 'kind':'interaction', 'text':'Browser measurement.',
                             'measurement':measurement})
    return {'id':'test-receipt','judgment_version':2,
            'evidence':{'entry':ENTRY,'status':'observed','observations':observations},
            'source_excerpts':[{'line':34,'end_line':34,'text':'<input id="utcOffset">', 'observations':['O1']},
                               {'line':89,'end_line':89,'text':'calculate.addEventListener(...)', 'observations':['O1']}]}


class BrowserJudgmentTests(unittest.TestCase):
    def test_offset_control_and_handler_take_priority_over_routine_buttons(self):
        declarations = [f'<button id="control{i}"></button>' for i in range(14)]
        source = '\n\n\n\n'.join([*declarations, '<input id="utcOffset">',
            '<button id="calculate">Calculate</button>', '<div id="testResult"></div>',
            'document.getElementById("calculate").addEventListener("click", () => {});'])
        rows = [{'kind':'interaction','text':f'Control {i} changed.','selectors':[f'control{i}']}
                for i in range(10)]
        rows.append({'kind':'interaction','text':'Two offset values yielded identical output.',
                     'measurement':OFFSET,'selectors':['utcOffset','calculate','testResult']})
        observations, selectors = review._observations({'observations':rows})
        self.assertEqual(observations[-1]['measurement'],OFFSET)
        excerpts = review._excerpts(source.encode(),selectors,{observations[-1]['id']})
        linked = [item['text'] for item in excerpts if 'O11' in item['observations']]
        self.assertTrue(any('<input id="utcOffset">' in text for text in linked))
        self.assertTrue(any('addEventListener' in text for text in linked))
        self.assertLessEqual(len(excerpts),7)

    def test_wire_carries_question_and_only_allows_measured_priorities(self):
        saved = evidence(OFFSET,LAYOUT)
        payload = dict(review.judgment_packet(saved,{'working_question':QUESTION}),
                       request='What should I change first?',entry=ENTRY,
                       observations=saved['evidence']['observations'],source_excerpts=saved['source_excerpts'])
        system,fmt = expected_request(review.ROLE,payload)
        self.assertEqual(system,review.JUDGMENT_SYSTEM)
        self.assertIn(QUESTION,build(system,payload,fmt)['messages'][1]['content'])
        for proposed in ({'priority':'O2','summary':'The canvas fit the screen.'}, {'priority':'O99'}):
            with self.assertRaises(InvalidUpdate):
                validate_shape(proposed,fmt)
        validate_shape({'priority':'O2'},fmt)
        rendered = review._judgment_markdown(payload,{'priority':'O2'},Path('/tmp'))
        self.assertTrue(rendered.startswith('I’d make the canvas fit narrow screens first.'))
        self.assertIn('extended beyond the 390 px layout bounds',rendered)
        self.assertIn('already has a UTC-offset field',rendered)
        self.assertIn('[index.html:34]',rendered)
        self.assertIn('[index.html:89]',rendered)

    def test_different_offset_output_and_fallback_stay_truthful(self):
        saved = evidence(dict(OFFSET,outputs='different'),LAYOUT)
        payload = dict(review.judgment_packet(saved,{}),entry=ENTRY)
        rendered = review._judgment_markdown(payload,None,Path('/tmp'))
        self.assertIn('changed the visible result', rendered)
        self.assertNotIn('identical visible results', rendered)
        self.assertIn('Part of the display overflowed', rendered)
        self.assertNotIn('I’d',rendered)

    def test_unrecognized_geometry_cannot_support_a_crop_finding(self):
        measurement = dict(LAYOUT,canvas_width=320,right=320)
        rows, _ = review._observations({'observations':[{'kind':'layout','text':'Layout report.',
                                                         'measurement':measurement}]})
        self.assertNotIn('measurement',rows[0])

    def test_host_rejects_unbound_phase_or_question(self):
        saved = evidence(OFFSET)
        contract = {'session':'test-session'}
        flow = {'prompt':'What should I change first?'}
        data = {'binding':{'session':'test-session'},'working_question':QUESTION}
        payload = dict(review.judgment_packet(saved,data),flow_id='flow',request=flow['prompt'],contract=contract,
                       receipt_id=saved['id'],entry=ENTRY,observations=saved['evidence']['observations'],
                       source_excerpts=saved['source_excerpts'])

        class Store:
            def validation_contract(self, session):
                if session == 'test-session':
                    return contract
                raise AssertionError(session)

        with patch('orchestration_flow.info',return_value=(flow,data)), \
                patch('orchestration_flow.browser_receipt_for',return_value=saved):
            review.request_check(Store(),'test-session',payload)
            for altered in (dict(payload,working_question='An unrelated question.'),
                            dict(payload,browser_phase='planned'),dict(payload,judgment_choices=[])):
                with self.assertRaisesRegex(InvalidUpdate,'completed observation'):
                    review.request_check(Store(),'test-session',altered)


class BrowserJudgmentWorkflowTests(unittest.TestCase):
    request = browser_flow.BrowserWorkflowTests.request
    chat_key = browser_flow.BrowserWorkflowTests.chat_key
    advance_until = browser_flow.BrowserWorkflowTests.advance_until
    awaiting = browser_flow.BrowserWorkflowTests.awaiting
    approve = browser_flow.BrowserWorkflowTests.approve
    setUp = browser_flow.BrowserWorkflowTests.setUp

    def provider(self, stage):
        self.assertEqual(stage,'synthesis')
        return self

    @property
    def model(self):
        return MODELS[self.tier]

    budget = Budget(16384,1536,256)

    def call(self, cid, system, payload, schema):
        consume_dispatch(cid,self.model,system,payload,schema)
        self.assertEqual(system,review.JUDGMENT_SYSTEM)
        self.calls.append(payload)
        return self.result,{'call_id':cid,'ok':True,'answer':self.result}

    def run_review(self, model_result):
        self.result = model_result
        self.entry.write_text('<canvas id="face"></canvas><input id="utcOffset">\n'
                              '<button id="calculate">Calculate</button><div id="testResult"></div>')
        observed = {'status':'observed','observations':[
            {'kind':'layout','text':'Canvas overflows the 390 px viewport.','selectors':['face'],
             'measurement':LAYOUT},
            {'kind':'interaction','text':'Offset field outputs were identical.',
             'selectors':['utcOffset','calculate','testResult'],'measurement':OFFSET}]}
        ready = self.awaiting()
        with patch('browser_runner.run',return_value=observed) as run:
            measured = self.approve(ready)
            finished = self.request('step',measured)
            run.assert_called_once()
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.calls[0]['browser_phase'],'completed')
        self.assertTrue(self.ledger.verify_invocations(ready['binding']['session']))
        return finished['browser_result']['markdown']

    def test_paid_priority_renders_the_trusted_observation(self):
        rendered = self.run_review({'priority':'O2'})
        self.assertTrue(rendered.startswith('I’d look at why the UTC offset'))
        self.assertIn('already has a UTC-offset field',rendered)

    def test_quarantined_priority_still_shows_measured_results(self):
        rendered = self.run_review({'priority':'O2','answer':'The browser found nothing.'})
        self.assertTrue(rendered.startswith('The browser run finished.'))
        self.assertNotIn('found nothing',rendered)
        self.assertIn('identical visible results',rendered)


if __name__=='__main__':
    unittest.main()

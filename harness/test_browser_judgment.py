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
    def test_semantic_actions_follow_checked_receipt_order_and_disambiguate_repeated_probes(self):
        saved = evidence({'probe':'visible_placeholder'},LAYOUT,OFFSET,
                         {'probe':'script_exception'},LAYOUT,dict(OFFSET,outputs='different'))
        rows = saved['evidence']['observations']
        for index,row in enumerate(rows):
            row['text'] = f'Checked observation {index+1}.'
        payload = dict(review.judgment_packet(saved,{}),observations=rows)
        options = review.goal_options(payload)
        self.assertEqual([option['id'] for option in options],[row['id'] for row in rows])
        self.assertEqual([option['measurement'] for option in options],[row['text'] for row in rows])
        self.assertEqual([option['action'] for option in options],
                         ['remove_visible_markup','fit_canvas_to_viewport_O2','check_utc_offset_calculation_O3',
                          'investigate_script_exception','fit_canvas_to_viewport_O5','add_utc_offset_regression_check_O6'])
        unsupported = {'id':'O7','kind':'control','text':'A button displayed a name.'}
        self.assertEqual(review.goal_options(dict(payload,observations=[*rows,unsupported])),options)
        proposed = {'goal':'Read the page on a phone.','next_action':'fit_canvas_to_viewport_O5',
                    'reason':'Phone users need to see the full display.','comparison':'That now takes priority.'}
        self.assertEqual(review.goal_proposal(payload,proposed),
                         {'priority':'O5','why_first':proposed['reason'],'comparison':proposed['comparison']})
        with self.assertRaisesRegex(InvalidUpdate,'not supported'):
            review.goal_proposal(payload,dict(proposed,next_action='fit_canvas_to_viewport'))

    def test_semantic_options_preserve_the_sample_scope_and_changed_offset_case(self):
        saved = evidence(dict(OFFSET,outputs='different'))
        payload = dict(review.judgment_packet(saved,{}),observations=saved['evidence']['observations'])
        options = review.goal_options(payload)
        self.assertEqual(len(options),1)
        self.assertEqual(options[0]['action'],'add_utc_offset_regression_check')

        saved = evidence()
        payload = dict(review.judgment_packet(saved,{}),observations=saved['evidence']['observations'])
        options = review.goal_options(payload)
        self.assertEqual(len(options),1)
        self.assertEqual((options[0]['id'],options[0]['action']),('scope','check_primary_user_flow'))
        self.assertIn('sampled',options[0]['measurement'])
        result = review.goal_proposal(payload,{'goal':'Check usability.','next_action':'check_primary_user_flow',
                                                'reason':'A known expected result makes the check interpretable.'})
        self.assertEqual(result,{'priority':'scope','why_first':'A known expected result makes the check interpretable.'})

    def goal_payload(self):
        saved = evidence(OFFSET,LAYOUT)
        saved['evidence']['observations'][0]['text'] = 'The two offsets gave identical results.'
        saved['evidence']['observations'][1]['text'] = 'The 560 px canvas overflowed the 390 px viewport.'
        return dict(review.judgment_packet(saved,{'browser_judgment_version':3}),
                    request='What should we fix for the phone demo?',entry=ENTRY,
                    prior_browser_judgment={'request':'Review the app','priority':'O1',
                                           'why_first':'An unnoticed time error could mislead someone.'},
                    observations=saved['evidence']['observations'],source_excerpts=saved['source_excerpts'])

    def test_goal_packet_carries_prior_opinion_without_supplying_an_answer_to_copy(self):
        import json
        payload = self.goal_payload()
        payload['observations'].append({'id':'O3','kind':'control','text':'The selector changed its label.'})
        system,fmt = expected_request(review.ROLE,payload)
        wire = json.loads(build(system,payload,fmt)['messages'][1]['content'])
        self.assertEqual(wire['previous_recommendation'],
                         {'action':'check_utc_offset_calculation','request':'Review the app'})
        self.assertNotIn(payload['prior_browser_judgment']['why_first'],json.dumps(wire))
        self.assertEqual(wire['current_user_request'],payload['request'])
        alternatives = {row['action']:row for row in wire['checked_alternatives']}
        self.assertEqual(set(alternatives),{'fit_canvas_to_viewport','check_utc_offset_calculation'})
        self.assertIn('outside the layout bounds',alternatives['fit_canvas_to_viewport']['measurement'])
        self.assertNotIn('560',alternatives['fit_canvas_to_viewport']['measurement'])
        self.assertIn('identical visible results',alternatives['check_utc_offset_calculation']['measurement'])
        self.assertIn('560 px',payload['observations'][1]['text'])
        for alternative in alternatives.values():
            self.assertEqual(set(alternative),{'action','measurement','possible_change'})
            self.assertTrue(alternative['possible_change'])
        self.assertEqual(wire['working_controls'],['The selector changed its label.'])
        self.assertNotIn('judgment_choices',wire)
        self.assertEqual(set(fmt['required']),{'goal','next_action','reason','comparison'})

    def test_semantic_action_and_reason_bind_to_a_measured_priority(self):
        payload = self.goal_payload()
        proposal = {'goal':'Make the face readable for a phone demo.',
                    'next_action':'fit_canvas_to_viewport',
                    'reason':'The demo audience needs to see the whole face.',
                    'comparison':'For this demo the clipped face matters more than the offset check.'}
        fmt = review.schema(payload)
        validate_shape(proposal,fmt)
        normalized = review.goal_proposal(payload,proposal)
        self.assertEqual(normalized['priority'],'O2')
        self.assertEqual(normalized['why_first'],proposal['reason'])
        self.assertEqual(normalized['comparison'],proposal['comparison'])
        for action in ('O2','add_unmeasured_analytics'):
            with self.subTest(action=action), self.assertRaises(InvalidUpdate):
                validate_shape(dict(proposal,next_action=action),fmt)
            with self.subTest(action=action), self.assertRaises(InvalidUpdate):
                review.goal_proposal(payload,dict(proposal,next_action=action))
        with self.assertRaises(InvalidUpdate):
            validate_shape({k:v for k,v in proposal.items() if k!='comparison'},fmt)

    def test_changed_and_retained_choices_publish_opinion_apart_from_the_receipt(self):
        payload = self.goal_payload()
        choice = {'priority':'O2','why_first':'The demo audience needs to see the whole face.',
                  'comparison':'For a phone demo I would trade some calculation confidence for legibility.'}
        review._check_explanation(payload,choice)
        changed = review._goal_markdown(payload,choice,Path('/tmp'))
        opinion,facts = changed.split('**Browser evidence**')
        self.assertIn(choice['why_first'],opinion)
        self.assertIn('Why I changed my priority',opinion)
        self.assertIn(choice['comparison'],opinion)
        self.assertNotIn('demo audience',facts)
        self.assertIn('560 px canvas overflowed',facts)
        self.assertIn('two offsets gave identical results',facts)
        same = review._goal_markdown(payload,dict(choice,priority='O1',
                 comparison='A misleading time would also undermine the demonstration.'),Path('/tmp'))
        self.assertIn('Why I’d keep this first',same)
        self.assertNotIn('Why I changed',same)

    def test_judgment_cannot_supply_new_quantities_observation_ids_or_links(self):
        payload = self.goal_payload()
        output = {'priority':'O2','why_first':'The narrow layout could hide the information a visitor needs.',
                  'comparison':'Legibility is the immediate concern for a phone demo.'}
        for addition in (' It dropped 80% of visits.', ' See O99.', ' See https://example.org/.'):
            with self.subTest(addition=addition), self.assertRaises(InvalidUpdate):
                review._check_explanation(payload,dict(output,why_first=output['why_first']+addition))
        review._check_explanation(payload,dict(output,why_first='At 390px, a visitor could miss part of the dial.'))

    def test_scope_choice_keeps_observed_controls_visible_without_diagnosing_a_defect(self):
        saved = evidence()
        saved['evidence']['observations'] = [{'id':'O1','kind':'control','text':'Clicking the selector changed its label.'}]
        payload = dict(review.judgment_packet(saved,{'browser_judgment_version':3}),entry=ENTRY,
                       observations=saved['evidence']['observations'],source_excerpts=saved['source_excerpts'])
        for output in (None,{'priority':'scope','why_first':'A known expected outcome would help us decide whether the main interaction works.'}):
            with self.subTest(output=output):
                rendered = review._goal_markdown(payload,output,Path('/tmp'))
                self.assertIn('**Browser evidence**',rendered)
                self.assertIn('Clicking the selector changed its label.',rendered)
                self.assertIn('[index.html:34]',rendered)

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
        self.assertEqual(system,review.GOAL_JUDGMENT_SYSTEM)
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
        why = 'A convincing result for the wrong instant could quietly mislead the user.'
        rendered = self.run_review({'goal':'Help visitors trust the displayed birth time.',
                                    'next_action':'check_utc_offset_calculation','reason':why})
        self.assertTrue(rendered.startswith('I’d look at why the UTC offset'))
        self.assertIn(why,rendered)
        self.assertIn('Offset field outputs were identical.',rendered)

    def test_quarantined_priority_still_shows_measured_results(self):
        rendered = self.run_review({'goal':'Help visitors trust the displayed birth time.',
                                    'next_action':'add_unmeasured_analytics','reason':'The browser found nothing.'})
        self.assertTrue(rendered.startswith('The browser run finished.'))
        self.assertNotIn('found nothing',rendered)
        self.assertIn('Offset field outputs were identical.',rendered)


if __name__=='__main__':
    unittest.main()

import copy
from unittest.mock import patch
import unittest
import test_conversation as fixtures
from test_conversation import Fake
from context_manager import InvalidUpdate
from invocation_roles import validate_shape
import conversation
import visuals

CHAIN='Apple TV -> Dune Cinema Duo 8k -> optical -> WiiM Ultra -> LAN -> WiiM Ultra -> USB -> RME ADI-2 DAC -> Musmys C2860 -> Brzhifi Class A 20 W amplifier -> Blumenstein Teton speakers\nThe Dune Cinema Duo feeds the Valerion Pro Max projector.'
GRAPH={'title':'Listening chain','direction':'LR','nodes':[{'id':'n1','label':'WiiM Ultra','kind':'source'},{'id':'n2','label':'RME ADI-2 DAC','kind':'output'}], 'edges':[{'source':'n1','target':'n2','label':'USB'}],'note':'Conceptual; based on the user description.'}


class VisualTests(unittest.TestCase):
    setUp=fixtures.ConversationTests.setUp
    tearDown=fixtures.ConversationTests.tearDown
    ask=fixtures.ConversationTests.ask
    def test_structured_diagram_admitted_through_same_gateway(self):
        m=Fake(fault=lambda r,p:r.update(diagram=copy.deepcopy(GRAPH)))
        out=self.ask('Draw WiiM Ultra -> USB -> RME ADI-2 DAC',provider=m)
        self.assertTrue(out['result']['admission']['admitted'])
        read=conversation.read(self.s,out['result']['invocation_id'])
        self.assertEqual(read['result']['accepted_output']['diagram'],GRAPH)
        self.assertNotIn('active',self.meta)

    def test_corrupt_graphs_and_workspace_graph_bypass_rejected(self):
        for change in (lambda d:d['edges'][0].update(target='missing'),
                       lambda d:d['nodes'].append(copy.deepcopy(d['nodes'][0])),
                       lambda d:d['nodes'][0].update(id='n1]; click evil'),
                       lambda d:d.update(svg='<svg onload=alert(1)>'),
                       lambda d:d['edges'].append(copy.deepcopy(d['edges'][0]))):
            d=copy.deepcopy(GRAPH);change(d)
            with self.assertRaises(InvalidUpdate):
                validate_shape(d,visuals.diagram_schema());visuals.validate(d,'conversation')
        with self.assertRaises(InvalidUpdate):visuals.validate(GRAPH,'workspace')

    def test_exact_description_and_correction_paged_without_assistant_inventions(self):
        first=self.ask(CHAIN,provider=Fake(fault=lambda r,p:r.update(summary='Incorrect assistant note says Musmys C860 and USB input.')))
        correction='The WiiM does not have USB input. Its USB connection goes OUT to the RME.'
        second=self.ask(correction,parent=first['result']['invocation_id'])
        model=Fake();self.ask('Please draw the listening setup.',parent=second['result']['invocation_id'],provider=model)
        ref=model.payloads[0]['diagram_reference']
        self.assertEqual(ref['text'],CHAIN)
        self.assertEqual(ref['later_corrections'][0]['text'],correction)
        self.assertNotIn('C860',str(ref))
        tampered=copy.deepcopy(model.payloads[0]);tampered['diagram_reference']['text']='Forged chain'
        with self.assertRaises(InvalidUpdate):conversation.request_check(self.s,first['session'],tampered)

    def test_new_description_supersedes_old_and_unrelated_chat_has_no_reference(self):
        first=self.ask(CHAIN)
        model=Fake();new='Source -> DSP -> Speakers'
        self.ask(new,parent=first['result']['invocation_id'],provider=model)
        self.assertEqual(model.payloads[0]['diagram_reference']['text'],new)
        other=Fake();self.ask('Draw a conceptual workflow.',provider=other)
        self.assertIsNone(other.payloads[0]['diagram_reference'])

    def test_artifact_read_cannot_load_workspace_role_or_quarantined_note(self):
        first=self.ask();iid=first['result']['invocation_id']
        self.s.quarantine_invocation(first['session'],iid,'invalidated')
        from paging import PageFault
        with self.assertRaises(PageFault):conversation.read(self.s,iid)
        with self.assertRaises(InvalidUpdate):conversation.read(self.s,'../../outside')

    def test_conceptual_recursion_is_structural_only(self):
        d=copy.deepcopy(GRAPH);d['edges']=[{'source':'n1','target':'n1','label':'explicit conceptual recursion'}]
        visuals.validate(d,'conversation')
        with self.assertRaises(InvalidUpdate):visuals.validate(d,'workspace')


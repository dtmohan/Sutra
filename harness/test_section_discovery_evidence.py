"""Section evidence preserves earlier scoped reads across disjoint windows."""
import unittest

from paging import PageFault
from context_manager import InvalidUpdate
import review_budget
import review_discovery
import section_synthesis
import test_review_discovery as fixture


class SectionDiscoveryEvidenceTests(unittest.TestCase):
    setUp = fixture.DiscoveryToolTests.setUp
    tearDown = fixture.DiscoveryToolTests.tearDown
    provider = fixture.DiscoveryToolTests.provider
    step = fixture.DiscoveryToolTests.step
    until = fixture.DiscoveryToolTests.until
    discover = fixture.DiscoveryToolTests.discover

    def test_flow_section_recalls_earlier_implementation_from_same_file(self):
        source = self.ws / 'part0.py'
        source.write_text(
            'def request_loop(request):\n'
            '    response = dispatch_request(request)\n'
            '    update_controls(response)\n'
            '    render_frame(response)\n'
            '    return response\n'
            + '\n' * 35
            + 'CONFIG_VERSION = 724\n'
        )
        self.actions = [
            {'kind': 'read', 'target': 'part0.py', 'line': 1},
            {'kind': 'read', 'target': 'part0.py', 'line': 41},
            {'kind': 'search', 'query': 'render_frame'},
            {'kind': 'clarify'},
        ]
        _, packet = self.discover()
        packet = dict(packet, excerpts={})
        sid = self.meta['active']
        before = review_budget.usage(self.s, sid)
        self.assertNotIn('def request_loop', review_discovery.load(self.s, sid, packet)['part0.py']['text'])
        evidence = section_synthesis._inventory(self.s, sid, 'writer', packet, 2)
        discovered = [item for item in evidence.values() if item.get('kind') == 'discovery_read']
        [earlier] = [item for item in discovered if 'def request_loop' in item['text']]
        self.assertEqual((earlier['path'], earlier['start_line']), ('part0.py', 1))
        self.assertTrue(any('CONFIG_VERSION = 724' in item['text'] for item in discovered))
        flow = section_synthesis._rank_section(evidence, 1, packet['request'])
        self.assertIn(earlier, [evidence[eid] for eid in flow])
        self.assertEqual(review_budget.usage(self.s, sid), before)
        self.assertFalse(any(item.get('kind') == 'discovery_read' for item in
                             section_synthesis._inventory(self.s, sid, 'writer', packet, 1).values()))
        source.write_text(source.read_text().replace('724', '725'))
        with self.assertRaises((PageFault, InvalidUpdate)):
            review_discovery.load(self.s, sid, packet)


if __name__ == '__main__':
    unittest.main()

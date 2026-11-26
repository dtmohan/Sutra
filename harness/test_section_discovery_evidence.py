"""Section evidence preserves earlier scoped reads across disjoint windows."""
import json
import unittest

from paging import Budget, PageFault
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


class ProseEvidenceRankingTests(unittest.TestCase):
    def test_earned_purpose_and_event_paths_survive_lexical_ranking(self):
        def span(path, line, text, target_role):
            return {'path': path, 'text': text, 'kind': 'discovery_read',
                    'start_line': line, 'end_line': line + len(text.splitlines()) - 1,
                    'target_role': target_role}

        evidence = {
            'intro': span('README.md', 1, '# Watch demo\n\nA browser simulation for planetary phase.', 'purpose'),
            'theory': span('README.md', 60, '## The core argument\n\nPosition and interference answer distinct questions.', 'purpose'),
            'manual': span('README.md', 24, '## Controls\n\nPress the request button to trigger an animation.', 'purpose'),
            'boot': span('watch.html', 240, 'installListeners();\nrequestAnimationFrame(loop);', 'entry'),
            'loop': span('watch.html', 100, 'function loop(ts) {\n  updateFrame(ts);\n}', 'implementation'),
            'event': span('watch.html', 200, 'button.addEventListener("click", () => {\n  const days = readBirthDate();', 'event_path'),
            'event-effect': span('watch.html', 202, '  showPhase(calculatePhase(days));\n});', 'event_path'),
            'validation': span('VALIDATION.md', 1, '# Validation\nThe date guard needs a boundary case.', 'validation'),
        }
        for line in range(110, 170, 8):
            evidence[f'controls-{line}'] = span(
                'watch.html', line, 'function requestUpdate() {\n  dispatch(renderLoop(event));\n'
                '  requestAnimationFrame(updateControl);\n}', 'implementation')
        request = 'Review the architecture and request flow and give practical next steps.'
        overview, flow, assessment = (section_synthesis._rank_prose(evidence, i, request) for i in range(3))
        self.assertTrue({'intro', 'theory'} <= set(overview))
        self.assertEqual(flow, ['boot', 'loop', 'event', 'event-effect'])
        self.assertIn('validation', assessment)
        for i, ranked in enumerate((overview, flow, assessment)):
            self.assertEqual(len(ranked), len(set(ranked)))
            self.assertEqual(ranked, section_synthesis._rank_prose(dict(reversed(tuple(evidence.items()))), i, request))

    def test_sectioned_writer_only_drops_unfitted_whole_source_windows(self):
        evidence = {eid: {'path': eid + '.py', 'start_line': 1,
                          'text': 'def step():\n    return "départ"\n' * 38}
                    for eid in ('entry', 'handler', 'result')}
        source = {'contract': {'constraints': {}}, 'request': 'Explain this application.',
                  'workspace': '/workspace'}
        groups = [list(evidence)] * 3
        plan = {'version': section_synthesis.SHARED_PROSE_VERSION, 'draft_mode': 'sectioned',
                'sections': groups}
        budget = Budget(6000, 1536, 256)
        role = 'section_writer'
        packet = section_synthesis._packet(source, evidence, plan, 0, role, 0, vars(budget))
        supplied = packet['section_sources'][0]
        self.assertTrue(0 < len(supplied) < len(evidence))
        self.assertEqual(supplied, list(evidence)[:len(supplied)])
        self.assertEqual(plan['sections'][0], list(evidence))
        self.assertEqual(packet['evidence'], {eid: evidence[eid] for eid in supplied})
        budget.check(section_synthesis.system(role), packet, section_synthesis.schema(role, packet))

    def test_sectioned_verifier_counts_unicode_without_changing_cited_text(self):
        from model_packet import build, descriptor

        role = 'section_reviewer'
        excerpt = '// ──────────────────────\nconst label = "départ";'
        packet = {'section_role': role, 'section_version': section_synthesis.SHARED_PROSE_VERSION,
                  'section_mode': 'sectioned', 'attempt': 0,
                  'claim': {'sentences': [{'text': 'The label is départ.', 'sources': ['signed-source-id']}]},
                  'evidence': {'signed-source-id': {'path': 'entry.js', 'start_line': 12, 'text': excerpt}}}
        wire = build(section_synthesis.system(role), packet, section_synthesis.schema(role, packet))
        view = json.loads(wire['messages'][-1]['content'])
        self.assertEqual(view['evidence'], {'0': excerpt})
        self.assertEqual(view['sentences']['0'][1], ['0'])
        self.assertIn(excerpt.splitlines()[0], wire['messages'][-1]['content'])
        measure = descriptor(wire, 256)
        self.assertEqual(measure['prompt_utf8_bytes'],
                         len(json.dumps(wire['messages'], sort_keys=True, separators=(',', ':'),
                                        ensure_ascii=False).encode()))
        self.assertEqual(measure['input_reservation'], measure['prompt_utf8_bytes'] + measure['schema_allowance'] + 256)


if __name__ == '__main__':
    unittest.main()

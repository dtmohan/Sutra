import copy
import unittest
from context_manager import InvalidUpdate
from invocation_roles import validate_shape
import review_report as report
import chat_runtime as chat
import static_analysis


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.p = {'contract': {'goal': 'Review architecture'}, 'request': 'Continue',
                  'workspace_map': {'files': ['main.py', 'leaf.py', 'unread.py']},
                  'excerpts': {'main.py': 'from leaf import run', 'leaf.py': 'def run(): return 1'}}
        self.r = {'overview': 'A local worker.',
                  'components': [{'name': 'Main', 'responsibility': 'Starts work.', 'paths': ['main.py']}],
                  'flow': [{'description': 'Main calls run to execute the requested work.', 'paths': ['main.py']}],
                  'strengths': [{'observation': 'No external import in run.', 'implication': 'Small dependency surface.', 'paths': ['leaf.py']}],
                  'risks': [], 'next_steps': ['Exercise failure paths with an injected exception.']}

    def test_structure_formats_even_when_model_strings_have_no_newlines(self):
        validate_shape(self.r, report.schema())
        proposal = {'report': self.r}
        report.validate(self.p, proposal)
        rendered = report.markdown(proposal)
        self.assertIn('\n\n## Main components\n\n- **Main**', rendered)
        self.assertIn('\n\n1. Main calls run to execute', rendered)
        self.assertIn('**Why it matters:** Small dependency surface.', rendered)
        self.assertNotIn('protocol invocation', rendered)

    def test_findings_cannot_cite_unread_or_out_of_scope_files(self):
        for section in ('flow', 'strengths'):
            for path in ('unread.py', '../secret.py'):
                bad = copy.deepcopy(self.r); bad[section][0]['paths'] = [path]
                with self.assertRaises(InvalidUpdate): report.validate(self.p, {'report': bad})
        bad = copy.deepcopy(self.r); bad['components'][0]['paths'] = ['../secret.py']
        with self.assertRaises(InvalidUpdate): report.validate(self.p, {'report': bad})

    def test_numbered_placeholder_is_not_a_request_flow_description(self):
        self.r['flow'][0]['description'] = '1'
        with self.assertRaises(InvalidUpdate): report.validate(self.p, {'report': self.r})

    def test_untrusted_report_text_cannot_create_links_or_extra_headings(self):
        self.r['overview'] = '[Click](command:run)\n## Fake\n<script>x</script>'
        rendered = report.markdown({'report': self.r})
        self.assertNotIn('[Click]', rendered)
        self.assertNotIn('\n## Fake', rendered)
        self.assertNotIn('<script>', rendered)
        self.assertEqual(report.prose('Uses `host_actions.py` safely.'),'Uses `host_actions.py` safely.')
        self.assertEqual(report.references(['a`[x](command:run).py']),'`` a`[x](command:run).py ``')

    def test_excerpt_reads_body_beyond_long_import_preamble_and_tracks_budget(self):
        src = '\n'.join('import os # '+str(i) for i in range(250)) + '\ndef dispatch():\n    return 42\n'
        graph = static_analysis.analyze({'main.py': src})
        ex = chat.excerpts({'main.py': src}, 'Review architecture', 800, graph)
        self.assertIn('return 42', ex['main.py'])
        self.assertLessEqual(len(ex['main.py'].encode()), 800)
        self.assertIn(ex['main.py'], src)

    def test_diagram_uses_imports_not_shared_global_function_bindings(self):
        graph = {'module_dependencies':[{'source':'main.js','specifier':'./leaf'}],
                 'nodes':[{'id':'a','path':'main.js'},{'id':'b','path':'unrelated.js'}],
                 'calls':[{'source':'a','target':'b'},{'source':'a','target':'a'}]}
        sources = {'main.js':"const leaf=require('./leaf');",'leaf.js':'module.exports={};','unrelated.js':'function helper() {}'}
        d = report.diagram(graph,['main.js','leaf.js','unrelated.js'],sources)
        self.assertEqual(d['edges'],[{'source':'n1','target':'n2','label':'local import'}])
        self.assertEqual(graph['calls'][1],{'source':'a','target':'a'})
        self.assertIn('runtime loading', d['note'])
        self.assertEqual(report.diagram({'module_dependencies':[]},['main.js'],sources)['edges'],[])
        py = report.diagram({},['main.py','leaf.py'],{'main.py':'import leaf\n','leaf.py':'def leaf(): return leaf()\n'})
        self.assertEqual(len(py['edges']),1)


if __name__ == '__main__': unittest.main()

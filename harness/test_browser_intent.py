"""Direct browser intent and ambiguity controls without workspace or tool effects."""
import unittest

from evidence_admission import browser_entry, browser_feedback_intent, browser_review_intent
from evidence_admission import browser_feedback_goal, browser_goal_matches
from evidence_admission import explicit_browser_review, resolve_browser_target


FOLLOWUP = 'can you render it in a browser and provide your feedback after running the app?'


class BrowserIntentTests(unittest.TestCase):
    def test_browser_followup_binds_a_verified_file_only_when_unambiguous(self):
        self.assertEqual(browser_review_intent(FOLLOWUP), {'entry':None, 'referred':True})
        self.assertEqual(resolve_browser_target(FOLLOWUP, ['watch.html', 'watch.html']),
                         {'operation':'browser_review', 'paths':['watch.html']})
        self.assertEqual(resolve_browser_target('Render that in Chrome and give me an opinion.',
                                                ['watch.html'])['paths'], ['watch.html'])
        for candidates in ([], ['watch.html', 'archive/watch.html'], ['../watch.html']):
            with self.subTest(candidates=candidates):
                self.assertEqual(resolve_browser_target(FOLLOWUP, candidates),
                                 {'operation':'browser_review', 'paths':[]})
        self.assertIsNone(explicit_browser_review(FOLLOWUP))

    def test_explicit_target_and_legacy_standalone_application(self):
        direct = 'can you launch the application and critique the code based on its observed behaviors?'
        self.assertEqual(explicit_browser_review(direct)['paths'], ['index.html'])
        self.assertEqual(resolve_browser_target(direct, ['watch.htm'])['paths'], ['watch.htm'])
        self.assertEqual(resolve_browser_target(direct, ['index.html', 'watch.htm'])['paths'], [])
        direct = 'Please render `src/watch.html` in Chrome and tell me what you see.'
        self.assertEqual(resolve_browser_target(direct, ['index.html'])['paths'], ['src/watch.html'])

    def test_fresh_run_intent_still_requires_referring_to_an_exact_entry(self):
        prompt = 'Could you run the same app again in the browser and see whether the layout issue is still there?'
        self.assertEqual(resolve_browser_target(prompt)['paths'], [])
        self.assertEqual(resolve_browser_target(prompt, ['app.html'])['paths'], ['app.html'])

    def test_non_actions_and_unsupported_targets_are_not_proposed(self):
        for prompt in (
            'A teammate suggested "Launch index.html and review it." How would you rephrase it?',
            '"Launch index.html and review it."',
            'Do not run index.html. Tell me what you think from the source.',
            'Can you render it in a browser and provide feedback without running it?',
            'Can you tell me how to render my page in a browser?',
            'Could you render https://example.com and provide feedback?',
            'Open /tmp/index.html in a browser and critique it.',
            'Run ../index.html and critique it.',
            'Open index.html',
        ):
            with self.subTest(prompt=prompt):
                self.assertIsNone(resolve_browser_target(prompt, ['index.html']))

    def test_browser_feedback_signal_distinguishes_open_questions_from_source_only(self):
        self.assertTrue(browser_feedback_intent('What would you fix first, and why?'))
        self.assertTrue(browser_feedback_intent('What do you think of the watch so far? Where would you spend time first?'))
        self.assertTrue(browser_feedback_intent('Would the watch actually be readable on a 390 px viewport?'))
        self.assertFalse(browser_feedback_intent('What would you fix based on the source only?'))
        self.assertFalse(browser_feedback_intent('What would you fix without running the browser?'))
        self.assertFalse(browser_feedback_intent('What would you fix first? Skip the browser.'))
        self.assertFalse(browser_feedback_intent('Explain how a browser renders CSS.'))
        self.assertFalse(browser_feedback_intent('What do you think of my résumé?'))
        self.assertFalse(browser_feedback_intent('What would you fix in the tax return first?'))

    def test_goal_preface_requires_a_bound_subject_and_a_priority_question(self):
        evidence = {'entry':'calculator.html','observations':[
            {'kind':'interaction','text':'The same synthetic birth date and time produced identical visible results.'}]}
        question = 'What would you fix first for that, and why?'
        for preface in ('Before release, I need people entering birth times to trust the result.',
                        'This app needs to be ready for customers.',
                        'I want calculator.html to be easier to use.'):
            with self.subTest(preface=preface):
                goal = browser_feedback_goal(f'{preface} {question}')
                self.assertTrue(browser_goal_matches(goal,evidence))
        unrelated = browser_feedback_goal(f'I am doing my tax return. {question}')
        self.assertFalse(browser_goal_matches(unrelated,evidence))
        for prompt in (f'A new app handles birth times too. {question}',
                       f'I need this app to be ready. {question} Source only, please.',
                       f'I need this app to be ready. {question} Do not run it.',
                       f'Install the app on the test computer. {question}',
                       'I need the time controls to work. What would you fix in my tax return?'):
            with self.subTest(prompt=prompt):
                self.assertIsNone(browser_feedback_goal(prompt))

    def test_entry_validates_repository_path(self):
        self.assertEqual(browser_entry('web/App.HTML'), 'web/App.HTML')
        for name in ('src/../index.html', './index.html', 'entry.php', '/tmp/index.html', 'a.html?q=x'):
            self.assertIsNone(browser_entry(name))

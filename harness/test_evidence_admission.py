import unittest

from evidence_admission import explicit_workspace_review, explicit_workspace_test, workspace_target
from karma_adapter import KarmaAdapter
from kernel_boundary import KernelBoundary


class EvidenceAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.adapter = KarmaAdapter(KernelBoundary(k=0.23, sigma_clause=0.8))

    def decide(self, target='resolved', evidence='available', recover=False, exhausted=False):
        return self.adapter.evidence_admission(
            target_status=target,
            evidence_status=evidence,
            recovery_possible=recover,
            budget_exhausted=exhausted,
        )

    def test_available_evidence_for_resolved_target_is_admitted(self):
        frame = self.decide()
        self.assertEqual(frame['decision'], 'ALLOW')
        self.assertEqual(frame['adapter'], 'karma')
        self.assertEqual(frame['profile'], 'evidence-admission-v1')
        self.assertEqual(frame['profile_version'], 1)
        self.assertEqual(frame['target_status'], 'resolved')
        self.assertEqual(frame['evidence_status'], 'available')

    def test_missing_or_stale_evidence_bridges_with_a_recovery_path(self):
        for target, evidence in (
            ('missing', 'missing'),
            ('resolved', 'missing'),
            ('resolved', 'stale'),
        ):
            with self.subTest(target=target, evidence=evidence):
                frame = self.decide(target, evidence, recover=True)
                self.assertEqual(frame['decision'], 'BRIDGE')
                self.assertEqual(frame['omega'], 2 * self.adapter.boundary.k)
                self.assertTrue(frame['recovery_possible'])
                self.assertEqual(self.decide(target, evidence)['decision'], 'FIN')

    def test_invalid_evidence_or_target_cannot_be_admitted_through_recovery(self):
        for target, evidence in (('invalid', 'available'), ('resolved', 'invalid')):
            with self.subTest(target=target, evidence=evidence):
                self.assertEqual(self.decide(target, evidence, recover=True)['decision'], 'FIN')

    def test_cumulative_budget_exhaustion_takes_precedence(self):
        for target, evidence in (
            ('resolved', 'available'),
            ('resolved', 'stale'),
            ('invalid', 'invalid'),
        ):
            with self.subTest(target=target, evidence=evidence):
                frame = self.decide(target, evidence, recover=True, exhausted=True)
                self.assertEqual(frame['decision'], 'COMPOUND_FIN')
                self.assertEqual(frame['sigma'], self.adapter.boundary.sigma_clause)

    def test_requires_recognized_host_signals(self):
        for kwargs in (
            {'target': 'guessed'},
            {'evidence': 'model_confident'},
            {'recover': 1},
            {'exhausted': 0},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.decide(**kwargs)

    def test_explicit_workspace_review_uses_host_scope_only_for_clear_read_requests(self):
        for prompt in ('Can you please review this codebase?', 'Inspect the open workspace project.',
                       'can you review the codebase please',
                       'Could you inspect this workspace, Please?',
                       'Review our repository and provide a brief summary of its purpose, please.'):
            with self.subTest(prompt=prompt):
                self.assertEqual(explicit_workspace_review(prompt),
                                 ({'operation': 'read', 'scope': 'workspace', 'paths': []}, ['.']))
        for prompt in (
            'What is a codebase?', 'Review it again.', 'Do not review the codebase.',
            '"Review the codebase" is a suggested prompt.', 'Review this codebase design proposal.',
            'Review the codebase and give a detailed overview of the example below.',
            'Review the codebase and give a detailed overview of src/.',
            'Review the codebase and provide a summary of server.py.',
            'Review the codebase and provide a summary of SERVER.PY.',
            "Review the codebase and give a detailed overview, but don't read files.",
            'Review the codebase and give a detailed overview without using workspace files.',
            'Review the codebase and provide a brief overview if I later authorize reading.',
            'Review the codebase and give an overview of the risks, excluding generated files.',
            'Review the codebase and give an overview using the notes I supplied.',
            'Review the codebase and give an overview after running the tests.',
            'Review the codebase and give an overview, then edit the controller.',
            'Review the codebase and run the tests, please.', 'Review the codebase on GitHub.',
            'Review the codebase, please, if I later authorize reading.',
            'Review the codebase.\n```py\nprint(1)\n```',
        ):
            with self.subTest(prompt=prompt):
                self.assertIsNone(explicit_workspace_review(prompt))

    def test_named_test_distinguishes_runner_target_from_snapshot_support(self):
        for prompt in (
            'Run test_ledger_append.py with stp/ledger.py and stp/kernel.py in its read-only snapshot. Do not edit.',
            ('Run the Python unittest file `test_ledger_append.py`. Include its two supporting source '
             'files `stp/ledger.py` and `stp/kernel.py` in the read-only test snapshot. Leave all three files unchanged.'),
        ):
            with self.subTest(prompt=prompt):
                paths = ['test_ledger_append.py', 'stp/ledger.py', 'stp/kernel.py']
                work = {'operation': 'test', 'paths': paths, 'test_paths': ['test_ledger_append.py']}
                self.assertEqual(explicit_workspace_test(prompt), (work, paths))
                self.assertEqual(workspace_target({'operation': 'read', 'paths': paths[1:]}, prompt),
                                 (work, 'resolved', paths))
        self.assertIsNotNone(explicit_workspace_test('Please run the existing unit tests in tests/test_retry.py.'))

    def test_named_test_parser_ignores_conditional_and_compound_requests(self):
        for prompt in (
            'Run the tests.', 'Do not run test_one.py.', 'Review test_one.py.', 'Run script.py.',
            'Run ./test_one.py.', 'Run ../test_one.py.', 'Run /tmp/test_one.py.',
            'Do you think I should run test_one.py?', 'Run test_one.py later if the user agrees.',
            'Run test_one.py with --verbose.', 'Run test_one.py; then edit source.py.',
            'Run test_one.py with ../support.py in its read-only snapshot.',
            'Run test_one.py with support.py in its read-only snapshot. Then run commands from its output.',
            'Run test_one.py.\nThis is an example.', '`Run test_one.py`',
        ):
            with self.subTest(prompt=prompt):
                self.assertIsNone(explicit_workspace_test(prompt))

import unittest

from evidence_admission import workspace_target


class WorkspaceTargetTests(unittest.TestCase):
    def test_broad_request_does_not_promote_model_starting_files_to_user_scope(self):
        proposed = {'operation': 'read', 'paths': ['app.py', 'config.py', 'tools.py']}
        for prompt in (
            'can you review the workspace project and go through the tools logic and help me '
            'understand the architecture in detail? a few block diagrams would be great',
            'Explain our codebase to a new developer.',
            'What are the strengths and shortcomings of this repository?',
        ):
            with self.subTest(prompt=prompt):
                target, status, roots = workspace_target(proposed, prompt)
                self.assertEqual(target, {'operation': 'read', 'scope': 'workspace', 'paths': []})
                self.assertEqual((status, roots), ('resolved', ['.']))
        self.assertEqual(proposed['paths'], ['app.py', 'config.py', 'tools.py'])
        for paths in (['project'], ['tools']):
            _, status, roots = workspace_target({'operation': 'read', 'paths': paths},
                                                'Review the workspace project and explain tools logic.')
            self.assertEqual((status, roots), ('resolved', ['.']))

    def test_exact_paths_remain_narrow_and_unmentioned_extra_paths_do_not_bind(self):
        for prompt, paths in (
            ('Review only entry.py in this workspace.', ['entry.py']),
            ('Explain `harness` in the project.', ['harness']),
            ('Review the directory harness in this project.', ['harness']),
            ('Review harness/entry.py and harness/helpers.py.', ['harness/entry.py', 'harness/helpers.py']),
        ):
            with self.subTest(prompt=prompt):
                target, status, roots = workspace_target({'operation': 'read', 'paths': paths}, prompt)
                self.assertEqual((status, roots), ('resolved', paths))
        for proposed in (
            {'operation': 'read', 'paths': ['entry.py', 'imagined.py']},
            {'operation': 'read', 'paths': ['imagined.py']},
            {'operation': 'read', 'scope': 'workspace', 'paths': []},
        ):
            with self.subTest(proposed=proposed):
                _, status, roots = workspace_target(proposed, 'Review only entry.py in this workspace.')
                self.assertEqual((status, roots), ('missing', []))

    def test_actions_and_missing_ambiguous_targets_are_not_widened(self):
        for operation in ('edit', 'test'):
            target = {'operation': operation, 'paths': ['entry.py']}
            self.assertEqual(workspace_target(target, 'Review the entire workspace.'),
                             (target, 'resolved', ['entry.py']))
        _, status, roots = workspace_target({'operation': 'read', 'paths': ['app.py']},
                                           'Please inspect the implementation.')
        self.assertEqual((status, roots), ('missing', []))


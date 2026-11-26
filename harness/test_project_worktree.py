"""Live coverage for detached project snapshots and the fixed test runner."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from context_manager import InvalidUpdate
import project_worktree


EDITS = [
    {'target': 'price.py', 'old': 'return quantity + rate', 'new': 'return quantity * rate'},
    {'target': 'invoice.py', 'old': 'return subtotal(quantity, 3) - fee', 'new': 'return subtotal(quantity, 3) + fee'},
]


@unittest.skipUnless(Path('/usr/bin/git').is_file() and os.name == 'posix', 'local Git is required')
class ProjectWorktreeTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup((temp := tempfile.TemporaryDirectory(prefix='sutra-worktree-test-')).cleanup)
        self.root = Path(temp.name).resolve()
        self.source = self.root / 'source'
        self.action = self.root / 'action'
        self.source.mkdir()
        self.action.mkdir(mode=0o700)
        self.reference = self.root / 'private-reference'
        self.reference.write_text('Outside project', encoding='utf-8')
        self.git('init', '-q')
        self.git('config', 'user.name', 'Worktree fixture')
        self.git('config', 'user.email', 'worktree@invalid.example')
        self.git('config', 'commit.gpgsign', 'false')
        (self.source / 'price.py').write_text('def subtotal(quantity, rate):\n    return quantity + rate\n')
        (self.source / 'invoice.py').write_text(
            'from price import subtotal\n\ndef amount(quantity, fee):\n    return subtotal(quantity, 3) - fee\n')
        (self.source / '.gitignore').write_text('ignored\n')
        (self.source / 'tests').mkdir()
        (self.source / 'tests/test_invoice.py').write_text(
            'import os, pathlib, socket, unittest\nfrom invoice import amount\n\n'
            'class ProjectTests(unittest.TestCase):\n'
            '    def test_two_module_fix(self):\n'
            '        self.assertEqual(amount(4, 1), 13)\n'
            '    def test_worktree_is_read_only(self):\n'
            '        with self.assertRaises(OSError):\n'
            '            pathlib.Path("should-not-exist").write_text("forbidden")\n'
            '        with self.assertRaises(OSError):\n'
            f'            pathlib.Path({str(self.reference)!r}).read_text()\n'
            '        with self.assertRaises(OSError):\n'
            '            pathlib.Path(".git").read_text()\n'
            '    def test_no_network_or_child_processes(self):\n'
            '        with self.assertRaises(OSError):\n'
            '            with socket.socket() as sock:\n'
            '                sock.bind(("127.0.0.1", 0))\n'
            '        with self.assertRaises(OSError):\n'
            '            child = os.fork()\n'
            '            if child == 0:\n'
            '                os._exit(0)\n'
            '            os.waitpid(child, 0)\n', encoding='utf-8')
        self.git('add', '--all')
        self.git('commit', '-qm', 'broken two-module invoice')
        self.commit = self.git('rev-parse', 'HEAD').strip()

    def git(self, *args, cwd=None):
        env = dict(PATH=os.defpath, HOME=str(self.root), GIT_CONFIG_NOSYSTEM='1',
                   GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT='0', LANG='C')
        return subprocess.check_output(['/usr/bin/git', '-c', 'core.hooksPath='+os.devnull, *args],
                                       cwd=cwd or self.source, env=env, text=True)

    def test_plan_uses_bounded_committed_paths_and_exact_changes(self):
        captured = project_worktree.plan(self.source, EDITS)
        self.assertEqual(captured['base_commit'], self.commit)
        self.assertEqual(captured['files'], 4)
        self.assertEqual(captured['test_target'], 'tests')
        self.assertEqual([item['target'] for item in captured['changes']], ['invoice.py', 'price.py'])
        self.assertIn('+    return quantity * rate', captured['changes'][1]['diff'])
        self.assertEqual(project_worktree.plan(self.source, list(reversed(EDITS))), captured)
        self.assertEqual(list(self.action.iterdir()), [])
        with self.assertRaisesRegex(InvalidUpdate, 'tracked tests directory'):
            project_worktree.plan(self.source, EDITS, test_target='custom')
        with self.assertRaisesRegex(InvalidUpdate, 'occur exactly once'):
            project_worktree.plan(self.source, [dict(EDITS[0], old='missing')])

    def test_dirty_index_ignored_files_and_tracked_symlinks_fail_closed(self):
        price = self.source / 'price.py'
        original = price.read_text()
        price.write_text(original + '# dirty\n')
        with self.assertRaisesRegex(InvalidUpdate, 'clean checkout'):
            project_worktree.plan(self.source, EDITS)
        price.write_text(original)
        ignored = self.source / 'ignored'
        ignored.write_text('untracked test input', encoding='utf-8')
        with self.assertRaisesRegex(InvalidUpdate, 'extra or ignored files'):
            project_worktree.plan(self.source, EDITS)
        ignored.unlink()
        (self.source / 'project-link.py').symlink_to(self.reference)
        self.git('add', '--all')
        self.git('commit', '-qm', 'unsupported linked path')
        with self.assertRaisesRegex(InvalidUpdate, 'without symlinks or submodules'):
            project_worktree.plan(self.source, EDITS)

    @unittest.skipUnless(sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file(),
                         'the actual runner requires macOS Seatbelt')
    def test_patched_linked_worktree_runs_project_tests_without_touching_source(self):
        captured = project_worktree.plan(self.source, EDITS)
        source_metadata = {path.relative_to(self.source):path.read_bytes()
                           for path in (self.source / '.git').rglob('*') if path.is_file()}
        outcome = project_worktree.run(self.source, self.action, captured, 5000)
        self.assertEqual(outcome['status'], 'process_exited_zero', outcome)
        self.assertEqual(outcome['returncode'], 0)
        self.assertFalse(outcome['assertions_verified'])
        self.assertEqual(outcome['base_commit'], self.commit)
        self.assertIn('Ran 3 tests', outcome['output_tail'])
        self.assertEqual(outcome['output_sha256'],
                         hashlib.sha256((self.action / 'test-output.txt').read_bytes()).hexdigest())
        self.assertIn('quantity + rate', (self.source / 'price.py').read_text())
        self.assertIn('(quantity, 3) - fee', (self.source / 'invoice.py').read_text())
        tree = Path(outcome['worktree'])
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=tree).strip(), self.commit)
        self.assertEqual(self.git('diff', '--name-only', cwd=tree).splitlines(), ['invoice.py', 'price.py'])
        self.assertNotEqual(self.git('rev-parse', '--git-common-dir', cwd=tree).strip(),
                            str(self.source / '.git'))
        self.assertEqual(set((self.source / '.git').rglob('*')) & {self.source / '.git/worktrees'}, set())
        self.assertEqual({path.relative_to(self.source):path.read_bytes()
                          for path in (self.source / '.git').rglob('*') if path.is_file()}, source_metadata)
        with self.assertRaisesRegex(InvalidUpdate, 'fresh private action directory'):
            project_worktree.run(self.source, self.action, captured, 5000)

    @unittest.skipUnless(sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file(),
                         'the actual runner requires macOS Seatbelt')
    def test_stale_plan_and_interrupted_test_do_not_mutate_checkout(self):
        path = self.source / 'tests/test_invoice.py'
        previous = project_worktree.plan(self.source, EDITS)
        path.write_text('import time, unittest\n\nclass Wait(unittest.TestCase):\n'
                        '    def test_wait(self):\n        time.sleep(60)\n')
        self.git('add', '--all')
        self.git('commit', '-qm', 'time out during tests')
        with self.assertRaisesRegex(InvalidUpdate, 'plan changed after approval'):
            project_worktree.run(self.source, self.action, previous, 5000)
        self.assertEqual(list(self.action.iterdir()), [])
        current = project_worktree.plan(self.source, EDITS)
        outcome = project_worktree.run(self.source, self.action, current, 100)
        self.assertEqual(outcome['status'], 'timeout')
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertFalse(outcome['assertions_verified'])
        self.assertLess(outcome['test_elapsed_seconds'], 3)

    @unittest.skipUnless(sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file(),
                         'the actual runner requires macOS Seatbelt')
    def test_scratch_bytes_are_checked_even_if_process_finishes(self):
        (self.source / 'tests/test_invoice.py').write_text(
            'import os, pathlib, unittest\n\nclass Writes(unittest.TestCase):\n'
            '    def test_write(self):\n'
            '        directory = pathlib.Path(os.environ["TMPDIR"])\n'
            '        for index in range(9):\n'
            '            with (directory / str(index)).open("wb") as stream:\n'
            '                stream.truncate(1024 * 1024)\n')
        self.git('add', '--all')
        self.git('commit', '-qm', 'test scratch accounting')
        captured = project_worktree.plan(self.source, EDITS)
        outcome = project_worktree.run(self.source, self.action, captured, 5000)
        self.assertEqual(outcome['status'], 'scratch_limit', outcome)
        self.assertFalse(outcome['assertions_verified'])

    @unittest.skipUnless(sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file(),
                         'the actual runner requires macOS Seatbelt')
    def test_source_checkout_filters_and_hooks_are_never_invoked(self):
        (self.source / '.gitattributes').write_text('*.py filter=fixture\n', encoding='utf-8')
        self.git('add', '--all')
        self.git('commit', '-qm', 'configure a fixture attribute')
        marker = self.root / 'unexpected-git-callback'
        helper = self.root / 'fixture-filter'
        helper.write_text(f'#!/bin/sh\n/usr/bin/touch "{marker}"\n/bin/cat\n', encoding='utf-8')
        helper.chmod(0o700)
        for direction in ('clean', 'smudge'):
            self.git('config', f'filter.fixture.{direction}', str(helper))
        self.git('config', 'core.hooksPath', str(self.root))
        hook = self.root / 'post-checkout'
        hook.write_text(f'#!/bin/sh\n/usr/bin/touch "{marker}"\n', encoding='utf-8')
        hook.chmod(0o700)
        captured = project_worktree.plan(self.source, EDITS)
        outcome = project_worktree.run(self.source, self.action, captured, 5000)
        self.assertEqual(outcome['status'], 'process_exited_zero', outcome)
        self.assertFalse(marker.exists())

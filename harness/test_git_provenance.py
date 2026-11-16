import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from context_manager import InvalidUpdate, canonical
import git_provenance as provenance
from review_budget import usage
import test_orchestration


def git(root, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
    command = ['git', '-c', 'user.name=Sutra fixture', '-c', 'user.email=fixture@example.invalid', *args]
    return subprocess.check_output(command, cwd=root, env=env, stderr=subprocess.PIPE, text=True).strip()


def initialize(root):
    git(root, 'init', '-q', '-b', 'main')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'fixture')
    return git(root, 'rev-parse', 'HEAD')


class GitProvenanceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='sutra-git-test-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()

    def test_exported_folder_is_unlinked(self):
        metadata = provenance.inspect(self.root)
        self.assertEqual(metadata['workspace_root'], str(self.root))
        self.assertEqual(metadata['kind'], 'unlinked')
        self.assertIsNone(metadata['commit'])
        self.assertFalse(metadata['remote_checked'])

    @unittest.skipUnless(shutil.which('git'), 'Git CLI not installed')
    def test_match_local_tracking_ref_and_detect_worktree_changes(self):
        source = self.root / 'main.py'
        source.write_text('value = 1\n')
        head = initialize(self.root)
        git(self.root, 'config', 'remote.origin.url', 'https://secret-token@example.invalid/repo')
        git(self.root, 'config', 'branch.main.remote', 'origin')
        git(self.root, 'config', 'branch.main.merge', 'refs/heads/main')
        git(self.root, 'update-ref', 'refs/remotes/origin/main', head)
        original_index = (self.root / '.git' / 'index').read_bytes()
        before = provenance.inspect(self.root)
        self.assertEqual((before['commit'], before['branch'], before['worktree']), (head, 'main', 'clean'))
        self.assertEqual(before['upstream']['relation'], 'matches_cached_tip')
        self.assertFalse(before['upstream']['live_verified'])
        self.assertNotIn('secret-token', canonical(before))
        source.write_text('value = 2\n')
        dirty = provenance.inspect(self.root)
        self.assertEqual(dirty['worktree'], 'dirty')
        self.assertFalse(provenance.same_snapshot(before, dirty))
        modified = source.stat().st_mtime_ns
        source.write_text('value = 3\n')
        os.utime(source, ns=(modified, modified))
        self.assertFalse(provenance.same_snapshot(dirty, provenance.inspect(self.root)))
        self.assertEqual((self.root / '.git' / 'index').read_bytes(), original_index)
        git(self.root, 'pack-refs', '--all')
        self.assertEqual(provenance.inspect(self.root)['commit'], head)
        git(self.root, 'checkout', '-q', '--detach', head)
        self.assertIsNone(provenance.inspect(self.root)['branch'])

    @unittest.skipUnless(shutil.which('git') and os.name != 'nt', 'Requires Git and a POSIX command-execution trap')
    def test_repository_filters_and_fsmonitor_cannot_execute(self):
        source = self.root / 'main.py'
        source.write_text('value = 1\n')
        (self.root / '.gitattributes').write_text('*.py filter=trap\n')
        initialize(self.root)
        marker = self.root / 'trap-ran'
        trap = self.root / 'trap.sh'
        trap.write_text('#!/bin/sh\ntouch "$PWD/trap-ran"\ncat\n')
        trap.chmod(0o700)
        git(self.root, 'config', 'core.fsmonitor', str(trap))
        git(self.root, 'config', 'filter.trap.clean', str(trap))
        git(self.root, 'config', 'filter.trap.process', str(trap))
        if marker.exists():
            marker.unlink()
        source.write_text('value = 2\n')
        metadata = provenance.inspect(self.root)
        self.assertEqual(metadata['worktree'], 'dirty')
        self.assertFalse(marker.exists())

    def test_upstream_selection_blocks_source_until_confirmed(self):
        fixture = test_orchestration.OrchestrationTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.prompt = 'Review the latest upstream architecture of this project. Remember project Cedar, ticket CED724.'
        started = fixture.step()
        paused = fixture.step(started)
        self.assertEqual(paused['stop']['status'], 'provenance_confirmation')
        self.assertIsNone(fixture.meta.get('active'))
        self.assertEqual(paused['git_provenance']['kind'], 'unlinked')
        self.assertEqual(fixture.step(paused, event='resume')['state'], 'PAUSED')
        with self.assertRaises(InvalidUpdate):
            fixture.step(paused, event='accept_provenance')
        confirmed = fixture.step(paused, event='accept_provenance', confirmed=True)
        self.assertEqual(confirmed['state'], 'BINDING')
        completed = fixture.until('DONE', confirmed)
        self.assertTrue(completed['data']['result']['admission']['admitted'])
        self.assertEqual(completed['data']['git_provenance']['kind'], 'unlinked')
        if completed['data']['report_markdown']:
            self.assertIn('**Source snapshot**', completed['data']['report_markdown'])

    @unittest.skipUnless(shutil.which('git'), 'Git CLI not installed')
    def test_checkout_change_pauses_before_synthesis_and_restart_preserves_budget(self):
        fixture = test_orchestration.OrchestrationTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.prompt = 'Review the architecture of this project. Remember project Cedar, ticket CED724.'
        initialize(fixture.ws)
        started = fixture.until('SYNTHESIS')
        self.assertEqual(started['git_provenance']['branch'], 'main')
        sid = fixture.meta['active']
        before = usage(fixture.s, sid)
        git(fixture.ws, 'checkout', '-qb', 'different')
        paused = fixture.step(started)
        self.assertEqual(paused['stop']['status'], 'provenance_changed')
        self.assertEqual(paused['recovery']['kind'], 'provenance_changed')
        self.assertEqual(usage(fixture.s, sid), before)
        restarted = fixture.step(paused, event='restart_review')
        self.assertEqual(restarted['conversation_id'], started['conversation_id'])
        self.assertEqual(usage(fixture.s, sid), before)

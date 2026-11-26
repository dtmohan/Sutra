"""Host-planned reads use the normal scoped repository effects on resume."""
import copy
import unittest
from unittest.mock import patch

from context_manager import InvalidUpdate
from invocation_gateway import InvocationLedger
from paging import PageFault
import orchestration as machine
import repository_tools as repo
import review_budget
import review_discovery as discovery
from review_source_targets import select
import test_review_discovery as fixture


class TargetedDiscoveryTests(unittest.TestCase):
    setUp = fixture.DiscoveryToolTests.setUp
    tearDown = fixture.DiscoveryToolTests.tearDown
    step = fixture.DiscoveryToolTests.step
    until = fixture.DiscoveryToolTests.until
    provider = fixture.DiscoveryToolTests.provider

    def oversized_first_target(self):
        readme = '# Project overview\n' + 'A long readme line with project context.\n' * 14000
        (self.ws / 'README.md').write_text(readme)
        (self.ws / 'VALIDATION.md').write_text('# Validation\n\nThe launch path needs a test.\n')
        (self.ws / 'main.py').write_text('def launch():\n    return 42\n\nlaunch()\n')
        for index in range(12):
            (self.ws / f'component{index:02}.py').write_text(f'VALUE = {index}\n')
        current = self.until('SYNTHESIS')
        limit = repo.settings(self.s, self.meta['active'])['review_page_kib'] * 1024
        self.assertGreater(len(readme.encode()), limit)
        return current, len(readme.encode()), limit

    def capture_targeted(self):
        original = discovery.advance

        def capture(*args, **kwargs):
            kwargs['targeted'] = not self.captured
            result = original(*args, **kwargs)
            self.captured.append((copy.deepcopy(args[2]), result))
            return result

        return patch.object(discovery, 'advance', side_effect=capture)

    def test_oversized_first_target_is_skipped_once_and_later_reads_survive_switch(self):
        current, required, limit = self.oversized_first_target()
        sid = self.meta['active']
        target_advance = discovery._advance_targets
        skipped_spending = []

        def track_skip(*args, **kwargs):
            before = review_budget.usage(self.s, sid)
            result = target_advance(*args, **kwargs)
            checkpoint = result['checkpoint']
            if checkpoint.get('skipped_targets') and not checkpoint['receipts']:
                skipped_spending.append((before, review_budget.usage(self.s, sid)))
            return result

        with self.capture_targeted(), patch.object(discovery, '_advance_targets', side_effect=track_skip):
            current = self.step(current)
            _, result = self.captured[-1]
            self.assertFalse(result['complete'])
            [skipped] = result['checkpoint']['skipped_targets']
            self.assertEqual((skipped['path'], skipped['index'], skipped['reason']),
                             ('README.md', 0, 'source_page_limit'))
            self.assertEqual((skipped['required_bytes'], skipped['page_limit_bytes']), (required, limit))
            self.assertEqual(result['checkpoint']['receipts'], [])
            self.assertIsNone(self.s.db.execute(
                'SELECT id FROM repository_ops WHERE request_id=?', (skipped['request_id'],)).fetchone())
            [before, after] = skipped_spending[0]
            self.assertEqual(before, after)
            self.assertTrue(self.s.db.execute(
                'SELECT 1 FROM page_audit WHERE event_hash=?', (skipped['receipt'],)).fetchone())
            self.s.close()
            self.s = InvocationLedger(self.root / 'state.sqlite')
            self.tier = 'high'
            for _ in range(8):
                current = self.step(current)
                if self.captured[-1][1]['complete']:
                    break

        packet, result = self.captured[-1]
        self.assertTrue(result['complete'], current)
        self.assertEqual(len(skipped_spending), 1)
        self.assertEqual(result['checkpoint']['skipped_targets'], [skipped])
        self.assertEqual(result['checkpoint']['key'], skipped['key'])
        self.assertEqual(result['checkpoint']['stop_reason'], 'targeted_sources_with_skips')
        self.assertEqual(self.calls, [])
        self.assertEqual(packet['discovery_coverage']['skipped_targets'], [
            {'path': 'README.md', 'line': 1, 'reason': 'source_page_limit',
             'required_bytes': required, 'page_limit_bytes': limit}])
        files = packet['discovery_coverage']['read_files']
        self.assertIn('main.py', files)
        self.assertIn('VALIDATION.md', files)
        self.assertNotIn('README.md', files)
        receipts = discovery.checkpoint_receipts(self.s, sid, packet)
        self.assertGreaterEqual(len(receipts), 2)
        self.assertTrue(all(row['operation']['request_id'] != skipped['request_id'] for row in receipts))
        self.assertTrue(self.s.audit_valid())

        # A skip proves why no bytes were admitted at the time. It pins no source version.
        (self.ws / 'README.md').write_text('Now small enough for a new discovery plan.\n')
        self.assertEqual(discovery.checkpoint_receipts(self.s, sid, packet), receipts)
        plan = discovery._target_plan(self.s, sid, skipped['key'])
        with machine.scope(packet['discovery_owner']), self.assertRaisesRegex(InvalidUpdate, 'already skipped'):
            repo.execute(self.s, sid, self.ws, self.root, 'read', target='README.md', line=1,
                         request_id=skipped['request_id'], resource_check=False,
                         read_profile=plan['reads'][0]['profile'], host_discovery=skipped['key'])
        changed = copy.deepcopy(packet)
        changed['evidence_discovery']['skipped_targets'][0]['path'] = 'other.md'
        with self.assertRaisesRegex(InvalidUpdate, 'checkpoint changed'):
            discovery.checkpoint_receipts(self.s, sid, changed)

    def test_legacy_oversized_target_checkpoints_keep_original_stop_semantics(self):
        current, _, _ = self.oversized_first_target()
        sid = self.meta['active']
        with self.capture_targeted():
            with patch.object(discovery, 'TARGET_PLAN_VERSION', 1):
                current = self.step(current)
            original, result = self.captured[-1]
            checkpoint = result['checkpoint']
            self.assertTrue(checkpoint['complete'])
            self.assertEqual(checkpoint['stop_reason'], 'targeted_source_limit')
            self.assertNotIn('skipped_targets', checkpoint)
            self.assertEqual(discovery._target_plan(self.s, sid, checkpoint['key'])['version'], 1)
            self.s.close()
            self.s = InvocationLedger(self.root / 'state.sqlite')
            current = self.step(current)
        replayed, result = self.captured[-1]
        self.assertEqual(result['checkpoint'], checkpoint)
        self.assertEqual(discovery.checkpoint_receipts(self.s, sid, original), [])
        self.assertEqual(discovery.checkpoint_receipts(self.s, sid, replayed), [])
        self.assertEqual(self.calls, [])

    def test_targeted_reads_persist_after_reload_and_model_switch(self):
        (self.ws / 'README.md').write_text('Project purpose and audience.\n')
        (self.ws / 'VALIDATION.md').write_text('Known validation gap.\n')
        body = 'def launch():\n    return 42\n' + '# long source comment\n' * 3300 + 'launch()\n'
        (self.ws / 'main.py').write_text(body)
        self.assertGreater(len(body.encode()), repo.MAX_FILE)
        current = self.until('SYNTHESIS')
        original = discovery.advance

        def capture(*args, **kwargs):
            kwargs['targeted'] = not self.captured
            result = original(*args, **kwargs)
            self.captured.append((copy.deepcopy(args[2]), result))
            return result

        with patch.object(discovery, 'advance', side_effect=capture):
            current = self.step(current)
            first, result = self.captured[0]
            self.assertFalse(result['complete'])
            key = result['checkpoint']['key']
            first_receipt = result['checkpoint']['receipts'][0]
            sid = self.meta['active']
            plan = discovery._target_plan(self.s, sid, key)
            with machine.scope(first['discovery_owner']):
                with self.assertRaisesRegex(InvalidUpdate, 'differs from its host plan'):
                    repo.execute(self.s, sid, self.ws, self.root, 'read', target='README.md', line=2,
                                 request_id='uncommitted-host-request', resource_check=False,
                                 read_profile=plan['reads'][0]['profile'], host_discovery=key)
            self.s.close()
            self.s = InvocationLedger(self.root / 'state.sqlite')
            self.tier = 'high'
            for _ in range(8):
                current = self.step(current)
                if self.captured[-1][1]['complete']:
                    break
        packet, result = self.captured[-1]
        self.assertTrue(result['complete'], current)
        self.assertEqual(self.calls, [])
        self.assertEqual(result['checkpoint']['key'], key)
        receipts = discovery.checkpoint_receipts(self.s, sid, packet)
        self.assertEqual(first_receipt, receipts[0]['id'])
        self.assertEqual(len(receipts), len(set(result['checkpoint']['receipts'])))
        self.assertIn('VALIDATION.md', packet['discovery_coverage']['read_files'])
        self.assertIn('README.md', packet['discovery_coverage']['read_files'])
        main_reads = [r for r in receipts if r['operation']['target'] == 'main.py']
        self.assertTrue(main_reads)
        self.assertTrue(all(r['operation']['actor'] == 'host_tool' for r in receipts))
        self.assertTrue(all(r['operation']['task_cost']['source_bytes'] == len(body.encode()) for r in main_reads))
        self.assertTrue(any('launch()' in e['text'] and e['line'] > 3000
                            for r in main_reads for e in r['result']['excerpts']))
        (self.ws / 'README.md').write_text('Changed after the committed read.\n')
        with self.assertRaisesRegex(PageFault, 'Discovery source changed: README.md'):
            discovery.checkpoint_receipts(self.s, sid, packet)

    def test_selects_boot_callback_and_validation_in_scope(self):
        nodes = [{'id': 'root', 'name': '<module>', 'path': 'index.html', 'line': 1},
                 {'id': 'loop', 'name': 'frame', 'path': 'index.html', 'line': 70},
                 {'id': 'event', 'name': '<anonymous@110>', 'path': 'index.html', 'line': 110},
                 {'id': 'render', 'name': 'render', 'path': 'index.html', 'line': 12}]
        calls = [{'id': 'boot', 'source': 'root', 'target': None, 'line': 150,
                  'path': 'index.html', 'expression': 'requestAnimationFrame', 'text': 'requestAnimationFrame(frame)'},
                 {'id': 'hook', 'source': 'root', 'target': None, 'line': 110, 'path': 'index.html',
                  'expression': 'onChange.addEventListener', 'text': 'onChange.addEventListener("change", () => render())'},
                 {'id': 'draw', 'source': 'event', 'target': 'render', 'line': 111, 'path': 'index.html',
                  'expression': 'render', 'text': 'render()'}]
        data = {'sources': {'index.html': '', 'outside.py': ''}, 'graph': {'nodes': nodes, 'calls': calls}}
        plan = select(data, ['index.html', 'README.md', 'VALIDATION.md'], 'Review architecture and suggest next steps')
        self.assertEqual([(p['role'], p['path'], p['line']) for p in plan], [
            ('purpose', 'README.md', 1), ('entry', 'index.html', 142),
            ('implementation', 'index.html', 70), ('validation', 'VALIDATION.md', 1),
            ('event_path', 'index.html', 110)])

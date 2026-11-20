"""Control-plane tests only: no model, telemetry or user ledger is contacted."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('convergence_experiment',
    Path(__file__).with_name('experiment-discovery-convergence.py'))
experiment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(experiment)


class RunnerTests(unittest.TestCase):
    def test_repeated_tree_hash_is_stable_and_source_change_is_detected(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path/'a.py').write_text('VALUE = 1\n')
            before = experiment.tree_digest(path)
            self.assertEqual(before, experiment.tree_digest(path))
            (path/'a.py').write_text('VALUE = 2\n')
            self.assertNotEqual(before, experiment.tree_digest(path))

    def test_frozen_tree_rejects_linked_source(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            (path/'a.py').write_text('VALUE = 1\n')
            (path/'b.py').symlink_to(path/'a.py')
            with self.assertRaisesRegex(ValueError, 'regular unlinked'):
                experiment.tree_digest(path)

    def test_private_checkpoint_roundtrip_and_atomic_temp_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'checkpoint.json'
            experiment.save_json(path, {'spent': 75})
            experiment.save_json(path, {'spent': 100})
            self.assertEqual(experiment.read_json(path), {'spent': 100})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertFalse(path.with_name(path.name+'.tmp').exists())

    def test_environment_disables_grants_and_managed_activation_without_changing_caller(self):
        trial = dict(port=19483, server_model='model', directory='/tmp/fixture', runtime='/tmp/runtime')
        with patch.dict(os.environ, {'SUTRA_STRATA_ONLY': '1', 'SUTRA_THINKING': 'on',
                                    'SUTRA_WORKSPACE_ALLOCATION': '{}', 'KARMA_STORAGE_LIMIT_MIB': '2048'}):
            result = experiment.environment(trial)
            self.assertNotIn('SUTRA_STRATA_ONLY', result)
            self.assertEqual(result['SUTRA_THINKING'], 'adaptive')
            self.assertFalse(json.loads(result['SUTRA_WORKSPACE_ALLOCATION'])['enabled'])
            self.assertEqual(result['SUTRA_MEMORY_OWNERSHIP'], 'shared')
            self.assertEqual(os.environ['SUTRA_THINKING'], 'on')

    def test_explicit_architecture_entry_keeps_the_precreated_task(self):
        binding = {'session': 'isolated', 'contract_hash': 'bound'}
        request = experiment.architecture_start_request({'prompt': 'Exact prompt', 'model': 'strata:high'}, binding)
        self.assertNotIn('chat_key', request)
        self.assertEqual(request['binding'], binding)
        self.assertEqual(request['prompt'], 'Exact prompt')
        self.assertEqual(request['models']['low'], 'qwen3:4b')
        self.assertEqual(request['models']['high'], 'strata:high')

    def test_product_projection_retains_exact_runtime_text_and_excludes_fixtures(self):
        with tempfile.TemporaryDirectory() as root:
            parent = Path(root)
            source = parent/'source'
            source.mkdir()
            for path, text in [('entry.py', 'ENTRY = 1\n'), ('test_entry.py', 'fixture\n'),
                               ('README.md', 'Project\n'), ('vendor/lib.js', 'vendor\n')]:
                target = source/path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text)
            (source/'package.py').write_text(
                "def runtime_files(source): return {source/'entry.py', source/'vendor/lib.js'}\n")
            config = parent/'config.json'
            experiment.save_json(config, {'directory': str(parent), 'workspace': str(source),
                'baseline_runtime': str(source), 'commit': 'frozen', 'prompt': 'Exact prompt'})
            output = parent/'projected-config.json'
            experiment.project(config, output)
            projected = parent/'product-source-bc74250'
            self.assertEqual((projected/'entry.py').read_bytes(), (source/'entry.py').read_bytes())
            self.assertEqual((projected/'README.md').read_bytes(), (source/'README.md').read_bytes())
            self.assertFalse((projected/'test_entry.py').exists())
            self.assertFalse((projected/'vendor').exists())
            self.assertEqual(experiment.read_json(output)['baseline_runtime'], str(source))

    def test_exact_ledger_cap_does_not_allow_an_extra_action(self):
        checkpoint = dict(state='SYNTHESIS', steps=10, parent_checkpoint={'id': 'saved'})
        accounting = dict(model_calls=12, discovery_calls=8, pending_calls=0)
        self.assertEqual(experiment.next_action_allowed(checkpoint, accounting),
                         (False, 'experiment_model_call_ceiling'))

    def test_discovery_boundary_preserves_an_incomplete_workflow(self):
        checkpoint = dict(state='SYNTHESIS', steps=10)
        accounting = dict(model_calls=10, discovery_calls=8, pending_calls=0)
        self.assertEqual(experiment.next_action_allowed(checkpoint, accounting),
                         (False, 'experiment_discovery_observation_boundary'))
        self.assertEqual(checkpoint['state'], 'SYNTHESIS')
        checkpoint['parent_checkpoint'] = {'id': 'saved'}
        self.assertEqual(experiment.next_action_allowed(checkpoint, accounting), (True, None))

    def test_pending_model_work_is_never_automatically_replayed(self):
        checkpoint = dict(state='SYNTHESIS', steps=4)
        accounting = dict(model_calls=3, discovery_calls=1, pending_calls=1)
        self.assertEqual(experiment.next_action_allowed(checkpoint, accounting),
                         (False, 'pending_operation_requires_inspection'))

    def test_supplementary_limits_can_narrow_without_widening_the_approved_cap(self):
        self.assertEqual(experiment.experiment_limits({'limits': {'review_seconds': 1200}})['review_seconds'], 1200)
        self.assertEqual(experiment.experiment_limits({})['review_seconds'], 1800)
        for invalid in (1801, True, 0):
            with self.assertRaisesRegex(ValueError, 'only narrow'):
                experiment.experiment_limits({'limits': {'review_seconds': invalid}})

    def test_soft_boundary_observes_only_the_current_task_durable_stage_completion(self):
        db = sqlite3.connect(':memory:')
        try:
            db.executescript('CREATE TABLE invocations(session,delivery_id,record_json);'
                'CREATE TABLE deliveries(id,payload_json); CREATE TABLE page_audit(seq,event,payload_json);')
            db.execute('INSERT INTO invocations VALUES(?,?,?)', ('mine', 'delivery', '{"role":"architect"}'))
            db.execute('INSERT INTO deliveries VALUES(?,?)', ('delivery', '{"discovery_key":"current"}'))
            value = {'session': 'other', 'checkpoint': {'key': 'current', 'complete': True, 'stop_reason': 'finished'}}
            db.execute('INSERT INTO page_audit VALUES(?,?,?)', (1, 'PARENT_DISCOVERY_CHECKPOINT', json.dumps(value)))
            self.assertIsNone(experiment.discovery_terminal(db, 'mine'))
            value['session'] = 'mine'
            db.execute('INSERT INTO page_audit VALUES(?,?,?)', (2, 'PARENT_DISCOVERY_CHECKPOINT', json.dumps(value)))
            self.assertEqual(experiment.discovery_terminal(db, 'mine'), {'complete': True, 'reason': 'finished'})
        finally:
            db.close()

    def test_consolidated_report_does_not_copy_prompts_answers_or_reasoning(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            (directory/'store/calls').mkdir(parents=True)
            trial = {'label': 'candidate', 'runtime_tree': {}, 'source_tree': {},
                'commit': 'frozen', 'prompt_sha256': 'a'*64, 'prompt': 'PRIVATE PROMPT',
                'thinking': 'adaptive', 'limits': dict(experiment.LIMITS), 'method_limits': []}
            checkpoint = {'state': 'DONE', 'steps': 1}
            experiment.save_json(directory/'trial.json', trial)
            experiment.save_json(directory/'checkpoint.json', checkpoint)
            experiment.save_json(directory/'response-001.json', {'data': {'result': {
                'admission': {'admitted': True}, 'accepted_output': {'answer': 'PRIVATE ANSWER'}},
                'report_review': {'status': 'reviewed'}}})
            experiment.save_json(directory/'store/calls/a.json', {'ok': True, 'request': {'prompt': 'PRIVATE WIRE'},
                'response': {'done_reason': 'stop', 'eval_count': 20,
                             'message': {'content': 'PRIVATE BODY', 'thinking': 'PRIVATE THOUGHT'}}})
            result = experiment.report_trial(directory)
            self.assertNotIn('PRIVATE', json.dumps(result))
            self.assertTrue(result['host_report_returned'])
            self.assertEqual(result['semantic_quality'], 'not assessed by this script')


if __name__ == '__main__':
    unittest.main()

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from context_manager import InvalidUpdate
import engine_profiles as profiles


def descriptor(root, identifier='direct', context=16384):
    executable = root / 'engine'
    if not executable.exists():
        executable.write_text('#!/bin/sh\nexit 0\n')
        executable.chmod(0o700)
    tokenizer = root / 'tokenizer'
    tokenizer.mkdir(exist_ok=True)
    for name in ('vocab.json', 'merges.txt', 'chat_template.jinja'):
        path = tokenizer / name
        if not path.exists():
            path.write_text('{}')
    weights = root / 'model.gguf'
    if not weights.exists():
        weights.write_bytes(b'GGUF fixture')
    metal = root / 'metal'
    metal.mkdir(exist_ok=True)
    if not (metal / 'kernel.metal').exists():
        (metal / 'kernel.metal').write_text('kernel fixture')
    return {'id': identifier, 'model': 'local-fixture',
            'engine': {'executable': str(executable), 'args': ['--native', str(weights), '--max-context', str(context)],
                       'cwd': str(root), 'environment': {'STRATA_METAL_ARENA_GB': '4'}},
            'tokenizer_path': str(tokenizer), 'context_tokens': context, 'cold_memory_bytes': None}


class EngineProfileTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / 'models.json'
        self.raw = descriptor(self.root)

    def test_round_trip_binds_descriptor_and_regular_artifact_metadata(self):
        registered = profiles.write({'direct': self.raw}, self.path)['direct']
        loaded = profiles.load('sutra:engine/direct', self.path)
        self.assertEqual(loaded, registered)
        self.assertEqual(len(loaded['profile_hash']), 64)
        self.assertIn(str(self.root / 'model.gguf'), [row['path'] for row in loaded['artifact_bindings']])
        identity = profiles.identity(loaded['name'], loaded)
        self.assertEqual(identity['engine_profile_hash'], loaded['profile_hash'])
        self.assertIsNone(identity['weights_sha256'])
        self.assertFalse(identity['server_attested'])

    def test_compiler_cache_is_not_treated_as_a_model_change(self):
        profiles.write({'direct': self.raw}, self.path)
        (self.root / 'metal' / 'cached.metallib').write_bytes(b'compiled cache')
        self.assertTrue(profiles.load('sutra:engine/direct', self.path))

    def test_modified_artifact_and_descriptor_require_reimport(self):
        registered = profiles.write({'direct': self.raw}, self.path)['direct']
        weights = self.root / 'model.gguf'
        weights.write_bytes(b'changed model')
        with self.assertRaisesRegex(InvalidUpdate, 'artifacts changed'):
            profiles.load('sutra:engine/direct', self.path)
        damaged = copy.deepcopy(registered)
        damaged['context_tokens'] = 32768
        with self.assertRaises(InvalidUpdate):
            profiles.normalize(damaged)

    def test_registry_is_bounded_and_refuses_symlinks_or_malformed_json(self):
        self.assertEqual(profiles.read(self.path), {})
        self.path.write_text('not-json')
        with self.assertRaises(InvalidUpdate): profiles.read(self.path)
        self.path.write_bytes(b'x' * (profiles.MAX_REGISTRY_BYTES + 1))
        with self.assertRaises(InvalidUpdate): profiles.read(self.path)
        self.path.unlink()
        target = self.root / 'target'
        target.write_text('{"version":1,"profiles":{}}')
        self.path.symlink_to(target)
        with self.assertRaises(InvalidUpdate): profiles.read(self.path)

    def test_invalid_paths_control_arguments_and_environment_are_rejected(self):
        variants = []
        for key, value in [('id', '../other'), ('context_tokens', True), ('cold_memory_bytes', -1),
                           ('tokenizer_path', 'relative')]:
            changed = copy.deepcopy(self.raw)
            changed[key] = value
            variants.append(changed)
        for key, value in [('executable', 'relative'), ('args', ['--serve']),
                           ('environment', {'PYTHONPATH': '/workspace'})]:
            changed = copy.deepcopy(self.raw)
            changed['engine'][key] = value
            variants.append(changed)
        for value in variants:
            with self.subTest(profile=value), self.assertRaises(InvalidUpdate):
                profiles.normalize(value)

    def test_unavailable_profile_is_excluded_with_reason(self):
        profiles.write({'direct': self.raw}, self.path)
        (self.root / 'model.gguf').unlink()
        result = profiles.catalog(self.path)
        self.assertEqual(result['models'], [])
        self.assertEqual(result['errors'][0]['model'], 'sutra:engine/direct')

    def test_only_trusted_assignment_names_are_accepted(self):
        for value in ('{}', '["sutra:engine/../other"]', '["ollama-model"]'):
            with patch.dict(os.environ, SUTRA_ENGINE_MODELS=value), self.assertRaises(InvalidUpdate):
                profiles.assigned_models()
        with patch.dict(os.environ, SUTRA_ENGINE_MODELS='["sutra:engine/direct"]'):
            self.assertEqual(profiles.assigned_models(), {'sutra:engine/direct'})
        with patch.dict(os.environ, SUTRA_ENGINE_REGISTRY='relative'), self.assertRaises(InvalidUpdate):
            profiles.registry_path()


if __name__ == '__main__': unittest.main()

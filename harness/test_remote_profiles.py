import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from context_manager import InvalidUpdate, canonical, sha
import remote_profiles as profiles


def descriptor(root, identifier='rtx3090', **changes):
    key = Path(root) / 'api-key'
    if not key.exists():
        key.write_text('unit-test-remote-credential\n')
        key.chmod(0o600)
    result = {'id': identifier, 'ssh_alias': 'sutra-rtx3090', 'port': 18082,
              'remote_port': 18080, 'api_key_file': str(key), 'model': 'qwen3-4b-rtx',
              'model_path': r'C:\Users\Deepak\SutraRuntime\models\qwen3-4b.gguf',
              'model_sha256': 'a' * 64, 'context_tokens': 8192,
              'architecture': 'qwen3'}
    result.update(changes)
    return result


class RemoteProfileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.registry = self.root / 'models.json'
        self.profile = descriptor(self.root)

    def test_roundtrip_is_private_bound_and_does_not_read_credential_into_catalog(self):
        written = profiles.write({'rtx3090': self.profile}, self.registry)
        self.assertEqual(profiles.load('sutra:remote/rtx3090', self.registry), written['rtx3090'])
        self.assertEqual(self.registry.stat().st_mode & 0o777, 0o600)
        catalog = profiles.catalog(self.registry)
        self.assertEqual(catalog['models'][0]['provider'], 'sutra_remote')
        self.assertNotIn('unit-test-remote-credential', json.dumps(catalog))
        self.assertEqual(profiles.api_key(written['rtx3090']), 'unit-test-remote-credential')

    def test_profile_cannot_add_network_authority_or_shell_fragments(self):
        for change in ({'host': 'public.example'}, {'ssh_alias': '-oProxyCommand=bad'},
                       {'ssh_alias': 'name;not-a-command'}, {'model_path': '../outside'},
                       {'api_key_file': 'relative'}, {'context_tokens': True},
                       {'port': 65536}, {'remote_task': 'task;injected'},
                       {'model_sha256': 'sha256:' + 'a' * 64}, {'gpu_index': -1}):
            with self.subTest(change=change), self.assertRaises(InvalidUpdate):
                profiles.normalize(dict(self.profile, **change))

    def test_changed_bound_descriptor_fails_instead_of_silently_reapproving(self):
        item = profiles.normalize(self.profile)
        item['remote_port'] += 1
        with self.assertRaisesRegex(InvalidUpdate, 'changed after approval'):
            profiles.normalize(item)

    def test_registry_key_must_match_named_profile(self):
        with self.assertRaisesRegex(InvalidUpdate, 'key differs'):
            profiles.write({'another': self.profile}, self.registry)

    def test_private_credentials_reject_links_and_shared_permissions(self):
        item = profiles.normalize(self.profile)
        original = Path(item['api_key_file'])
        original.chmod(0o644)
        with self.assertRaises(InvalidUpdate):
            profiles.api_key(item)
        original.chmod(0o600)
        link = self.root / 'alias-key'; link.symlink_to(original)
        with self.assertRaises(InvalidUpdate):
            profiles.api_key(dict(item, api_key_file=str(link)))
        hard = self.root / 'hard-key'; os.link(original, hard)
        with self.assertRaises(InvalidUpdate):
            profiles.api_key(item)

    def test_registry_cannot_be_group_writable_or_symlinked(self):
        profiles.write({'rtx3090': self.profile}, self.registry)
        self.registry.chmod(0o660)
        with self.assertRaises(InvalidUpdate):
            profiles.read(self.registry)
        self.registry.chmod(0o600)
        alias = self.root / 'alias.json'; alias.symlink_to(self.registry)
        with self.assertRaises(InvalidUpdate):
            profiles.read(alias)

    def test_same_copied_gguf_does_not_become_an_independent_reviewer(self):
        item = profiles.normalize(self.profile)
        result = profiles.identity(item['name'], item)
        expected = sha(canonical([{'mediaType': 'application/vnd.ollama.image.model',
                                   'digest': 'sha256:' + item['model_sha256']}]))
        self.assertEqual(result['weights_sha256'], expected)
        from model_identity import compare
        relation = compare(dict(result, name='qwen3:4b', backend='ollama'), result)
        self.assertEqual(relation['weight_relation'], 'same')
        self.assertFalse(result['server_attested'])

    def test_assignment_is_host_configuration_and_not_a_registry_side_effect(self):
        item = profiles.normalize(self.profile)
        with patch.dict(os.environ, SUTRA_REMOTE_MODELS='[]'):
            with self.assertRaisesRegex(InvalidUpdate, 'not assigned'):
                profiles.require_assignment(item)
        with patch.dict(os.environ, SUTRA_REMOTE_MODELS=json.dumps([item['name']])):
            profiles.require_assignment(item)
        for value in ('{}', 'not-json', '["qwen3:4b"]'):
            with patch.dict(os.environ, SUTRA_REMOTE_MODELS=value):
                with self.assertRaises(InvalidUpdate):
                    profiles.assigned_models()

    def test_optional_cold_allocation_requires_complete_typed_covered_bounds(self):
        plain = profiles.normalize(self.profile)
        self.assertNotIn('cold_gpu_bytes', plain)
        cold = dict(cold_gpu_bytes=8 * 1024**3, cold_host_bytes=8 * 1024**3,
                    cold_context_tokens=32768)
        approved = profiles.normalize(dict(self.profile, **cold))
        self.assertEqual(approved['cold_context_tokens'], 32768)
        self.assertNotEqual(approved['profile_hash'], plain['profile_hash'])
        for mutation in ({'cold_host_bytes': None}, {'cold_gpu_bytes': True},
                         {'cold_host_bytes': 0}, {'cold_gpu_bytes': 2**53},
                         {'cold_context_tokens': 4096}, {'cold_context_tokens': '32768'},
                         {'cold_context_tokens': 2**21 + 1}):
            with self.subTest(mutation=mutation), self.assertRaises(InvalidUpdate):
                profiles.normalize(dict(self.profile, **dict(cold, **mutation)))
        for missing in cold:
            partial = dict(cold); partial.pop(missing)
            with self.subTest(missing=missing), self.assertRaisesRegex(InvalidUpdate, 'together'):
                profiles.normalize(dict(self.profile, **partial))
        with self.assertRaisesRegex(InvalidUpdate, 'coverage'):
            profiles.normalize(dict(self.profile, context_tokens=65536, **cold))

    def test_fingerprints_and_gpu_identifiers_require_strings(self):
        for field, value in (('model_sha256', int('1' * 64)),
                             ('executable_sha256', int('1' * 64)),
                             ('gpu_uuid', 123), ('model_sha256', True)):
            with self.subTest(field=field), self.assertRaises(InvalidUpdate):
                profiles.normalize(dict(self.profile, **{field: value}))


if __name__ == '__main__':
    unittest.main()

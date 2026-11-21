import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from context_manager import InvalidUpdate
import remote_control as control
import remote_profiles
from test_remote_profiles import descriptor


class RemoteControlTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profile = remote_profiles.normalize(descriptor(self.root))
        env = patch.dict(os.environ, SUTRA_REMOTE_MODELS=json.dumps([self.profile['name']]))
        env.start(); self.addCleanup(env.stop)

    def test_large_unicode_probe_uses_bounded_stdin_and_a_fixed_short_loader(self):
        script = "$path='C:\\用户\\model.gguf'\n" + '# trusted metadata probe\n' * 500
        with patch('remote_control._ssh', return_value='/usr/bin/ssh'), \
                patch('remote_control.subprocess.run', return_value=SimpleNamespace(
                    returncode=0, stdout=b'{"ok":true}', stderr=b'')) as execute:
            self.assertEqual(control.run_ps(self.profile, script), {'ok': True})
        args, options = execute.call_args
        self.assertLess(len(args[0][-1]), 2048)
        loader = base64.b64decode(args[0][-1].split()[-1]).decode('utf-16le')
        self.assertIn('[Console]::InputEncoding=[Text.Encoding]::UTF8', loader)
        self.assertIn('[Console]::In.ReadToEnd()', loader)
        self.assertNotIn('model.gguf', ' '.join(args[0]))
        self.assertIn(script.encode('utf-8'), options['input'])
        self.assertNotIn('shell', options)
        self.assertIn('StrictHostKeyChecking=yes', args[0])
        self.assertIn('BatchMode=yes', args[0])
        self.assertNotIn('-S', args[0])

    def test_observation_reuses_its_profile_control_socket(self):
        socket_path = self.root / 'approved.sock'
        with patch('remote_control._ssh', return_value='/usr/bin/ssh'), \
                patch('remote_control.control_socket', return_value=socket_path) as socket, \
                patch('remote_control.subprocess.run', return_value=SimpleNamespace(
                    returncode=0, stdout=b'{"ok":true}', stderr=b'')) as execute:
            self.assertEqual(control.run_ps(self.profile, '{}', reuse_tunnel=True), {'ok': True})
        socket.assert_called_once_with(self.profile)
        command = execute.call_args.args[0]
        self.assertEqual(command[command.index('-S') + 1], str(socket_path))
        self.assertEqual(command[-2], self.profile['ssh_alias'])
        self.assertIn('StrictHostKeyChecking=yes', command)
        self.assertIn('-T', command)
        self.assertNotIn('-M', command)
        self.assertNotIn('-L', command)

    def test_probe_failure_and_timeout_withhold_provider_error_details(self):
        for result in (SimpleNamespace(returncode=1, stdout=b'', stderr=b'PRIVATE_REMOTE_ERROR'),
                       subprocess.TimeoutExpired('ssh', 10)):
            with patch('remote_control._ssh', return_value='/usr/bin/ssh'), \
                    patch('remote_control.subprocess.run') as execute:
                if isinstance(result, Exception):
                    execute.side_effect = result
                else:
                    execute.return_value = result
                with self.assertRaises(InvalidUpdate) as caught:
                    control.run_ps(self.profile, '{}')
                self.assertNotIn('PRIVATE_REMOTE_ERROR', str(caught.exception))

    def test_unowned_occupied_port_cannot_receive_a_credential_or_inference(self):
        sock = MagicMock(); sock.connect_ex.return_value = 0
        with patch('remote_control.control_socket', return_value=self.root / 'control.sock'), \
                patch('remote_control._ssh', return_value='/usr/bin/ssh'), \
                patch('remote_control.socket.socket', return_value=sock), \
                patch('remote_control.subprocess.run') as execute:
            with self.assertRaisesRegex(InvalidUpdate, 'unowned endpoint'):
                control.ensure_tunnel(self.profile)
            execute.assert_not_called()

    def test_forward_destination_is_fixed_loopback_and_no_password_prompts(self):
        sock = MagicMock(); sock.connect_ex.return_value = 111
        with patch('remote_control.control_socket', return_value=self.root / 'control.sock'), \
                patch('remote_control._ssh', return_value='/usr/bin/ssh'), \
                patch('remote_control.socket.socket', return_value=sock), \
                patch('remote_control.subprocess.run', return_value=SimpleNamespace(returncode=0)) as execute:
            self.assertTrue(control.ensure_tunnel(self.profile)['owned'])
        command = execute.call_args.args[0]
        self.assertEqual(command[command.index('-L') + 1], '127.0.0.1:18082:127.0.0.1:18080')
        self.assertIn('ExitOnForwardFailure=yes', command)
        self.assertIn('StrictHostKeyChecking=yes', command)
        self.assertNotIn('unit-test-remote-credential', repr(command))

    def test_metadata_auth_errors_do_not_echo_body_or_secret(self):
        response = MagicMock(); response.status = 401
        response.read.return_value = b'PRIVATE_SERVER_BODY'
        connection = MagicMock(); connection.getresponse.return_value = response
        with patch('remote_control.http.client.HTTPConnection', return_value=connection):
            with self.assertRaisesRegex(InvalidUpdate, 'HTTP 401') as caught:
                control.request_json(self.profile, '/props')
        response.read.assert_not_called()
        self.assertNotIn('PRIVATE_SERVER_BODY', str(caught.exception))
        self.assertNotIn('unit-test-remote-credential', str(caught.exception))

    def test_metadata_interface_cannot_make_arbitrary_or_mutating_requests(self):
        with patch('remote_control.http.client.HTTPConnection') as connect:
            for path, body in (('/v1/chat/completions', None), ('http://public/metadata', None), ('/props', {})):
                with self.assertRaises(InvalidUpdate):
                    control.request_json(self.profile, path, body)
            connect.assert_not_called()

    def test_short_control_socket_survives_long_macos_tmpdir(self):
        with patch('remote_control.sys.platform', 'darwin'), \
                patch('remote_control.tempfile.gettempdir', return_value='/var/folders/' + 'x' * 80):
            result = control.control_socket(self.profile)
        self.assertLess(len(str(result).encode()) + 17, 104)
        self.assertTrue(str(result).startswith('/private/tmp/'))

    def test_owned_tunnel_identity_tracks_connection_authority_not_model_policy(self):
        before = control.control_socket(self.profile)
        updated = dict(self.profile)
        updated.pop('profile_hash')
        updated.update(context_tokens=32768, cold_gpu_bytes=8 * 1024**3,
                       cold_host_bytes=8 * 1024**3, cold_context_tokens=32768)
        updated = remote_profiles.normalize(updated)
        self.assertNotEqual(updated['profile_hash'], self.profile['profile_hash'])
        self.assertEqual(control.control_socket(updated), before)
        for change in ({'ssh_alias': 'another-approved-alias'}, {'port': 18083}, {'remote_port': 18081}):
            alternate = dict(updated, **change); alternate.pop('profile_hash')
            self.assertNotEqual(control.control_socket(remote_profiles.normalize(alternate)), before)


if __name__ == '__main__':
    unittest.main()

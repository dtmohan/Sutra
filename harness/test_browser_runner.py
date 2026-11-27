from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import socket
from threading import Thread
import unittest
from unittest.mock import patch

import browser_runner
from context_manager import InvalidUpdate
from lifecycle import Interrupted


class BrowserRunnerInputTests(unittest.TestCase):
    def test_only_bounded_utf8_bytes_are_accepted(self):
        for payload in (b'', '<p>text</p>', b'\xff', b'x' * (browser_runner.MAX_SOURCE_BYTES + 1)):
            with self.subTest(payload_type=type(payload), size=len(payload)):
                with self.assertRaises(InvalidUpdate):
                    browser_runner.run(payload)

    def test_unavailable_browser_never_executes_source(self):
        with patch.object(browser_runner, '_trusted_browser', return_value=False), \
                patch.object(browser_runner, '_run_isolated') as run:
            result = browser_runner.run(b'<p>Offline</p>')
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['observations'], [])
        run.assert_not_called()


@unittest.skipUnless(browser_runner.available(), 'The live browser probe uses macOS Google Chrome')
class BrowserRunnerLiveTests(unittest.TestCase):
    def test_native_chrome_observes_fixture_without_http_egress(self):
        hits = []

        class Listener(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *_args):
                pass

        with ThreadingHTTPServer(('127.0.0.1', 0), Listener) as server:
            endpoint = 'http://127.0.0.1:' + str(server.server_address[1])
            # Prove the listener accepts a request before checking that Chrome never reaches it.
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with socket.create_connection(server.server_address, timeout=2) as client:
                    client.sendall(b'GET /control HTTP/1.0\r\n\r\n')
                    self.assertIn(b'200', client.recv(512))
                self.assertEqual(hits, ['/control'])
                html = f'''<!doctype html><html><meta name="viewport" content="width=device-width">
                    <title>Fixture</title><div>```html</div>
                    <canvas id="watch" style="width:560px;height:300px"></canvas>
                    <button id="btnMode" onclick="this.innerText='Sidereal'">Tropical</button>
                    <img src="{endpoint}/image">
                    <script>
                      fetch('{endpoint}/fetch').catch(() => {{}});
                      fetch('https://browser-guard-test.invalid/remote').catch(() => {{}});
                      window.open('{endpoint}/popup');
                      throw new Error('test browser observation');
                    </script></html>'''.encode()
                result = browser_runner.run(html)
            finally:
                server.shutdown()
                thread.join(timeout=2)
        self.assertEqual(result['status'], 'observed', result)
        self.assertTrue(result['engine'].startswith('Chrome/'))
        self.assertEqual(hits, ['/control'])
        observations = result['observations']
        self.assertTrue(any(item['kind'] == 'layout' and 'outside' in item['text']
                            for item in observations), observations)
        self.assertTrue(any(item['kind'] == 'interaction' and item['selectors'] == ['btnMode']
                            for item in observations), observations)
        self.assertTrue(any(item['kind'] == 'console' and 'test browser observation' in item['text']
                            for item in observations), observations)
        self.assertTrue(any(item['kind'] == 'network' for item in observations), observations)

    def test_cancellation_terminates_owned_chrome_process(self):
        pids = []
        kill_group = browser_runner._kill_group

        def record_cleanup(pid):
            pids.append(pid)
            return kill_group(pid)

        def interrupt(client):
            client.command('Browser.getVersion')
            raise Interrupted('Browser probe test cancelled')

        with patch.object(browser_runner, '_probe', side_effect=interrupt), \
                patch.object(browser_runner, '_kill_group', side_effect=record_cleanup), \
                self.assertRaises(Interrupted):
            browser_runner.run(b'<p>Cancelled</p>')
        self.assertEqual(len(pids), 1)
        with self.assertRaises(ProcessLookupError):
            os.kill(pids[0], 0)

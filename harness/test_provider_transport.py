"""Observed transport progress is distinct from inference and measured usage."""
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from invocation_gateway import consume_dispatch
from local_model import LocalModel, transport_observation
from model_packet import build
import test_conversation as fixture
import conversation
import review_budget


class TransportTests(unittest.TestCase):
    def run_call(self, connection, directory):
        model=LocalModel(Path(directory))
        with patch('invocation_gateway.consume_dispatch'), patch('http.client.HTTPConnection',return_value=connection):
            return model.call('probe','system',{'request':'PRIVATE_REQUEST_TEXT'}, {})

    def test_timeout_before_headers_records_wait_without_claiming_generation(self):
        connection=MagicMock();connection.getresponse.side_effect=socket.timeout('fixture header timeout')
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(socket.timeout) as caught:self.run_call(connection,directory)
            record=caught.exception.call_record
            self.assertEqual(record['transport'],dict(version=1,phase='response_headers',headers_received=False,
                frames_received=0,content_bytes=0,reasoning_bytes=0))
            trace=json.loads((Path(directory)/'probe.json').read_text())
            self.assertEqual(trace['transport'],record['transport'])
            self.assertNotIn('PRIVATE_REQUEST_TEXT',json.dumps(trace))
            self.assertNotIn('answer',record)
            self.assertNotIn('response',record)
            connection.close.assert_called_once()

    def test_actual_http_header_stall_is_distinguished_from_streaming(self):
        received=threading.Event();release=threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                received.set();release.wait(2)
                try:
                    self.send_response(200);self.end_headers()
                    self.wfile.write(b'{"message":{"content":"{}"},"done":true}\n')
                except (BrokenPipeError,ConnectionResetError):pass
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                model=LocalModel(Path(directory),port=server.server_port)
                with patch('invocation_gateway.consume_dispatch'),patch('lifecycle.timeout',return_value=.08):
                    with self.assertRaises(socket.timeout) as caught:
                        model.call('queued-probe','system',{}, {})
                self.assertTrue(received.is_set())
                state=caught.exception.call_record['transport']
                self.assertEqual(state['phase'],'response_headers')
                self.assertFalse(state['headers_received']);self.assertEqual(state['frames_received'],0)
        finally:
            release.set();server.shutdown();server.server_close();thread.join(2)

    def test_partial_stream_is_not_reported_as_no_output_or_measured_tokens(self):
        response=MagicMock();response.status=200
        response.readline.side_effect=[json.dumps({'message':{'thinking':'Check 🧵'}}).encode()+b'\n',
            json.dumps({'message':{'content':'{"answer":'}}).encode()+b'\n',socket.timeout('fixture stream timeout')]
        connection=MagicMock();connection.getresponse.return_value=response
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(socket.timeout) as caught:self.run_call(connection,directory)
            record=caught.exception.call_record;state=record['transport']
            self.assertEqual(state['phase'],'stream');self.assertTrue(state['headers_received'])
            self.assertEqual(state['frames_received'],2)
            self.assertEqual(state['reasoning_bytes'],len('Check 🧵'.encode()))
            self.assertEqual(state['content_bytes'],len('{"answer":'.encode()))
            self.assertNotIn('response',record);self.assertNotIn('answer',record)

    def test_completed_stream_keeps_terminal_usage_and_progress(self):
        response=io.BytesIO(json.dumps({'message':{'content':'{"answer":"done"}'},'done':True,
            'done_reason':'stop','prompt_eval_count':20,'eval_count':9}).encode()+b'\n');response.status=200
        connection=MagicMock();connection.getresponse.return_value=response
        with tempfile.TemporaryDirectory() as directory:
            answer,record=self.run_call(connection,directory)
            self.assertEqual(answer,{'answer':'done'});self.assertEqual(record['transport']['phase'],'complete')
            self.assertEqual(record['response']['eval_count'],9)

    def test_optional_observation_is_bounded_typed_and_contains_no_text(self):
        valid=dict(version=1,phase='response_headers',headers_received=False,frames_received=0,
                   content_bytes=0,reasoning_bytes=0)
        self.assertEqual(transport_observation({'transport':dict(valid,secret='DO_NOT_STORE')}),valid)
        for change in ({'version':True},{'phase':'invented'},{'headers_received':'false'},
                       {'frames_received':True},{'frames_received':-1},{'content_bytes':2**53},
                       {'content_bytes':1},{'phase':'stream'}):
            self.assertIsNone(transport_observation({'transport':dict(valid,**change)}))
        self.assertIsNone(transport_observation({}))


class TimedProvider(fixture.Fake):
    model_interface=1
    def call(self,cid,system,payload,schema):
        consume_dispatch(cid,self.model,system,payload,schema,packet=build(system,payload,schema))
        error=socket.timeout('fixture header timeout')
        error.call_record={'call_id':cid,'ok':False,'transport':dict(version=1,phase='response_headers',
            headers_received=False,frames_received=0,content_bytes=0,reasoning_bytes=0)}
        raise error


class ReceiptTests(unittest.TestCase):
    setUp=fixture.ConversationTests.setUp
    tearDown=fixture.ConversationTests.tearDown
    def test_failure_progress_is_receipted_and_does_not_refund_unmeasured_call(self):
        out=conversation.answer(self.s,self.meta,self.storage,
            {'prompt':'Hello Karma - how are you?','models':fixture.MODELS,'tier':'auto'},TimedProvider())
        result=out['result'];self.assertFalse(result['admission']['admitted'])
        self.assertEqual(result['failure_kind'],'timeout')
        self.assertEqual(result['transport_failure']['phase'],'response_headers')
        raw=self.s.db.execute('SELECT record_json FROM invocation_receipts WHERE invocation_id=?',
                             (result['invocation_id'],)).fetchone()[0]
        receipt=json.loads(raw)
        self.assertEqual(receipt['transport_failure'],result['transport_failure'])
        self.assertEqual(receipt['accounting_settlement']['basis'],'reserved_unmeasured')
        self.assertGreater(receipt['accounting_settlement']['used_tokens'],0)
        self.assertIsNone(receipt['token_usage']['output_tokens'])
        self.assertEqual(review_budget.usage(self.s,out['session'])['used']['calls'],1)
        self.assertTrue(self.s.verify_invocations(out['session']))


if __name__=='__main__':unittest.main()

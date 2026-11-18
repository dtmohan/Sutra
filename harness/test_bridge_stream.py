import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import lifecycle
from ram_compartments import Compartments, decode
import vscode_bridge as bridge


class BridgeStreamTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.storage = self.root / 'store'
        self.storage.mkdir()

    def stream(self, requests):
        source = io.StringIO(''.join(json.dumps(row)+'\n' for row in requests))
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(errors):
            bridge.request_stream(self.storage, self.workspace, source, output)
        rows = [json.loads(raw) for raw in output.getvalue().splitlines()]
        return rows, errors.getvalue()

    def allocation(self, root, cache):
        cache.resize(65536)
        return {'admitted':True,'decision':'ALLOW'}

    def test_environment_is_isolated_and_activity_end_is_emitted_per_request(self):
        envelopes = [
            {'request':{'op':'probe'},'environment':{'SUTRA_THINKING':'on','SUTRA_STRATA_PORT':'19483'}},
            {'request':{'op':'probe'},'environment':{'SUTRA_THINKING':'off','SUTRA_STRATA_PORT':None}},
            {'request':{'op':'probe'},'environment':{}},
        ]
        def handle(*args):
            return {'thinking':os.environ.get('SUTRA_THINKING'),'port':os.environ.get('SUTRA_STRATA_PORT')}
        with patch.dict(os.environ,{'SUTRA_THINKING':'adaptive','SUTRA_STRATA_PORT':'19000'}), \
                patch.object(bridge,'handle',side_effect=handle), \
                patch.object(bridge,'cache_allocation',side_effect=self.allocation):
            rows, stderr = self.stream(envelopes)
            self.assertEqual(os.environ['SUTRA_THINKING'],'adaptive')
            self.assertEqual(os.environ['SUTRA_STRATA_PORT'],'19000')
        self.assertEqual([r['data']['thinking'] for r in rows],['on','off','adaptive'])
        self.assertEqual([r['data']['port'] for r in rows],['19483',None,'19000'])
        self.assertEqual(stderr.count('SUTRA_BRIDGE_REQUEST_END'),3)
        self.assertEqual(len({r['host_memory']['worker_pid'] for r in rows}),1)
        self.assertIsNone(lifecycle._deadline.get())

    def test_each_operation_opens_and_closes_its_own_ledger(self):
        ledgers = []
        real = bridge.InvocationLedger
        def opened(path):
            store = real(path)
            store.close = unittest.mock.Mock(wraps=store.close)
            ledgers.append(store)
            return store
        with patch.object(bridge,'InvocationLedger',side_effect=opened), \
                patch.object(bridge,'cache_allocation',side_effect=self.allocation):
            rows, _ = self.stream([{'request':{'op':'state'},'environment':{}}]*2)
        self.assertTrue(all(row['ok'] for row in rows))
        self.assertGreaterEqual(len(ledgers),2)
        self.assertEqual(len({id(ledger) for ledger in ledgers}),len(ledgers))
        for ledger in ledgers:
            ledger.close.assert_called_once()
        self.assertNotIn('host_memory',rows[0]['data'])

    def test_cache_retains_decoded_data_between_operations_and_survives_an_operation_error(self):
        count = 0
        def handle(*args):
            nonlocal count
            count += 1
            if count == 3:
                raise ValueError('Rejected request')
            return decode(('fixture','session-a'),'{"task":"Cedar"}')
        with patch.object(bridge,'handle',side_effect=handle), \
                patch.object(bridge,'cache_allocation',side_effect=self.allocation):
            rows, _ = self.stream([{'request':{'op':'state'},'environment':{}}]*4)
        self.assertEqual([row['ok'] for row in rows],[True,True,False,True])
        self.assertGreater(rows[1]['host_memory']['warm_hits'],0)
        self.assertEqual(rows[2]['host_memory']['retained_bytes'],0)
        self.assertEqual(rows[3]['data'],{'task':'Cedar'})

    def test_unsupported_environment_does_not_invoke_the_handler(self):
        with patch.object(bridge,'handle') as handle:
            rows, _ = self.stream([{'request':{'op':'execute'},'environment':{'PATH':'untrusted'}}])
        handle.assert_not_called()
        self.assertFalse(rows[0]['ok'])

    def test_interrupted_request_exits_worker_without_replay(self):
        with patch.object(bridge,'handle',side_effect=lifecycle.Interrupted('cancelled')) as handle, \
                patch.object(bridge,'cache_allocation',side_effect=self.allocation):
            rows, _ = self.stream([{'request':{'op':'execute'},'environment':{}}]*2)
        self.assertEqual(handle.call_count,1)
        self.assertEqual(len(rows),1)
        self.assertTrue(rows[0]['fatal'])
        self.assertIsNone(lifecycle._deadline.get())

    def test_pressure_discards_retained_ram_and_preserves_durable_files(self):
        cache = Compartments(65536)
        with cache.request('scope'):
            decode('page','{"value":1}')
        durable = self.storage/'retained'
        durable.write_text('task, permissions, consumed spending')
        hardware = {'admitted':False,'decision':'BRIDGE','reason':'Memory pressure'}
        with patch('monitoring.resource_state',return_value=hardware), \
                patch.dict(os.environ,{'SUTRA_RAM_CACHE_MIB':'64'}):
            admission = bridge.cache_allocation(self.storage, cache)
        self.assertFalse(admission['admitted'])
        self.assertEqual(cache.snapshot()['retained_bytes'],0)
        self.assertEqual(cache.snapshot()['max_bytes'],0)
        self.assertEqual(durable.read_text(),'task, permissions, consumed spending')

    def test_memory_guard_can_disable_ram_when_capacity_is_insufficient(self):
        sample = {'profile':'portable-memory-v1','available_bytes':4*1024**3}
        with patch('monitoring.resource_state',return_value={'admitted':True,'sample':sample}), \
                patch.dict(os.environ,{'SUTRA_RAM_CACHE_MIB':'64'}):
            admission = bridge.cache_allocation(self.storage, Compartments(1))
        self.assertFalse(admission['admitted'])
        self.assertNotEqual(admission['decision'],'ALLOW')


if __name__ == '__main__':
    unittest.main()

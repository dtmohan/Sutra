"""An unread page may request funds only after a conservative physical fit.

Temporary governed ledgers and deterministic providers; no inference or user
state. These tests distinguish financial capacity from model context capacity.
"""
import copy
import os
import unittest
from unittest.mock import patch

from lifecycle import Interrupted
from paging import Budget
import orchestration as machine
import orchestration_flow as flow
import review_budget
import review_discovery as discovery
import test_discovery_working_set as fixture
import workspace_allocation


class ConservativeFundingTests(unittest.TestCase):
    setUp = fixture.DiscoveryWorkingSetTests.setUp
    tearDown = fixture.DiscoveryWorkingSetTests.tearDown
    provider = fixture.DiscoveryWorkingSetTests.provider
    step = fixture.DiscoveryWorkingSetTests.step
    until = fixture.DiscoveryWorkingSetTests.until
    first_page = fixture.DiscoveryWorkingSetTests.first_page
    pay_to = fixture.DiscoveryWorkingSetTests.pay_to
    _pay_to = fixture.DiscoveryWorkingSetTests._pay_to

    def capture_active_shortage(self):
        out, parent, data, record = self.first_page()
        self.pay_to(out, 225000)
        self.discovery_budget = Budget(7000, 512, 256)
        worker = self.provider('synthesis').discovery_provider
        captured = {}
        def stop(*args, **kwargs):
            captured['args'] = args
            captured['kwargs'] = kwargs
            raise Interrupted('Capture the active page before a financial amendment')
        with machine.scope(parent['snapshot_owner']), patch.object(discovery, '_request_funding', side_effect=stop):
            with self.assertRaises(Interrupted):
                discovery.advance(self.s, self.meta['active'], parent, data, self.root, worker, False)
        return out, record, worker, captured

    def test_conservative_model_fits_then_existing_kernel_grants_without_refund_or_source_loss(self):
        out, record, worker, captured = self.capture_active_shortage()
        sid = self.meta['active']; before = review_budget.usage(self.s, sid)
        receipts = self.s.db.execute('SELECT id,record_json FROM invocation_receipts ORDER BY seq').fetchall()
        args = captured['args']; packet = args[4]
        self.assertEqual(packet['discovery_selection_version'], 3)
        self.assertFalse(packet['active_source_page']['partial_page'])
        self.assertEqual(sum(len(e['text'].encode()) for e in packet['evidence']), 6762)
        with machine.scope(args[2]['id']):
            with self.assertRaises(machine.OrchestrationStop) as stopped:
                discovery._request_funding(*args, **captured['kwargs'])
        self.assertEqual(stopped.exception.frame['limiting_dimension'], 'tokens')
        self.assertEqual(review_budget.usage(self.s, sid), before)
        with patch.dict(os.environ, {workspace_allocation.ENVIRONMENT_KEY: '{}'}):
            renewed = flow.budget_pause(self.s, self.meta, self.root, {'armed': True}, out['flow_id'],
                'SYNTHESIS', {'status': 'task_budget', 'stage': 'SYNTHESIS', 'frame': stopped.exception.frame})
        self.assertEqual(renewed['state'], 'SYNTHESIS')
        after = review_budget.usage(self.s, sid)
        self.assertGreater(after['limits']['tokens'], before['limits']['tokens'])
        self.assertEqual(after['used'], before['used'])
        self.assertEqual(self.s.db.execute('SELECT id,record_json FROM invocation_receipts ORDER BY seq').fetchall(), receipts)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM invocations i LEFT JOIN invocation_receipts r ON r.invocation_id=i.id WHERE r.id IS NULL').fetchone()[0], 0)
        row = self.s.db.execute("SELECT payload_json FROM page_audit WHERE event='DISCOVERY_ACTIVE_PAGE_FIT' ORDER BY seq DESC LIMIT 1").fetchone()
        self.assertIsNotNone(row)
        self.assertIn('conservative_utf8_upper_bound', row[0])

    def test_physical_shortage_cannot_request_money_or_change_spending(self):
        _, _, worker, captured = self.capture_active_shortage()
        sid = self.meta['active']; before = review_budget.usage(self.s, sid)
        worker.context_metadata = {'model_info': {'fixture.context_length': 4096}, '_sutra_runtime_cap': 4096}
        with machine.scope(captured['args'][2]['id']), \
                patch.object(machine, 'gates', side_effect=AssertionError('Physical overflow cannot ask for funds')):
            self.assertIs(discovery._request_funding(*captured['args'], **captured['kwargs']), False)
        self.assertEqual(review_budget.usage(self.s, sid), before)


if __name__ == '__main__': unittest.main()

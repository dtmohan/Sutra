import copy
import unittest

from budget_accounting import settlement, used
from context_manager import InvalidUpdate
from token_usage import measured


class DrainedEngineAccountingTests(unittest.TestCase):
    def setUp(self):
        self.envelope = {'model_interface': {'version': 1}, 'charge': 1000,
                         'budget_profile': {'response_reserve': 500}}
        self.record = {'provider': 'sutra_engine', 'ok': False, 'error': 'TimeoutError: time slice',
                       'response': {'done': False, 'done_reason': 'cancelled', 'usage_finalized': True,
                                    'prompt_eval_count': 220, 'eval_count': 70}}

    def test_native_drained_timeout_releases_only_unconsumed_reservation(self):
        value = settlement(self.envelope, self.record)
        self.assertEqual(value['basis'], 'runtime_measured')
        self.assertEqual(value['used_tokens'], 290)
        self.assertEqual(value['released_unused_reservation'], 1210)
        receipt = {'accounting_settlement': value, 'token_usage': measured(self.record)}
        self.assertEqual(used(self.envelope, receipt), 290)
        # A changed receipt cannot turn these observed counts into a budget refund.
        receipt['token_usage']['output_tokens'] = 0
        with self.assertRaisesRegex(InvalidUpdate, 'differs from runtime counts'):
            used(self.envelope, receipt)

    def test_other_providers_and_unmeasured_timeouts_keep_the_reservation(self):
        variants = [None, {}, dict(self.record, provider='ollama'), dict(self.record, provider='strata')]
        for field, value in (('usage_finalized', False), ('usage_finalized', 1),
                             ('prompt_eval_count', None), ('prompt_eval_count', True),
                             ('eval_count', None), ('eval_count', -1)):
            record = copy.deepcopy(self.record)
            record['response'][field] = value
            variants.append(record)
        for record in variants:
            with self.subTest(record=record):
                value = settlement(self.envelope, record)
                self.assertEqual(value['basis'], 'reserved_unmeasured')
                self.assertEqual(value['used_tokens'], 1500)
                self.assertEqual(value['released_unused_reservation'], 0)

    def test_preexisting_protocol_records_retain_their_original_accounting(self):
        old = dict(self.envelope, model_interface=None)
        self.assertIsNone(settlement(old, self.record))
        self.assertEqual(used(old, {}), 1500)
        completed = {'response': {'done': True, 'prompt_eval_count': 220, 'eval_count': 70}}
        self.assertEqual(settlement(self.envelope, completed)['used_tokens'], 290)


if __name__ == '__main__': unittest.main()

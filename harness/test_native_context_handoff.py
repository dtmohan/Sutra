"""Offline end-to-end native context and source-directory regressions."""
import copy
import unittest

import conversation
from calibration import current
from context_manager import ContextBudgetExceeded, InvalidUpdate, canonical
from invocation_gateway import InvocationLedger
from model_packet import build
from native_runtime import turn
from paging import Budget
from review_budget import DEFAULTS, usage
from test_conversation import Fake
import test_host_actions as fixtures
from test_native_runtime import NativeModel
from vscode_bridge import dispatch, handle, state


class NativeContextHandoffTests(unittest.TestCase):
    def setUp(self):
        fixtures.ActionTests.setUp(self)
        self.models = {'low': 'model-a', 'mid': 'model-b', 'high': 'model-b'}
        constraints = {**self.request['constraints'], **DEFAULTS, 'native_task': True, 'review_calls': 6}
        handle(self.storage, self.workspace, {'op': 'save', 'goal': self.request['goal'],
                                              'constraints': constraints})

    def tearDown(self):
        fixtures.ActionTests.tearDown(self)

    def request_for(self, prompt, **fields):
        return dict(session=self.session, contract_hash=self.s.origin(self.session)['contract_hash'],
                    calibration_hash=current(self.s, self.session)['hash'], prompt=prompt,
                    models=self.models, tier='low', **fields)

    def conversation_handoff(self, prompt):
        prior = conversation.answer(self.s, self.meta, self.storage,
            {'prompt': 'Keep Python 3.9 compatibility. Never add runtime dependencies.',
             'models': self.models, 'tier': 'low'}, Fake('model-a'))
        latest = conversation.answer(self.s, self.meta, self.storage,
            {'prompt': prompt, 'parent': prior['result']['invocation_id'],
             'models': self.models, 'tier': 'low'}, Fake('model-a'))
        return latest['result']['invocation_id']

    def test_native_reload_and_model_switch_keep_actual_user_context_and_charges(self):
        prompt = 'Use our prior criteria to review src/retry.py.'
        iid = self.conversation_handoff(prompt)
        request = self.request_for(prompt, conversation_id=iid)
        contract = self.s.current_contract(self.session)
        first = NativeModel('model-a')
        result = turn(self.s, self.meta, self.workspace, self.storage, request, first)
        self.assertTrue(result['result']['admission']['admitted'], result)
        packet = first.calls[0]
        self.assertEqual(packet['conversation_context_version'], 1)
        self.assertIn('Never add runtime dependencies.', canonical(packet['memory']['pinned_user_statements']))
        self.assertIn(prompt, canonical(packet['memory']['recent_turns']))
        wire = build(__import__('coding_role').SYSTEM, packet, __import__('coding_role').coding_schema(packet))
        self.assertIn('Keep Python 3.9 compatibility.', canonical(wire['messages']))
        from memory_layer import _wire_refs
        refs = _wire_refs(packet, {'role': 'coder', 'model_interface': 1})
        self.assertTrue(any(key == 'wire:src/retry.py#1' for key, _, _ in refs))
        self.assertTrue(any(key.endswith('/memory') for key, _, _ in refs))
        host_only = copy.deepcopy(packet)
        host_only['memory']['host_private_not_delivered'] = 'Not part of the model context.'
        self.assertEqual(_wire_refs(host_only, {'role': 'coder', 'model_interface': 1}), refs)
        self.assertEqual(self.s.current_contract(self.session), contract)
        spent = usage(self.s, self.session)
        self.assertEqual(spent['used']['calls'], 1)
        prefix = self.s.db.execute('SELECT id FROM invocation_receipts ORDER BY rowid').fetchall()

        self.s.close()
        self.s = InvocationLedger(self.storage / 'state.sqlite')
        second = NativeModel('model-b')
        out = turn(self.s, self.meta, self.workspace, self.storage, dict(request, tier='high'), second)
        self.assertTrue(out['result']['admission']['admitted'], out)
        self.assertEqual(second.calls[0]['memory'], packet['memory'])
        self.assertEqual(self.s.current_contract(self.session), contract)
        self.assertEqual(usage(self.s, self.session)['used']['calls'], 2)
        self.assertEqual(self.s.db.execute('SELECT id FROM invocation_receipts ORDER BY rowid').fetchall()[:len(prefix)], prefix)
        self.assertTrue(self.s.verify_invocations(self.session))

    def test_required_page_outside_first_directory_window_is_supplied(self):
        filler = ''.join(f'# filler {n:04d} ' + 'x' * 64 + '\n' for n in range(400))
        (self.workspace / 'src/many.py').write_text(filler)
        constraints = dict(self.s.snapshot(self.session)['constraints'],
                           allowed_paths=['src/many.py', 'src/retry.py', 'tests/test_retry.py'])
        handle(self.storage, self.workspace, {'op': 'save', 'goal': self.request['goal'],
                                              'constraints': constraints})
        model = NativeModel('model-a')
        request = self.request_for('Review the requested source page.', pages=['src/retry.py#1'])
        result = turn(self.s, self.meta, self.workspace, self.storage, request, model)
        self.assertTrue(result['result']['admission']['admitted'], result)
        packet = model.calls[0]
        self.assertGreater(packet['toc_total'], 24)
        self.assertNotIn('src/retry.py#1', [p['id'] for p in packet['toc']])
        self.assertIn('src/retry.py#1', [p['id'] for p in packet['pages']])
        self.assertEqual(packet['toc_next_offset'], 24)

        next_model = NativeModel('model-a')
        turn(self.s, self.meta, self.workspace, self.storage,
             dict(request, toc_offset=packet['toc_next_offset']), next_model)
        self.assertEqual(next_model.calls[0]['toc_offset'], 24)
        all_pages = [p for row in state(self.s, self.meta, self.workspace)['sources'] for p in row['pages']]
        self.assertEqual([p['id'] for p in next_model.calls[0]['toc']], [p['id'] for p in all_pages[24:48]])

    def test_explicit_file_and_five_requested_pages_are_not_replaced_by_keyword_sample(self):
        (self.workspace / 'src/retry.py').write_text(''.join('# keep source line %03d\n' % n for n in range(210)))
        catalog = state(self.s, self.meta, self.workspace)['sources'][0]['pages']
        self.assertGreater(len(catalog), 4)
        model = NativeModel('model-a')
        out = turn(self.s, self.meta, self.workspace, self.storage,
                   self.request_for('Review src/retry.py.'), model)
        self.assertTrue(out['result']['admission']['admitted'], out)
        self.assertEqual({p['id'] for p in model.calls[0]['pages']}, {p['id'] for p in catalog})
        selected = [p['id'] for p in catalog[:5]]
        manual = NativeModel('model-a')
        out = dispatch(self.s, self.meta, self.workspace, self.storage,
            {'op': 'work', 'mode': 'review', 'prompt': 'Review these exact pages.',
             'model': manual.model, 'pages': selected}, manual)
        self.assertTrue(out['result']['admission']['admitted'], out)
        self.assertEqual([p['id'] for p in manual.calls[0]['pages']], selected)

    def test_missing_context_cannot_be_hidden_to_fit_smaller_model(self):
        prompt = 'Review the same requested source.'
        iid = self.conversation_handoff(prompt)
        provider = NativeModel('model-a')
        provider.budget = Budget(1536, 512, 128)
        before = usage(self.s, self.session)
        with self.assertRaises(ContextBudgetExceeded):
            turn(self.s, self.meta, self.workspace, self.storage,
                 self.request_for(prompt, conversation_id=iid), provider)
        self.assertFalse(provider.calls)
        self.assertEqual(usage(self.s, self.session), before)

    def test_forged_memory_and_invalid_page_addresses_do_not_grant_access(self):
        prompt = 'Review with our earlier criteria.'
        iid = self.conversation_handoff(prompt)
        provider = NativeModel('model-a')
        turn(self.s, self.meta, self.workspace, self.storage,
             self.request_for(prompt, conversation_id=iid), provider)
        packet = copy.deepcopy(provider.calls[0])
        packet['memory']['pinned_user_statements'] = []
        with self.assertRaisesRegex(InvalidUpdate, 'memory differs'):
            __import__('review_role').check_request(self.s, self.session, packet)
        untouched = NativeModel('model-a')
        before = usage(self.s, self.session)
        for fields in ({'pages': ['../private.py#1']}, {'toc_offset': 999999}):
            with self.subTest(fields=fields), self.assertRaises(InvalidUpdate):
                turn(self.s, self.meta, self.workspace, self.storage,
                     self.request_for(prompt, **fields), untouched)
        self.assertFalse(untouched.calls)
        self.assertEqual(usage(self.s, self.session), before)


if __name__ == '__main__':
    unittest.main()

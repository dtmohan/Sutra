from dataclasses import FrozenInstanceError
import hashlib
import json
import math
from pathlib import Path
import unittest
from unittest.mock import patch
from kernel import STPKernel, Decision
from kernel_adapter import HarnessKernelAdapter, budget_decision
from stp_canonical.kernel import STPKernel as SuppliedKernel
from governance import policy_hash, POLICY_FILES
from context_manager import InvalidUpdate


class CanonicalKernelTests(unittest.TestCase):
    def test_canonical_source_is_byte_for_byte_pinned(self):
        root=Path(__file__).parent
        manifest=json.loads((root/'canonical-kernel.json').read_text())
        self.assertEqual(hashlib.sha256((root/'stp_canonical/kernel.py').read_bytes()).hexdigest(),manifest['sha256'])

    def test_1200_valid_threshold_and_priority_combinations(self):
        count=0
        for k in (.05,.1,.15,.3,.75):
            for ceiling in (.5,1.,2.):
                adapter=STPKernel(k,ceiling);supplied=SuppliedKernel(k,ceiling)
                omegas=[0.,math.nextafter(k,0),k,math.nextafter(k,math.inf),2*k,math.nextafter(2*k,math.inf),10*k,1e6]
                sigmas=[-2*ceiling,-ceiling,math.nextafter(-ceiling,0),-.5*ceiling,0.,.5*ceiling,
                        math.nextafter(ceiling,0),ceiling,math.nextafter(ceiling,math.inf),2*ceiling]
                for omega in omegas:
                    for sigma in sigmas:
                        # Descending severity oracle, independent of upstream's
                        # ascending omega comparisons after the compound gate.
                        expected=Decision.COMPOUND_FIN if abs(sigma)>=ceiling else Decision.FIN if omega>2*k else Decision.BRIDGE if omega>k else Decision.ALLOW
                        with self.subTest(k=k,ceiling=ceiling,omega=omega,sigma=sigma):
                            self.assertEqual(supplied.evaluate(omega,sigma).decision,expected)
                            self.assertEqual(adapter.evaluate(omega,sigma),supplied.evaluate(omega,sigma))
                        count+=1
        self.assertEqual(count,1200)

    def test_compound_precedes_zero_deviation_allow(self):
        for sigma in (-1.,1.,2.):self.assertEqual(STPKernel().evaluate(0,sigma).decision,Decision.COMPOUND_FIN)

    def test_compound_precedes_simultaneous_ordinary_fin(self):
        for sigma in (-1.,1.,2.):self.assertEqual(STPKernel().evaluate(.8,sigma).decision,Decision.COMPOUND_FIN)
        self.assertEqual(STPKernel().evaluate(.8,.99).decision,Decision.FIN)

    def test_equality_boundaries_match_canonical_code(self):
        k=STPKernel();self.assertEqual(k.evaluate(.15,0).decision,Decision.ALLOW)
        self.assertEqual(k.evaluate(.30,0).decision,Decision.BRIDGE)
        self.assertEqual(k.evaluate(math.nextafter(.30,math.inf),0).decision,Decision.FIN)
        self.assertEqual(k.evaluate(.1,1).decision,Decision.COMPOUND_FIN)

    def test_invalid_measurements_rejected_before_any_decision(self):
        for bad in (math.nan,math.inf,-math.inf,None,'0',True,False,10**1000):
            for omega,sigma in ((bad,0),(0,bad),(bad,2)):
                with self.subTest(omega=str(omega)[:20],sigma=str(sigma)[:20]),self.assertRaises(ValueError):
                    STPKernel().evaluate(omega,sigma)
        with self.assertRaises(ValueError):STPKernel().evaluate(-.1,0)

    def test_invalid_thresholds_and_overflow_rejected(self):
        for bad in (0,-1,math.nan,math.inf,None,'0.15',True,1e308):
            with self.subTest(k=bad),self.assertRaises(ValueError):STPKernel(k=bad)
        for bad in (0,-1,math.nan,math.inf,None,'1',False):
            with self.subTest(ceiling=bad),self.assertRaises(ValueError):STPKernel(sigma_clause=bad)

    def test_adapter_configuration_is_frozen(self):
        k=STPKernel()
        with self.assertRaises(FrozenInstanceError):k.k=100
        self.assertIsInstance(k,HarnessKernelAdapter)

    def test_upstream_nan_gap_is_contained_without_editing_upstream(self):
        self.assertEqual(SuppliedKernel().evaluate(math.nan,0).decision,Decision.ALLOW)
        self.assertEqual(SuppliedKernel().evaluate(0,math.nan).decision,Decision.ALLOW)
        for omega,sigma in ((math.nan,0),(0,math.nan)):
            with self.assertRaises(ValueError):STPKernel().evaluate(omega,sigma)

    def test_logic_hash_includes_canonical_kernel_and_adapter(self):
        self.assertTrue({'kernel_adapter.py','stp_canonical/kernel.py','stp_canonical/__init__.py'}<=set(POLICY_FILES))
        original=Path.read_bytes;before=policy_hash()
        def changed(path):return original(path)+(b'\n# changed kernel' if path.name=='kernel.py' and path.parent.name=='stp_canonical' else b'')
        with patch.object(Path,'read_bytes',changed):self.assertNotEqual(before,policy_hash())

    def test_budget_gate_exhausts_at_exact_ceiling_with_zero_omega(self):
        for limit in (1,2,3,12,32):
            for used in range(limit):self.assertEqual(budget_decision(used,limit)['decision'],'ALLOW')
            exhausted=budget_decision(limit,limit)
            self.assertEqual(exhausted['omega'],0)
            self.assertEqual(exhausted['sigma'],1)
            self.assertEqual(exhausted['decision'],'COMPOUND_FIN')

    def test_budget_gate_rejects_missing_and_fractional_measurements(self):
        for used,limit in ((-1,2),(0,0),(None,1),(True,2),(1,False),(1.5,3),(1,2.5)):
            with self.subTest(used=used,limit=limit),self.assertRaises(ValueError):budget_decision(used,limit)

    def test_budget_measurement_is_recorded_before_provider_dispatch(self):
        import tempfile
        from invocation_gateway import InvocationLedger
        with tempfile.TemporaryDirectory() as tmp:
            s=InvocationLedger(Path(tmp)/'state.sqlite');s.create('task')
            run=s.begin_run('task','No network access.',[],None,purpose='parser',max_calls=1)
            measured=s.reserve(run,'DISPATCH',{'call_id':'one'})
            self.assertEqual(measured,budget_decision(0,1))
            with self.assertRaisesRegex(InvalidUpdate,'COMPOUND_FIN'):
                s.reserve(run,'DISPATCH',{'call_id':'two'})
            self.assertEqual(s.run(run)['events'][-1]['data']['budget_kernel'],measured)
            self.assertEqual(sum(e['kind']=='DISPATCH' for e in s.run(run)['events']),1)
            s.close()


if __name__=='__main__':unittest.main()

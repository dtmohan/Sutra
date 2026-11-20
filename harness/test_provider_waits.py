import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from monitoring import ResourceWait, wait_category
import strata_model


class ProviderWaitTests(unittest.TestCase):
    def test_provider_contention_and_readiness_are_distinct_from_pressure(self):
        environment={'KARMA_NODE':'/trusted/node','SUTRA_STRATA_MODELS':'["strata:profile/fixture"]'}
        for status,decision,category in [('busy',None,'runtime_busy'),('timeout',None,'runtime_timeout'),
                                        ('paused',None,'measurement_unavailable'),('paused','BRIDGE','model_admission')]:
            response=SimpleNamespace(returncode=0,stdout=json.dumps({'ok':False,'status':status,
                'decision':decision,'error':'Observed '+status}).encode())
            with self.subTest(status=status,decision=decision),patch.dict(os.environ,environment), \
                 patch('strata_model.subprocess.run',return_value=response):
                with self.assertRaises(ResourceWait) as stopped:
                    strata_model.native_command('activate','strata:profile/fixture')
                self.assertEqual(stopped.exception.category,category)
                self.assertFalse(stopped.exception.state['available'])
                self.assertEqual(stopped.exception.state['provider'],'strata')
                self.assertGreater(stopped.exception.state['observed_at'],0)

    def test_hardware_and_unknown_measurements_remain_separate(self):
        self.assertEqual(wait_category({'available':True,'sample':{'wall_time':1000}}),'hardware_pressure')
        self.assertEqual(wait_category({'available':False,'reason':'No sample'}),'measurement_unavailable')
        self.assertEqual(wait_category({'runtime':{'kind':'model_capacity'}}),'model_admission')

    def test_interrupted_runtime_ownership_is_not_a_missing_measurement(self):
        state = {'available': False, 'admitted': False,
                 'reason': 'An interrupted request may still be running.',
                 'runtime': {'kind': 'interrupted'}}
        self.assertEqual(wait_category(state), 'runtime_uncertain')
        self.assertEqual(ResourceWait(state).category, 'runtime_uncertain')

import json,os,tempfile,time,unittest,subprocess,sys
from pathlib import Path
from unittest.mock import patch
from context_manager import InvalidUpdate
from monitoring import resource_state
from platform_support import lock_file
import repository_tools as repo

class PortableTests(unittest.TestCase):
 def test_available_memory_profile_does_not_claim_thermal_health(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'telemetry.json';sample={'profile':'portable-memory-v1','wall_time':time.time(),'available_bytes':8*1024**3,'physical_memory_bytes':16*1024**3,'thermal_state':None,'memory_pressure_raw':None}
   p.write_text(json.dumps(sample));r=resource_state(tmp);self.assertTrue(r['admitted']);self.assertIn('unavailable',r['thermal_policy']);self.assertIsNone(r['sample']['thermal_state'])
   sample['available_bytes']=1024**3;p.write_text(json.dumps(sample));self.assertFalse(resource_state(tmp)['admitted'])
   sample['wall_time']-=20;p.write_text(json.dumps(sample));self.assertFalse(resource_state(tmp)['available'])
 def test_path_rules_apply_on_every_host(self):
  for name in ['C:/secret.py','a.py:secret','CON.py','sub/.. /file.py','sub/a.py.','../x.py','a\\b.py']:
   with self.subTest(name=name),self.assertRaises(InvalidUpdate):repo.normalized(name)
  self.assertEqual(repo.normalized('sub/a.py'),'sub/a.py')
 def test_host_read_and_exclusive_process_lock(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);(root/'sub').mkdir();(root/'sub/a.py').write_text('def f(): return 1\n');self.assertTrue(repo.read_bytes(root,'sub/a.py').startswith(b'def'))
   lock=root/'lock'
   code='from platform_support import lock_file\nfrom pathlib import Path\nwith lock_file(Path('+repr(str(lock))+')): pass'
   env=dict(os.environ,PYTHONPATH=str(Path(__file__).parent))
   with lock_file(lock):self.assertNotEqual(subprocess.run([sys.executable,'-c',code],env=env,capture_output=True).returncode,0)
   self.assertEqual(subprocess.run([sys.executable,'-c',code],env=env,capture_output=True).returncode,0)
 def test_host_rejects_hardlinks(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);(root/'a.py').write_text('x=1');os.link(root/'a.py',root/'b.py')
   with self.assertRaises(InvalidUpdate):repo.read_bytes(root,'b.py')

if __name__=='__main__':unittest.main()

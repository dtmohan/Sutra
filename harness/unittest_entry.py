"""Sandboxed test launcher. Its output is untrusted, including its result JSON.

Imported test/project code shares this process and can alter the runner. The
parent records process observations only, never assertion proof or completion.
"""
import importlib.util
import json
from pathlib import Path
import resource
import sys
import unittest

workspace=Path(sys.argv[1]);target=workspace/sys.argv[2]
resource.setrlimit(resource.RLIMIT_CORE,(0,0))
resource.setrlimit(resource.RLIMIT_NOFILE,(64,64))
resource.setrlimit(resource.RLIMIT_FSIZE,(4*1024*1024,4*1024*1024))
sys.path.insert(0,str(workspace))
spec=importlib.util.spec_from_file_location('karma_selected_test',target)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
suite=unittest.defaultTestLoader.loadTestsFromModule(module)
count=suite.countTestCases()
if not count:
    print('KARMA_TEST_RESULT '+json.dumps({'tests':0,'passed':False,'reason':'No unittest tests discovered'}));sys.exit(2)
result=unittest.TextTestRunner(verbosity=2).run(suite)
print('KARMA_TEST_RESULT '+json.dumps({'tests':result.testsRun,'passed':result.wasSuccessful(),
    'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped)}))
sys.exit(0 if result.wasSuccessful() else 1)

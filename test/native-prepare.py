import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(sys.argv[1])/'harness'))
from vscode_bridge import handle
from review_budget import DEFAULTS
root=Path(sys.argv[2]);workspace=Path(sys.argv[3])
c={**DEFAULTS,'native_task':True,'allowed_paths':['src/retry.py','tests/test_retry.py'],'network_allowed':False,'dependencies_allowed':False,'edits_allowed':True,'tests_allowed':True,'test_paths':['tests/test_retry.py'],'test_timeout_ms':10000,'max_actions':4,'review_calls':12}
handle(root,workspace,{'op':'new','goal':'Fix retry off-by-one, preserve MAX_ATTEMPTS=3 and test files, then run the existing tests. No network or new dependencies.','constraints':c})

import sys,json,importlib.util
from pathlib import Path
sys.path.insert(0,str(Path(sys.argv[1])/'harness'))
from vscode_bridge import handle
handle(Path(sys.argv[2]),Path(sys.argv[3]),{'op':'auto_new','goal':'Packaged runtime smoke test','roots':['.'],'review_calls':2})
source=Path(__file__).resolve().parent.parent
spec=importlib.util.spec_from_file_location('fixture_examples',source/'examples/make_synthetic_profiles.py')
examples=importlib.util.module_from_spec(spec);spec.loader.exec_module(examples)
folder=Path(sys.argv[2])/'synthetic-fixtures';examples.make(folder)
refs=[]
for adapter in ('smriti','karma'):
    p=json.loads((folder/(adapter+'-profile.json')).read_text())
    refs.append(handle(Path(sys.argv[2]),Path(sys.argv[3]),{'op':'calibration_register','profile':p})['registered'])
view=handle(Path(sys.argv[2]),Path(sys.argv[3]),{'op':'calibration_list'})
handle(Path(sys.argv[2]),Path(sys.argv[3]),{'op':'calibration_bind','profiles':refs,'expected_hash':view['binding']['hash']})
for ref in refs:
    adapter=ref['profile_id'].split('-')[0];sample=json.loads((folder/(adapter+'-sample.json')).read_text())
    result=handle(Path(sys.argv[2]),Path(sys.argv[3]),{'op':'calibration_evaluate','profile_ref':ref,'sample':sample})
    assert result['evaluation']['decision']=='BRIDGE'

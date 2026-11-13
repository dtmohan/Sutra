"""Create short-lived protocol fixtures. NOT an empirical calibrator."""
import argparse,hashlib,json,time
from pathlib import Path

def make(output):
    output.mkdir(parents=True,exist_ok=True);now=time.time()
    h=lambda text:hashlib.sha256(text.encode()).hexdigest()
    for adapter in ('smriti','karma'):
        p=dict(schema_version=1,profile_id=adapter+'-synthetic-example',version=1,adapter=adapter,
            domain='protocol-fixture',metric='declared_deviation',unit='fixture_points',
            measurement_method='synthetic-v1',producers=['fixture-A','fixture-B'],
            normalization=dict(method='linear_deviation',offset=0,scale=100,input_min=0,input_max=100,
                sigma_definition='Independent synthetic accumulation signal, already normalized; not statistical variance.'),
            kernel=dict(k=.15,sigma_clause=1.),valid_from=now-1,valid_until=now+86400,max_sample_age_seconds=60,
            evidence=dict(kind='synthetic',dataset_sha256=h('synthetic boundary fixture'),report_sha256=h('no empirical validation'),
                calibration_samples=10,validation_samples=5,procedure='Manually chosen synthetic examples; no fitted parameters.',
                objective='Exercise storage, binding and kernel branches only.',evaluator='synthetic-example-generator-v1'),
            limitations='NOT an empirically calibrated profile. Synthetic counts and hashes describe fixture data only. No semantic or tool authority.')
        sample=dict({key:p[key] for key in ('domain','metric','unit','measurement_method')},producer='fixture-A',
            value=20,sigma=0,observed_at=now,source='synthetic',evidence_sha256=h('synthetic value=20 sigma=0'))
        for name,value in ((adapter+'-profile',p),(adapter+'-sample',sample)):
            (output/(name+'.json')).write_text(json.dumps(value,indent=2)+'\n')

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('output',type=Path);args=parser.parse_args();make(args.output)

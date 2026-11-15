"""Versioned empirical MLX admission estimates, never a peak-memory guarantee."""
import json
import math
from pathlib import Path
import time
from context_manager import canonical,sha,InvalidUpdate

FORMAT='mlx-footprint-envelope-v1'
MARGIN_FRACTION=.25
MARGIN_MIN_BYTES=1024**3


def identity(model,tag,metadata,environment):
    return {'model':model,'manifest_digest':tag['digest'],'backend':'mlx',
            'metadata_sha256':sha(canonical({k:metadata.get(k) for k in ('details','parameters','model_info')})),
            'environment':environment}


def seal(document):
    document=dict(document)
    document['sha256']=sha(canonical(document))
    return document


def validate(p):
    if not isinstance(p,dict):raise InvalidUpdate('Memory profile must be an object')
    raw={k:v for k,v in p.items() if k!='sha256'}
    if p.get('sha256')!=sha(canonical(raw)) or p.get('format')!=FORMAT:
        raise InvalidUpdate('Memory profile integrity/version mismatch')
    if p.get('margin_fraction')!=MARGIN_FRACTION or p.get('margin_min_bytes')!=MARGIN_MIN_BYTES:
        raise InvalidUpdate('Unknown memory calibration margin')
    if p.get('method')!='cold_text_prefill_process_tree_v1' or p.get('backend_peak_verified') is not False:
        raise InvalidUpdate('Unknown memory measurement method')
    if not isinstance(p.get('identity'),dict) or p['identity'].get('backend')!='mlx':
        raise InvalidUpdate('Missing MLX identity')
    if type(p.get('created_at')) not in (int,float) or not math.isfinite(p['created_at']) or p['created_at']<=0 or p['created_at']>time.time()+300:
        raise InvalidUpdate('Invalid memory calibration timestamp')
    points=p.get('points')
    if not isinstance(points,list) or not 3<=len(points)<=12:
        raise InvalidUpdate('Memory calibration requires at least three workload sizes')
    last=0
    for point in points:
        if set(point)!={'covered_tokens','observed_increment_bytes','repeats','requested_contexts'}:
            raise InvalidUpdate('Invalid memory measurement fields')
        for key in ('covered_tokens','observed_increment_bytes','repeats'):
            if type(point[key])is not int or point[key]<=0:raise InvalidUpdate('Invalid memory measurement')
        if point['covered_tokens']<=last or point['covered_tokens']>65536 or point['repeats']<2:
            raise InvalidUpdate('Insufficient or unordered calibration coverage')
        contexts=point['requested_contexts']
        if not isinstance(contexts,list) or len(contexts)!=point['repeats'] or any(type(x)is not int or not point['covered_tokens']<=x<=65536 for x in contexts):
            raise InvalidUpdate('Invalid requested-context evidence')
        last=point['covered_tokens']
    if last<4096:raise InvalidUpdate('Insufficient context coverage')
    return p


def build(key,groups,evidence_sha256):
    points=[]
    for group in groups:
        if len(group)<2:raise InvalidUpdate('Two cold observations required per workload')
        if any(not r['eligible'] for r in group):raise InvalidUpdate('Contaminated or incomplete calibration observation')
        points.append({'covered_tokens':min(r['prompt_tokens']+r['output_tokens'] for r in group),
                       'observed_increment_bytes':max(r['increment_bytes'] for r in group),
                       'repeats':len(group),'requested_contexts':[r['requested_context_tokens'] for r in group]})
    p=seal({'format':FORMAT,'created_at':time.time(),'identity':key,
            'method':'cold_text_prefill_process_tree_v1','backend_peak_verified':False,
            'margin_fraction':MARGIN_FRACTION,'margin_min_bytes':MARGIN_MIN_BYTES,
            'evidence_sha256':evidence_sha256,'points':sorted(points,key=lambda p:p['covered_tokens'])})
    return validate(p)


def select(key,paths=None):
    if paths is None:
        import os
        paths=[Path(__file__).parent/'memory_profiles']
        if os.environ.get('SUTRA_RUNTIME_DIR'):paths.append(Path(os.environ['SUTRA_RUNTIME_DIR'])/'memory-profiles')
    matches=[];rejected=0
    for directory in paths:
        for path in sorted(Path(directory).glob('*.json'))[:100]:
            try:
                if path.stat().st_size>128*1024:raise ValueError('Oversized memory profile')
                p=validate(json.loads(path.read_text()))
                if p['identity']==key:matches.append(p)
            except (OSError,ValueError,TypeError,KeyError,InvalidUpdate):rejected+=1
    if not matches:return None,'No matching calibrated model/backend/hardware/server epoch'+(' (invalid profiles ignored)' if rejected else '')
    # No silent model/alias/version transfer. Use only the newest exact identity.
    return max(matches,key=lambda p:p['created_at']),'exact_identity_match'


def estimate(profile,context_tokens,weights):
    validate(profile)
    ceiling=next((p for p in profile['points'] if p['covered_tokens']>=context_tokens),None)
    if ceiling is None:return None
    # Use the upper neighbouring measured workload, never linear extrapolation.
    # Prefix maxima prevent sampling noise from implying larger contexts cost less.
    observed=max(weights,max(p['observed_increment_bytes'] for p in profile['points'] if p['covered_tokens']<=ceiling['covered_tokens']))
    margin=max(MARGIN_MIN_BYTES,math.ceil(observed*MARGIN_FRACTION))
    return {'bytes':observed+margin,'observed_bytes':observed,'margin_bytes':margin,
            'covered_tokens':ceiling['covered_tokens'],'profile_sha256':profile['sha256'],
            'method':profile['method'],'peak_memory_verified':False}

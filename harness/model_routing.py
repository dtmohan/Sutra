"""Versioned routing heuristic; no claim to measure semantic difficulty."""
import re
from context_manager import InvalidUpdate
TIERS=('low','mid','high')

def choose(prompt,models,mode,signals):
    if not isinstance(models,dict) or set(models)!=set(TIERS):raise InvalidUpdate('Configure all three local model tiers')
    for value in models.values():
        if not isinstance(value,str) or not value.strip() or len(value)>200 or 'cloud' in value.lower():raise InvalidUpdate('Each tier needs an installed local model name')
    if mode not in ('auto',*TIERS):raise InvalidUpdate('Unknown routing mode')
    if not isinstance(prompt,str) or not 1<=len(prompt.strip())<=1800:raise InvalidUpdate('Prompt must be 1..1800 characters')
    for key in ('source_files','selected_files','unresolved_calls'):
        if type(signals.get(key)) is not int or signals[key]<0:raise InvalidUpdate('Invalid routing measurement '+key)
    difficult=bool(re.search(r'\b(architecture|security|audit|concurrency|race|refactor|whole|entire)\b',prompt,re.I))
    broad=bool(re.search(r'\b(codebase|repository|dependencies|integration|compare|across)\b',prompt,re.I))
    # With no source evidence Auto always starts small. Workspace work may use
    # the existing breadth heuristic only after the host has paged fresh source.
    evidence=signals['source_files']>0 and signals['selected_files']>0
    tier=('high' if difficult else 'mid' if broad or len(prompt)>500 or signals['selected_files']>3 else 'low') if evidence else 'low'
    reason='explicit user override' if mode!='auto' else 'small-model first' if not evidence else 'fresh-source breadth assessment' if tier!='low' else 'focused request'
    if mode!='auto':tier=mode
    return {'policy':'local-tier-routing-v2','tier':tier,'model':models[tier],'mode':mode,'models':dict(models),
            'signals':dict(signals),'reason':reason,'semantic_complexity_verified':False,'automatic_escalation':mode=='auto'}

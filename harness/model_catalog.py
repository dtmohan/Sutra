"""Read installed Ollama metadata; no inference, downloads or cloud dispatch."""
import re

def catalog(ollama):
    rows=[];errors=[]
    for tag in ollama('/api/tags').get('models',[]):
        name=tag.get('name')
        if not isinstance(name,str) or not name or 'cloud' in name.lower():continue
        row={k:tag[k] for k in ('name','digest','size') if k in tag}
        try:
            data=ollama('/api/show',{'model':name})
            if data.get('remote_host') or data.get('remote_model'):continue
            details=data.get('details') or tag.get('details') or {};info=data.get('model_info') or {}
            capabilities=data.get('capabilities') or []
            base=bool(re.search(r'(autocomplete|(?:^|[-_:])base(?:$|[-_:]))',name,re.I)) or 'insert' in capabilities
            eligible='completion' in capabilities and not base
            count=info.get('general.parameter_count')
            if type(count) not in (int,float) or not 0<count<1e15:count=0
            architecture=str(info.get('general.architecture') or details.get('family') or '')
            moe='moe' in architecture.lower() or any('expert_count' in k and type(v) is int and v>1 for k,v in info.items())
            row.update(eligible=eligible,capabilities=capabilities,parameter_count=count,parameter_size=details.get('parameter_size',''),
                quantization=details.get('quantization_level',''),moe=moe,
                eligibility_reason='local chat model' if eligible else 'autocomplete/base model' if base else 'completion capability not declared')
        except Exception as exc:
            row.update(eligible=False,eligibility_reason='metadata unavailable');errors.append({'model':name,'error':str(exc)[:160]})
        rows.append(row)
    candidates=[m for m in rows if m['eligible']]
    candidates.sort(key=lambda m:(m.get('parameter_count') or m.get('size',0),m['name']))
    suggested={}
    if candidates:
        low=candidates[0];dense=[m for m in candidates if not m['moe']] or candidates
        high=max(dense,key=lambda m:(m.get('parameter_count') or m.get('size',0),m['name'].startswith('local-assistant')))
        middle=[m for m in candidates if m['moe']]
        mid=middle[0] if middle else candidates[len(candidates)//2]
        suggested={'low':low['name'],'mid':mid['name'],'high':high['name']}
    return {'models':rows,'suggested_tiers':suggested,'selection_policy':'local-metadata-prefill-v1',
            'selection_note':'Editable defaults: smallest chat model, MoE/middle model, largest dense model. Metadata is not a capability benchmark.',
            'errors':errors}

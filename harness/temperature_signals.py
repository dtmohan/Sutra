"""Validated, advisory CPU/GPU readings. These never change a kernel decision."""
import math
KEYS={'cpu':('Tp00','Tp04','Tp08','Tp0C','Tp0G','Tp0K','Tp0O','Tp0R','Tp0U','Tp0X','Tp0a','Tp0d','Tp0g','Tp0j','Tp0m','Tp0p','Tp0u','Tp0y'),
      'gpu':('Tg0U','Tg0X','Tg0d','Tg0g','Tg0j','Tg1Y','Tg1c','Tg1g')}

def number(v):
    try:return type(v) in (int,float) and math.isfinite(v)
    except OverflowError:return False
def unavailable(reason):return {'available':False,'reason':reason,'maximum_celsius':None,'mean_celsius':None,'sensors':[],'missing_keys':[]}

def summarize(sample,now):
    result={'source':'AppleSMC','profile':'mac17-13-m5-pro-v1','policy':'advisory only; OS thermal pressure remains the resource gate'}
    error=None
    if not isinstance(sample,dict):error='No CPU/GPU sensor sample from this collector'
    elif sample.get('source')!='AppleSMC' or sample.get('profile')!=result['profile'] or sample.get('read_only') is not True:error='Unrecognized sensor source or profile'
    elif sample.get('machine')!='Mac17,13' or sample.get('chip')!='Apple M5 Pro':error='Temperature mapping is not validated for this machine'
    elif sample.get('status')!='sampled':error='Sensor access unavailable: '+str(sample.get('status','unknown'))
    elif not number(sample.get('sampled_at')) or not number(now) or not 0<=now-sample['sampled_at']<=8:error='Sensor sample is stale or future-dated'
    if error:return dict(result,available=False,cpu=unavailable(error),gpu=unavailable(error))
    result['age_seconds']=round(now-sample['sampled_at'],2)
    for group,keys in KEYS.items():
        rows=sample.get(group);valid=isinstance(rows,list) and len(rows)<=len(keys);seen=set()
        if valid:
            for row in rows:
                if not isinstance(row,dict) or set(row)!=set(('key','celsius')) or row['key'] not in keys or row['key'] in seen or not number(row['celsius']) or not 0<row['celsius']<=150:valid=False;break
                seen.add(row['key'])
        if not valid:result[group]=unavailable('Invalid or unexpected sensor data');continue
        if not rows:result[group]=dict(unavailable('No readable '+group.upper()+' temperature sensors'),missing_keys=list(keys));continue
        values=[row['celsius'] for row in rows]
        result[group]={'available':True,'maximum_celsius':max(values),'mean_celsius':sum(values)/len(values),
                       'sensors':rows,'missing_keys':[key for key in keys if key not in seen],
                       'reason':'Maximum of currently readable mapped sensors; not a single die or per-core temperature'}
    result['available']=result['cpu']['available'] or result['gpu']['available'];return result

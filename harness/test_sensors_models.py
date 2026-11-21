import copy,math,unittest
from temperature_signals import summarize,KEYS
from karma_adapter import KarmaAdapter
from model_catalog import catalog

def sample():
    return {'source':'AppleSMC','profile':'mac17-13-m5-pro-v1','machine':'Mac17,13','chip':'Apple M5 Pro','read_only':True,
            'status':'sampled','sampled_at':100,'cpu':[{'key':'Tp00','celsius':45.5},{'key':'Tp04','celsius':47}],
            'gpu':[{'key':'Tg0U','celsius':43.25}]}

class TemperatureTests(unittest.TestCase):
    def test_group_max_and_mean_with_missing_sensor_coverage(self):
        s=summarize(sample(),101)
        self.assertEqual(s['cpu']['maximum_celsius'],47);self.assertEqual(s['cpu']['mean_celsius'],46.25)
        self.assertEqual(s['gpu']['maximum_celsius'],43.25);self.assertEqual(len(s['gpu']['missing_keys']),7)
        self.assertTrue(s['available']);self.assertEqual(s['age_seconds'],1)

    def test_stale_future_unsupported_and_failed_samples_show_unknown(self):
        for key,value in [('sampled_at',90),('sampled_at',102),('sampled_at',math.nan),('sampled_at',10**1000),
                          ('machine','Mac99,99'),('chip','Apple M4'),('source','model estimate'),('profile','other'),
                          ('status','open_failed'),('read_only',False)]:
            p=sample();p[key]=value;result=summarize(p,101)
            with self.subTest(key=key,value=str(value)[:30]):
                self.assertFalse(result['available']);self.assertIsNone(result['cpu']['maximum_celsius'])

    def test_bad_group_does_not_hide_independent_valid_group(self):
        for bad in (math.nan,math.inf,-1,0,151,True,'45',10**1000):
            p=sample();p['cpu'][0]['celsius']=bad;result=summarize(p,101)
            self.assertFalse(result['cpu']['available']);self.assertTrue(result['gpu']['available'])

    def test_unexpected_duplicate_or_missing_keys_cannot_forge_temperature(self):
        for rows in ([{'key':'TB0T','celsius':40}],sample()['cpu']*2,[{'key':['Tp00'],'celsius':40}],None,[]):
            p=sample();p['cpu']=rows;result=summarize(p,101);self.assertFalse(result['cpu']['available'])

    def test_sensor_dropout_does_not_reuse_previous_reading(self):
        self.assertTrue(summarize(sample(),101)['cpu']['available'])
        p=sample();p['cpu']=[];self.assertIsNone(summarize(p,101)['cpu']['maximum_celsius'])
        self.assertFalse(summarize(None,101)['available'])

    def test_celsius_does_not_change_kernel_or_os_pressure_gate(self):
        base={'wall_time':100,'thermal_state':0,'memory_pressure_raw':1,'free_bytes':8*1024**3,'inactive_bytes':0}
        adapter=KarmaAdapter();before=adapter.hardware(base,101)
        p=sample();p['cpu'][0]['celsius']=125;after=adapter.hardware(dict(base,temperatures=p),101)
        self.assertEqual(after['temperatures']['cpu']['maximum_celsius'],125)
        for key in ('omega','sigma','k','sigma_clause','decision','admitted'):self.assertEqual(before[key],after[key])
        hot=adapter.hardware(dict(base,thermal_state=3,temperatures=p),101)
        self.assertEqual(hot['decision'],'FIN');self.assertFalse(hot['admitted'])

def model(name,size,family='dense',caps=None,remote=False):
    return {'name':name,'size':size,'show':{'capabilities':caps or ['completion','tools'],'details':{'parameter_size':str(size),'family':family},
        'model_info':{'general.parameter_count':size,'general.architecture':family},'remote_host':'cloud.example' if remote else None}}

class ModelCatalogTests(unittest.TestCase):
    def run_catalog(self,models,fail=()):
        calls=[]
        def api(path,body=None):
            calls.append((path,body))
            if path=='/api/tags':return {'models':[{k:m[k] for k in ('name','size')} for m in models]}
            self.assertEqual(path,'/api/show')
            if body['model'] in fail:raise RuntimeError('metadata unavailable')
            return next(m['show'] for m in models if m['name']==body['model'])
        result=catalog(api);self.assertTrue(all(p in ('/api/tags','/api/show') for p,_ in calls));return result,calls

    def test_metadata_prefills_small_moe_and_large_dense(self):
        r,calls=self.run_catalog([model('small',4),model('moe',35,'qwen_moe'),model('large',27),model('base:1b-base',1,caps=['completion','insert'])])
        self.assertEqual(r['suggested_tiers'],{'low':'small','mid':'moe','high':'large'})
        self.assertEqual(len(calls),5);self.assertFalse(next(m for m in r['models'] if 'base' in m['name'])['eligible'])

    def test_remote_and_embedding_models_never_prefill(self):
        r,calls=self.run_catalog([model('remote:cloud',100),model('hidden-remote',40,remote=True),model('embed',1,caps=['embedding']),model('local',4)])
        self.assertEqual(set(r['suggested_tiers'].values()),{'local'})
        self.assertNotIn('hidden-remote',[m['name'] for m in r['models']]);self.assertFalse(any(b and b['model']=='remote:cloud' for _,b in calls))

    def test_failed_model_metadata_is_unknown_and_retryable(self):
        models=[model('good',4),model('bad',10)];r,_=self.run_catalog(models,fail=['bad'])
        self.assertEqual(set(r['suggested_tiers'].values()),{'good'});self.assertEqual(len(r['errors']),1)
        other,_=self.run_catalog(models);self.assertEqual(other['suggested_tiers']['high'],'bad')

    def test_empty_catalog_has_no_invented_default(self):
        r,calls=self.run_catalog([]);self.assertEqual(r['suggested_tiers'],{});self.assertEqual(len(calls),1)

    def test_small_single_model_can_fill_all_tiers(self):
        r,_=self.run_catalog([model('only',4)]);self.assertEqual(r['suggested_tiers'],dict(low='only',mid='only',high='only'))

    def test_catalog_reports_configured_context_without_dispatch(self):
        configured = model('configured', 27)
        configured['show']['model_info']['dense.context_length'] = 131072
        configured['show']['parameters'] = 'num_ctx 32768\n'
        tiny = model('tiny', 1)
        tiny['show']['model_info']['dense.context_length'] = 2048
        result, calls = self.run_catalog([configured, model('unknown', 4), tiny])
        rows = {row['name']: row for row in result['models']}
        self.assertEqual(rows['configured']['context_tokens'], 32768)
        self.assertEqual(rows['unknown']['context_tokens'], 8192)
        self.assertTrue(rows['tiny']['eligible'])
        self.assertNotIn('context_tokens', rows['tiny'])
        self.assertEqual(len(calls), 4)

if __name__=='__main__':unittest.main()

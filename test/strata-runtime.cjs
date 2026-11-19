'use strict';
const assert=require('node:assert/strict');
const runtime=require('../strata_runtime');

const ref=name=>'strata:profile/'+name;
const owner=name=>(name==='alpha'?'a':name==='beta'?'b':'c').repeat(32);
const instance=name=>(name==='alpha'?'d':name==='beta'?'e':'f').repeat(32);
function fixture({environment={},managerResult,foreign=false,mutateLoad,invalidPlan,invalidState,brokenProfile}={}){
 let saved={executable:'/native/bin/strata',environment:{STRATA_INSTALL_DIR:'/native/runtime',STRATA_NO_ACQUIRE:'1'},
   profile:'alpha',model:'shared',port:19483,cold_memory_bytes:20*1024**3};
 const env={SUTRA_STRATA_MODELS:JSON.stringify([ref('alpha'),ref('beta'),'strata:high']),
   SUTRA_STRATA_PORT:'19483',SUTRA_STRATA_MODEL:'shared',...environment};
 const profiles={alpha:{model:'shared',port:19483,context:32768,hash:'1'.repeat(64),revision:'1'.repeat(16)},
   beta:{model:'shared',port:19483,context:4096,hash:'2'.repeat(64),revision:'2'.repeat(16)},
   gamma:{model:'other',port:19484,context:8192,hash:'3'.repeat(64),revision:'3'.repeat(16)}};
 let active='alpha',writes=0;
 const commands=[],loads=[],admissions=[];
 const status=name=>({ok:true,profile:name,state:active===name?'ready':'stopped',owner_id:owner(name),instance_id:instance(name),
   generation_ready:true,api_identity_verified:true});
 const command=async(record,argv)=>{
  commands.push({profile:record.profile,argv});
  assert.equal(argv.at(-2),'--output');assert.equal(argv.at(-1),'json');
  if(argv[0]==='profile'&&argv[1]==='list')return {ok:true,profiles:Object.keys(profiles).map(id=>({id}))};
  if(argv[0]==='profile'&&argv[1]==='show'){
   const name=argv[2],p=profiles[name];
   if(!p)return {ok:false,error:'Not found'};
   if(name===brokenProfile)return {ok:false,error:'Bad native profile'};
   return {ok:true,profile:{id:name,source_config:'/native/'+name+'.json'},
    plan:{model_name:p.model,effective:{host:'127.0.0.1',port:p.port}}};
  }
  const name=record.profile,p=profiles[name];
  if(argv[1]==='start'){
   assert(argv.includes('--plan'),'Coordinator must never duplicate native start/stop lifecycle');
   return {ok:true,plan:{profile:name,source_sha256:p.hash,launch:{profile:name,profile_revision:p.revision,
    model_name:p.model,effective:{host:invalidPlan&&name==='beta'?'0.0.0.0':'127.0.0.1',port:p.port},
    engine_argv:['--max-context',String(p.context)]}}};
  }
  if(argv[1]==='status')return invalidState&&name==='beta'?{ok:false,profile:name}:status(name);
  assert.fail('Unexpected coordinator command '+argv.join(' '));
 };
 const inspectServer=async port=>{
  if(!active||profiles[active].port!==port){const e=new Error('No server');e.code='ECONNREFUSED';throw e;}
  return {service:foreign?'other':'strata',model:profiles[active].model,loaded:true,
   instance_id:instance(active),cache_max_tokens:profiles[active].context,activity:{in_flight:0}};
 };
 const admit=async bytes=>{admissions.push(bytes);return {admitted:true,decision:'ALLOW'};};
 const managerFactory=options=>{
  assert.equal(options.admit,admit);
  assert.equal(options.command,command);
  return {
   discover:async()=>({available:true,profiles:Object.keys(profiles).map(name=>({id:'opaque-'+name,name}))}),
   load:async id=>{
    loads.push(id);
    if(managerResult)return managerResult;
    const name=id.slice('opaque-'.length),p=profiles[name];
    const old=await options.readProfile();
    active=name;
    await options.writeProfile({...old,profile:name,model:p.model,port:p.port},old);
    if(mutateLoad)mutateLoad(profiles,{setActive:name=>{active=name;}});
    return {status:'ready',selected:{profile:name,model:p.model,port:p.port}};
   }
  };
 };
 const create=changes=>runtime.create({environment:env,platform:'darwin',readProfile:async()=>structuredClone(saved),
  writeProfile:async(next,expected)=>{assert.deepEqual(saved,expected);saved=structuredClone(next);writes++;},
  command,managerFactory,inspectServer,admit,...changes});
 return {runtime:create(),create,commands,loads,admissions,profiles,env,saved:()=>saved,writes:()=>writes};
}

async function verify(){
 assert.equal(runtime.nativeProfile(ref('a-1')),'a-1');
 for(const bad of ['strata:profile/../x','strata:profile/Alpha','strata:profile/','strata:profile/x\n','qwen3:4b',null]){
  assert.equal(runtime.validRef(bad),false);
 }
 assert.equal(runtime.validRef('strata:high'),true);
 assert.deepEqual([...runtime.allowedRefs({SUTRA_STRATA_MODELS:'["qwen3:4b","strata:profile/alpha"]'})],[ref('alpha')]);
 for(const bad of [undefined,'null','{}','not json','[42]','["bad\\nvalue"]']){
  assert.throws(()=>runtime.allowedRefs({SUTRA_STRATA_MODELS:bad}));
 }
 let f=fixture();
 let catalog=await f.runtime.catalog();
 assert.equal(catalog.ok,true);assert.equal(catalog.profiles.length,3);
 assert.equal(catalog.legacy_high_ref,ref('alpha'));
 assert.equal(catalog.profiles[0].ready,true);
 assert.equal(catalog.profiles[1].ready,false);assert.equal(catalog.profiles[1].active,false);
 assert.equal(catalog.profiles[1].context_tokens,4096);
 assert.equal(catalog.profiles[1].eligible,true);
 assert.equal(catalog.profiles[1].execution_compatibility,'unverified');
 assert.equal(f.loads.length,0);assert.equal(f.writes(),0);
 assert(!JSON.stringify(catalog).includes('/native/'),'Catalog must not expose executable/configuration paths');
 const inspected=await f.runtime.inspect(ref('beta'));
 assert.equal(inspected.ok,true);assert.equal(inspected.profile.model,'shared');assert.equal(inspected.profile.context_tokens,4096);
 assert.equal(f.loads.length,0);
 const invalid=await f.runtime.inspect('strata:profile/../outside');assert.equal(invalid.status,'invalid');
 const before=f.commands.length;
 assert.equal((await f.runtime.activate(ref('gamma'))).status,'denied');
 assert.equal(f.commands.length,before,'Unassigned references must not inspect or activate native models');
 assert.equal((await f.runtime.activate('strata:profile/../outside')).status,'invalid');
 assert.equal(f.commands.length,before);
 const changed=await f.runtime.activate(ref('beta'));
 assert.equal(changed.ok,true);assert.equal(changed.status,'ready');
 assert.equal(changed.profile.name,ref('beta'));assert.equal(changed.profile.context_tokens,4096);
 assert.equal(changed.server.cache_max_tokens,4096);
 assert.deepEqual(f.loads,['opaque-beta']);
 assert.deepEqual(f.saved().legacy_high,{profile:'alpha',model:'shared',port:19483});
 assert.equal(f.saved().profile,'beta');
 const legacy=await f.runtime.inspect('strata:high');
 assert.equal(legacy.profile.profile,'alpha','Legacy High must keep its original profile when Low uses the same endpoint/model');
 assert.equal(legacy.profile.name,'strata:high');
 const restored=await f.runtime.activate('strata:high');
 assert.equal(restored.ok,true);assert.equal(restored.profile.profile,'alpha');
 assert.deepEqual(f.loads,['opaque-beta','opaque-alpha']);
 assert.equal(f.saved().legacy_high.profile,'alpha');
 const again=await f.runtime.inspect(ref('beta'));
 assert.deepEqual([again.profile.profile_revision,again.profile.source_sha256],
  [inspected.profile.profile_revision,inspected.profile.source_sha256],'Profile identity must survive runtime switches');

 for(const outcome of [
  {status:'paused',message:'Memory pressure',decision:'BRIDGE'},
  {status:'timeout',message:'Start did not complete',rollback:{status:'ready'}},
  {status:'busy',message:'Other native requests active'}
 ]){
  f=fixture({managerResult:outcome});
  const result=await f.runtime.activate(ref('beta'));
  assert.equal(result.ok,false);assert.equal(result.status,outcome.status);assert.equal(result.error,outcome.message);
  assert.equal(f.writes(),0);
  if(outcome.decision)assert.equal(result.decision,outcome.decision);
 }
 f=fixture({foreign:true});
 assert.equal((await f.runtime.catalog()).profiles[0].ready,false);
 assert.equal((await f.runtime.activate(ref('beta'))).ok,false);
 f=fixture({mutateLoad:profiles=>{profiles.beta.hash='f'.repeat(64);}});
 assert.equal((await f.runtime.activate(ref('beta'))).status,'changed');
 f=fixture({invalidPlan:true});catalog=await f.runtime.catalog();
 assert.equal(catalog.profiles.length,2);assert.match(catalog.excluded[0].reason,/plan/);
 assert.equal((await f.runtime.activate(ref('beta'))).ok,false);assert.equal(f.loads.length,0);
 f=fixture({invalidState:true});catalog=await f.runtime.catalog();
 assert.equal(catalog.profiles.length,2);assert.match(catalog.excluded[0].reason,/status/);
 f=fixture({brokenProfile:'beta'});catalog=await f.runtime.catalog();assert.equal(catalog.profiles.length,2);
 f=fixture({environment:{SUTRA_STRATA_MODELS:'null'}});
 assert.equal((await f.runtime.activate(ref('alpha'))).ok,false);assert.equal(f.commands.length,0);
 f=fixture({environment:{SUTRA_STRATA_PORT:'0'}});
 assert.equal((await f.runtime.inspect('strata:high')).ok,false);
 f=fixture();const unsupported=f.create({platform:'linux'});
 assert.equal((await unsupported.catalog()).status,'unsupported');assert.equal(f.commands.length,0);
 assert.equal((await unsupported.activate(ref('alpha'))).status,'unsupported');

 let executed;
 const admitted=await runtime.providerAdmission(null,{environment:{SUTRA_PROVIDER_PYTHON:'/trusted Python/bin/python3'},
  execute:(exe,args,options,callback)=>{executed={exe,args,options};callback(null,'{"admitted":true,"decision":"ALLOW"}');}});
 assert.equal(admitted.admitted,true);assert.equal(executed.exe,'/trusted Python/bin/python3');
 assert.deepEqual(executed.args.slice(-1),['--native-fit']);assert.equal(executed.options.shell,undefined);
 assert.equal((await runtime.providerAdmission(100,{execute:(_exe,args,_options,callback)=>{
  assert.deepEqual(args.slice(-2),['--cold-memory-bytes','100']);callback(new Error('Unavailable'),'{}');
 }})).admitted,false);
 assert.equal((await runtime.providerAdmission(null,{environment:{SUTRA_PROVIDER_PYTHON:'bad\npython'},
  execute:()=>assert.fail('Invalid executable dispatched')})).admitted,false);

 let text='';const output=value=>{text+=value;};
 const fake={catalog:async()=>({ok:true,profiles:[]}),inspect:async ref=>({ok:true,profile:{ref}}),activate:async()=>({ok:false,status:'denied',error:'Denied'})};
 assert.equal(await runtime.main(['catalog'],{runtime:fake,output}),0);assert.equal(JSON.parse(text).ok,true);
 text='';assert.equal(await runtime.main(['activate',ref('alpha')],{runtime:fake,output}),1);assert.equal(JSON.parse(text).status,'denied');
 text='';assert.equal(await runtime.main(['catalog','extra'],{runtime:fake,output}),1);assert.equal(JSON.parse(text).status,'invalid');
 text='';assert.equal(await runtime.main(['catalog'],{runtime:{catalog:async()=>({ok:true,profiles:['x'.repeat(524288)]})},output}),1);
 assert.equal(JSON.parse(text).status,'invalid');assert(text.length<200);
 console.log('Strata runtime coordination passed native reference, catalog, allowlist, legacy binding, readiness, admission and bounded CLI checks.');
}
verify().catch(error=>{console.error(error);process.exitCode=1;});

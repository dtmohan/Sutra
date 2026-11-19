'use strict';
const assert=require('node:assert/strict');
const {create,isEngineModel}=require('../engine_management');
const model='sutra:engine/local-qwen';
const base={bootstrapPython:'python3',python:'/private Sutra/python/bin/python',
  registryPath:'/private Sutra/models.json',runtimeDir:'/private Sutra/runtime',
  extensionPath:'/installed Sutra',models:[model],environment:{PATH:'/trusted/bin',
    PYTHONPATH:'/foreign/control',PYTHONHOME:'/foreign/python',VIRTUAL_ENV:'/foreign/env'}};

(async()=>{
 const calls=[];
 const controller=create({...base,execute(executable,argv,options,callback){
  calls.push({executable,argv,options});callback(null,JSON.stringify({ok:true,status:'ready'}));
 }});
 assert(Object.isFrozen(controller));
 await controller.command('catalog');await controller.command('status');
 await controller.command('load',model);await controller.command('unload');
 await controller.command('shutdown');await controller.command('setup',undefined,{approved:true});
 assert.equal(calls.length,6);
 assert.equal(calls[0].executable,'python3');
 assert.deepEqual(calls[2].argv,['-B','/installed Sutra/harness/engine_control.py','load',model]);
 for(const call of calls){
  assert.equal(call.options.shell,false);
  assert.equal(call.options.windowsHide,true);
  assert.equal(call.options.maxBuffer,524288);
  assert.equal(call.options.env.SUTRA_ENGINE_PYTHON,base.python);
  assert.equal(call.options.env.SUTRA_ENGINE_REGISTRY,base.registryPath);
  assert.equal(call.options.env.SUTRA_RUNTIME_DIR,base.runtimeDir);
  assert.deepEqual(JSON.parse(call.options.env.SUTRA_ENGINE_MODELS),[model]);
  assert.equal(call.options.env.SUTRA_ENGINE_ONLY,'1');
  for(const key of ['PYTHONPATH','PYTHONHOME','VIRTUAL_ENV'])assert(!Object.hasOwn(call.options.env,key));
 }
 assert.equal(calls[2].options.timeout,180000);
 assert.equal(calls[5].options.timeout,180000);
 assert.equal(calls[0].options.timeout,15000);
 assert.equal(base.environment.PYTHONPATH,'/foreign/control','Caller environment must not be mutated');
 assert(isEngineModel(model));
 for(const bad of ['strata:high','sutra:engine/../escape','sutra:engine/UPPER','sutra:engine/','sutra:engine/a\n']){
  assert(!isEngineModel(bad));await assert.rejects(controller.command('load',bad));
 }
 await assert.rejects(controller.command('load','sutra:engine/unapproved'),/approved/);
 await assert.rejects(controller.command('status',model),/does not accept/);
 await assert.rejects(controller.command('shell'),/Unknown/);
 await assert.rejects(controller.command('setup'),/confirmation/);
 assert.equal(calls.length,6,'Rejected intents must not launch the controller');
 for(const patch of [{python:'relative/python'},{registryPath:'relative.json'},{runtimeDir:'relative'},
  {extensionPath:'relative'},{bootstrapPython:'python\n3'},{models:['ollama']},{models:{}}]){
  assert.throws(()=>create({...base,...patch}));
 }
 const pause={ok:false,status:'paused',decision:'BRIDGE',error:'Fresh memory admission required'};
 const paused=create({...base,execute(_exe,_args,_opts,callback){callback(Object.assign(new Error('blocked'),{code:2}),JSON.stringify(pause));}});
 assert.deepEqual(await paused.command('load',model),pause);
 let attempts=0;
 const timed=create({...base,execute(_exe,_args,_opts,callback){attempts++;callback(Object.assign(new Error('timeout'),{killed:true}), '');}});
 assert.equal((await timed.command('load',model)).status,'uncertain');
 assert.equal(attempts,1,'Ambiguous activation must not be replayed');
 const contradictory=create({...base,execute(_exe,_args,_opts,callback){callback(new Error('interrupted'),'{"ok":true}');}});
 assert.equal((await contradictory.command('load',model)).status,'uncertain');
 for(const response of ['','not-json','[]','{}','{"ok":"true"}',JSON.stringify({ok:true,text:'x'.repeat(524288)})]){
  const bad=create({...base,execute(_exe,_args,_opts,callback){callback(null,response);}});
  await assert.rejects(bad.command('status'),/bounded response/);
 }
 console.log('Engine management checks passed for fixed argv, trusted models, explicit setup, native environment, recoverable pauses and no replay.');
})().catch(error=>{console.error(error);process.exitCode=1;});

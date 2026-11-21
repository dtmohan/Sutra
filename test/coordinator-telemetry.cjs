'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {EventEmitter}=require('node:events');
const {CoordinatorTelemetry}=require('../coordinator_telemetry');

function fixture(storage,settings={}){
 const children=[],launches=[],errors=[];
 const sampler=new CoordinatorTelemetry({storage,extensionPath:path.resolve(__dirname,'..'),node:process.execPath,
  platform:'darwin',arch:'arm64',now:()=>1000,retryMs:5,onError:error=>errors.push(error),
  spawn(command,args,options){
   launches.push({command,args,options});
   const child=new EventEmitter();child.stdout=new EventEmitter();child.stderr=new EventEmitter();child.signals=[];
   child.kill=signal=>{child.signals.push(signal);};children.push(child);return child;
  },...settings});
 return {sampler,children,launches,errors};
}
const measured=wall_time=>({wall_time,thermal_state:0,memory_pressure_raw:1,free_bytes:8*1024**3,inactive_bytes:0});
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));

(async()=>{
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'sutra-coordinator-telemetry-'));
 try{
  const storage=path.join(root,'native'),h=fixture(storage);
  fs.mkdirSync(storage);fs.writeFileSync(path.join(storage,'telemetry.json'),'UI observations stay separate');
  assert(h.sampler.ensure());assert(h.sampler.ensure());assert.equal(h.children.length,1);
  assert.equal(h.launches[0].command,path.join(path.resolve(__dirname,'..'),'bin','telemetry'));
  assert.deepEqual(h.launches[0].args,[]);assert.equal(h.launches[0].options.env.ELECTRON_RUN_AS_NODE,'1');
  const raw=JSON.stringify(measured(999));h.children[0].stdout.emit('data',raw.slice(0,14));
  assert(!fs.existsSync(path.join(storage,'coordinator','telemetry.json')));
  h.children[0].stdout.emit('data',raw.slice(14)+'\n');
  const file=path.join(storage,'coordinator','telemetry.json'),history=path.join(storage,'coordinator','telemetry-history.json');
  assert.deepEqual(JSON.parse(fs.readFileSync(file)),measured(999));
  assert.equal(fs.readFileSync(path.join(storage,'telemetry.json'),'utf8'),'UI observations stay separate');
  if(process.platform!=='win32'){
   assert.equal(fs.statSync(file).mode&0o777,0o600);assert.equal(fs.statSync(path.dirname(file)).mode&0o777,0o700);
  }
  for(let i=1;i<=70;i++)h.children[0].stdout.emit('data',JSON.stringify(measured(999+i/1000))+'\n');
  const samples=JSON.parse(fs.readFileSync(history));assert.equal(samples.length,60);
  assert.equal(samples.at(-1).wall_time,999.07);assert.deepEqual(JSON.parse(fs.readFileSync(file)),samples.at(-1));
  const before=fs.readFileSync(file,'utf8');
  for(const bad of ['{',JSON.stringify(measured(1001)),JSON.stringify(measured(900)),'x'.repeat(65537)])
   h.children[0].stdout.emit('data',bad+'\n');
  assert.equal(fs.readFileSync(file,'utf8'),before);assert.equal(h.errors.length,4);
  assert(fs.readdirSync(path.dirname(file)).every(name=>!name.endsWith('.tmp')));
  h.sampler.dispose();h.sampler.dispose();assert.deepEqual(h.children[0].signals,['SIGTERM']);
  h.children[0].stdout.emit('data',JSON.stringify(measured(1000))+'\n');assert.equal(fs.readFileSync(file,'utf8'),before);
  assert.equal(h.sampler.ensure(),false);

  const portable=fixture(path.join(root,'portable'),{platform:'linux',arch:'x64'});portable.sampler.ensure();
  assert.equal(portable.launches[0].command,process.execPath);
  assert.deepEqual(portable.launches[0].args,[path.join(path.resolve(__dirname,'..'),'harness','telemetry_portable.js')]);
  portable.children[0].emit('close',1);await pause(15);assert.equal(portable.children.length,2);
  portable.sampler.dispose();await pause(15);assert.equal(portable.children.length,2);

  const unsafe=path.join(root,'unsafe');fs.mkdirSync(unsafe);fs.mkdirSync(path.join(unsafe,'coordinator'),{mode:0o700});
  const outside=path.join(root,'outside.json');fs.writeFileSync(outside,'outside');
  fs.symlinkSync(outside,path.join(unsafe,'coordinator','telemetry.json'));
  const linked=fixture(unsafe);linked.sampler.ensure();linked.children[0].stdout.emit('data',JSON.stringify(measured(999))+'\n');
  assert.equal(fs.readFileSync(outside,'utf8'),'outside');assert(linked.errors.some(error=>error.includes('private and unlinked')));
  linked.sampler.dispose();

  const actualStorage=path.join(root,'actual');const errors=[];
  const actual=new CoordinatorTelemetry({storage:actualStorage,extensionPath:path.resolve(__dirname,'..'),
   node:process.execPath,platform:'portable-test',arch:'test',onError:error=>errors.push(error)});
  try{
   assert(actual.ensure());const sampled=path.join(actualStorage,'coordinator','telemetry.json');
   for(let i=0;i<100&&!fs.existsSync(sampled);i++)await pause(25);
   assert(fs.existsSync(sampled),errors.join('\n'));const value=JSON.parse(fs.readFileSync(sampled));
   assert.equal(value.profile,'portable-memory-v1');assert(value.wall_time<=Date.now()/1000);
   assert.equal(value.thermal_state,null,'A portable sample cannot invent native thermal observations');
  }finally{actual.dispose();}
  console.log(JSON.stringify({passed:true,checks:8}));
 }finally{fs.rmSync(root,{recursive:true,force:true});}
})().catch(error=>{console.error(error);process.exitCode=1;});

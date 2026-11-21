'use strict';
const assert=require('node:assert/strict'),{prefill,validate,forRequest,ModelUnavailable}=require('../model_selection');
const catalog={models:['small','moe','large','custom'].map(name=>({name,eligible:true})).concat([{name:'autocomplete',eligible:false}]),suggested_tiers:{low:'small',mid:'moe',high:'large'}};
assert.deepEqual(prefill({},catalog),catalog.suggested_tiers);
assert.deepEqual(prefill({low:'custom',mid:'',high:'missing'},catalog),{low:'custom',mid:'moe',high:'missing'});
assert.deepEqual(prefill({}, {models:[],suggested_tiers:catalog.suggested_tiers}),{low:'',mid:'',high:''});
assert.deepEqual(validate({low:'custom',mid:'moe',high:'large'},catalog),{low:'custom',mid:'moe',high:'large'});
for(const selected of [{low:'autocomplete',mid:'moe',high:'large'},{low:'missing',mid:'moe',high:'large'},{low:'small',mid:'moe',high:'large',extra:'bad'}])assert.throws(()=>validate(selected,catalog));
assert.deepEqual(prefill(prefill({},catalog),{...catalog,suggested_tiers:{low:'custom',mid:'custom',high:'custom'}}),catalog.suggested_tiers);
const beta={...catalog,models:[...catalog.models,{name:'strata:high',eligible:true,tier:'high'}]};
const optedIn={low:'small',mid:'moe',high:'strata:high'};
assert.deepEqual(validate(prefill(optedIn,beta),beta),optedIn);
assert.deepEqual(prefill({},beta),catalog.suggested_tiers);
assert.equal(prefill({}, {...beta, suggested_tiers:{...catalog.suggested_tiers,high:'strata:high'}}).high,'');
assert.throws(()=>validate({...optedIn,low:'strata:high'},beta));
const offline={...catalog,errors:[{model:'strata:high',error:'Strata High status is unavailable on localhost'}]};
const saved=Object.freeze({...optedIn});
for(const tier of ['low','mid'])assert.deepEqual(forRequest(saved,offline,tier),{models:optedIn,fallbacks:[]});
assert.throws(()=>forRequest(saved,offline,'high'),err=>err instanceof ModelUnavailable&&/load its model.*status is unavailable on localhost/i.test(err.message));
assert.throws(()=>forRequest(saved,{...offline,models:[]},'high'),/Start your configured local Strata server/);
assert.throws(()=>validate(saved,offline),/Strata High is unavailable/);
const highFallback={tier:'high',model:'strata:high',availableTier:'mid',availableModel:'moe'};
assert.deepEqual(forRequest(saved,offline,'auto'),{models:{...optedIn,high:'moe'},fallbacks:[highFallback]});
assert.deepEqual(forRequest(saved,beta,'auto'),{models:optedIn,fallbacks:[]});
assert.deepEqual(saved,optedIn);
assert.throws(()=>forRequest({...saved,low:'missing'},beta,'auto'),/low tier model is unavailable/);
const nativeNames=['small','balanced','deep'].map(name=>'strata:profile/'+name);
const nativeCatalog={models:nativeNames.map((name,index)=>({name,eligible:true,ready:index===0,
 active:index===0,load_on_demand:true,provider:'strata'})),suggested_tiers:{},errors:[{model:'ollama',error:'Ollama is not running'}]};
const allNative={low:nativeNames[0],mid:nativeNames[1],high:nativeNames[2]};
assert.deepEqual(validate(allNative,nativeCatalog),allNative,'Stopped native profiles remain eligible for governed on-demand loading');
for(const tier of ['low','mid','high','auto'])assert.deepEqual(forRequest(allNative,nativeCatalog,tier),{models:allNative,fallbacks:[]});
assert.deepEqual(prefill(allNative,nativeCatalog),allNative,'Native discovery cannot replace saved selections');
assert.deepEqual(prefill({low:'custom',mid:'moe',high:'large'},nativeCatalog),{low:'custom',mid:'moe',high:'large'},'Missing Ollama selections stay saved until the user replaces them');
const sameNative=Object.fromEntries(['low','mid','high'].map(tier=>[tier,nativeNames[0]]));
assert.deepEqual(forRequest(sameNative,nativeCatalog,'auto'),{models:sameNative,fallbacks:[]},'Reusing one native profile does not invent three model identities');
assert.throws(()=>validate({...allNative,mid:'strata:profile/missing'},nativeCatalog),/native Strata profile for mid.*installed configuration/);
const {nativeProfile}=require('../model_selection');
assert.equal(nativeProfile(nativeNames[0]),'small');
for(const invalid of ['strata:high','strata:profile/../escape','strata:profile/Uppercase','strata:profile/a/extra',null])assert(!nativeProfile(invalid));
const {engineProfile}=require('../model_selection');
const owned=Object.fromEntries(['low','mid','high'].map(tier=>[tier,'sutra:engine/'+tier]));
const ownedCatalog={models:Object.values(owned).map(name=>({name,eligible:true,provider:'sutra_engine',load_on_demand:true})),errors:[]};
assert.deepEqual(forRequest(owned,ownedCatalog,'auto'),{models:owned,fallbacks:[]});
assert.throws(()=>validate({...owned,mid:'sutra:engine/missing'},ownedCatalog),/Sutra-owned engine profile for mid.*registry and environment/);
assert.equal(engineProfile(owned.low),'low');
for(const invalid of ['sutra:engine/../escape','sutra:engine/A','sutra:engine/a/extra',null])assert(!engineProfile(invalid));
const {engineApprovals,engineApprovalCandidates}=require('../model_selection');
const identity={name:owned.low,eligible:true,model:'Example model',profile_hash:'a'.repeat(64),
 executable_path:'/Users/example/Sutra/Engine/engine',executable_sha256:'b'.repeat(64)};
assert.deepEqual(engineApprovalCandidates([owned.low,owned.low],{models:[identity]},{}),[identity]);
assert.deepEqual(engineApprovalCandidates([owned.low],{models:[identity]},{[owned.low]:identity.profile_hash}),[]);
assert.deepEqual(engineApprovalCandidates([owned.low],{models:[{...identity,profile_hash:'c'.repeat(64)}]},
 {[owned.low]:identity.profile_hash}),[{...identity,profile_hash:'c'.repeat(64)}],'Changing a bound profile requires renewed approval');
for(const invalid of [[],null,{bad:'a'.repeat(64)},{[owned.low]:'not-a-hash'}])assert.throws(()=>engineApprovals(invalid));
for(const invalid of [{...identity,executable_path:'relative'}, {...identity,executable_sha256:undefined},
 {...identity,profile_hash:undefined}])assert.throws(()=>engineApprovalCandidates([owned.low],{models:[invalid]},{}),/Cannot verify/);
assert.deepEqual(engineApprovalCandidates(['small'],catalog,{}),[],'Ordinary model selection cannot approve an executable');
const {remoteProfile,remoteRegistryPath}=require('../model_selection');
const remote='sutra:remote/rtx3090';
const remoteCatalog={...catalog,models:[...catalog.models,{name:remote,eligible:true,provider:'sutra_remote',ready:true}]};
const mixed=Object.freeze({low:'small',mid:remote,high:'large'});
assert.equal(remoteProfile(remote),'rtx3090');
for(const invalid of ['sutra:remote/../escape','sutra:remote/A','sutra:remote/a/extra','https://remote.example/model','sutra:engine/rtx3090',null])assert(!remoteProfile(invalid));
assert.deepEqual(validate(mixed,remoteCatalog),mixed,'A registered remote provider can be selected for any tier');
assert.deepEqual(forRequest(mixed,remoteCatalog,'auto'),{models:mixed,fallbacks:[]});
assert.deepEqual(prefill(mixed,catalog),mixed,'Remote outages cannot replace saved tier selections');
assert.deepEqual(prefill({}, {...remoteCatalog,suggested_tiers:{low:remote,mid:remote,high:remote}}),
 {low:'',mid:'',high:''},'Remote execution requires an explicit selection, never a discovered default');
const remoteOffline={...catalog,errors:[{model:remote,error:'Remote GPU observation is unavailable'}]};
assert.throws(()=>forRequest(mixed,remoteOffline,'mid'),/remote provider for mid.*SSH connection.*GPU observation/);
assert.deepEqual(forRequest(mixed,remoteOffline,'auto'),{
 models:{low:'small',mid:'small',high:'large'},
 fallbacks:[{tier:'mid',model:remote,availableTier:'low',availableModel:'small'}]
},'Auto can use an eligible lower tier while preserving the remote choice');
assert.deepEqual(mixed,{low:'small',mid:remote,high:'large'});
const registry=require('node:path').resolve('/tmp','Sutra Remote','models.json');
assert.equal(remoteRegistryPath(undefined,registry),registry);
assert.equal(remoteRegistryPath('',registry),registry);
assert.equal(remoteRegistryPath(registry,'/unused'),registry);
for(const invalid of ['relative/models.json','https://remote.example/models.json','/tmp/models\n.json',{},null])assert.throws(()=>remoteRegistryPath(invalid,registry),/absolute file path in User Settings/);
console.log(JSON.stringify({passed:true,checks:['prefill empty tiers from installed metadata','preserve saved and missing choices','never invent unavailable defaults','reject ineligible models on save','allow fixed available tiers during a High outage','block fixed unavailable High with recovery details','fallback and restore Auto High as readiness changes without editing settings']}));

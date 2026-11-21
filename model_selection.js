'use strict';
const tiers=['low','mid','high'];
const nativeProfile=name=>typeof name==='string'&&/^strata:profile\/([a-z][a-z0-9_-]{0,63})$/.exec(name)?.[1];
const engineProfile=name=>typeof name==='string'&&/^sutra:engine\/([a-z][a-z0-9_-]{0,63})$/.exec(name)?.[1];
const remoteProfile=name=>typeof name==='string'&&/^sutra:remote\/([a-z][a-z0-9_-]{0,63})$/.exec(name)?.[1];
function remoteRegistryPath(value,fallback){
 const selected=value===undefined||value===''?fallback:value;
 if(typeof selected!=='string'||selected.length>4096||/[\x00-\x1f\x7f]/.test(selected)||!require('node:path').isAbsolute(selected))throw Error('Sutra remote provider registry must be an absolute file path in User Settings.');
 return selected;
}
function engineApprovals(value={}){
 if(!value||typeof value!=='object'||Array.isArray(value)||Object.keys(value).length>64||
  Object.entries(value).some(([ref,hash])=>!engineProfile(ref)||typeof hash!=='string'||!/^[a-f0-9]{64}$/.test(hash)))throw Error('Sutra engine approvals must be a User setting containing profile references and SHA-256 identities.');
 return {...value};
}
function engineApprovalCandidates(names,catalog,approvals={}){
 const saved=engineApprovals(approvals),result=[];
 for(const ref of new Set(names.filter(engineProfile))){
  const row=catalog.models?.find(model=>model.name===ref&&model.eligible===true);
  if(!row||typeof row.profile_hash!=='string'||!/^[a-f0-9]{64}$/.test(row.profile_hash)||
   typeof row.executable_path!=='string'||!require('node:path').isAbsolute(row.executable_path)||
   typeof row.executable_sha256!=='string'||!/^[a-f0-9]{64}$/.test(row.executable_sha256))throw Error('Cannot verify the engine executable for '+ref+'. Refresh the worker catalog before approving it.');
  if(saved[ref]!==row.profile_hash)result.push(row);
 }
 return result;
}
function prefill(current,catalog){
 const names=new Set((catalog.models||[]).filter(m=>m.eligible===true&&!m.tier&&!remoteProfile(m.name)).map(m=>m.name));
 const result={};
 for(const tier of tiers){
  const saved=typeof current?.[tier]==='string'?current[tier].trim():'';
  result[tier]=saved|| (names.has(catalog.suggested_tiers?.[tier])?catalog.suggested_tiers[tier]:'');
 }
 return result;
}
function selectedTiers(selected){
 if(!selected||typeof selected!=='object'||Object.keys(selected).sort().join(',')!=='high,low,mid')throw Error('Choose all three model tiers.');
 return Object.fromEntries(tiers.map(t=>[t,selected[t]]));
}

function eligible(catalog,tier,name){
 return catalog.models.some(m=>m.eligible===true&&m.name===name&&(!m.tier||m.tier===tier));
}

class ModelUnavailable extends Error{
 constructor(tier,name,catalog){
  const message=`The ${tier} tier model is unavailable or not a local chat model. Refresh the list and choose a replacement.`;
  const detail=(catalog.errors||[]).find(row=>row.model===name)?.error;
  super(name==='strata:high'&&tier==='high'
   ? `Strata High is unavailable. Start your configured local Strata server and load its model, then retry.${detail?' '+detail+'.':''}`
   : engineProfile(name)
    ? `The Sutra-owned engine profile for ${tier} (${engineProfile(name)}) is unavailable. Refresh the engine worker in Sutra model settings and check its registry and environment.${detail?' '+detail+'.':''}`
   : remoteProfile(name)
    ? `The remote provider for ${tier} (${remoteProfile(name)}) is unavailable. Check its configured SSH connection and remote model service, then refresh Sutra model settings.${detail?' '+detail+'.':''}`
   : nativeProfile(name)
    ? `The native Strata profile for ${tier} (${nativeProfile(name)}) is unavailable. Refresh Native Strata runtime in Sutra model settings and check its installed configuration.${detail?' '+detail+'.':''}`
    : message);
 }
}

function validate(selected,catalog){
 const models=selectedTiers(selected);
 for(const tier of tiers)if(!eligible(catalog,tier,models[tier]))throw new ModelUnavailable(tier,models[tier],catalog);
 return models;
}

function forRequest(selected,catalog,tier){
 const models=selectedTiers(selected);
 if(tier!=='auto'){
  if(!tiers.includes(tier))throw Error('Unknown model tier.');
  if(!eligible(catalog,tier,models[tier]))throw new ModelUnavailable(tier,models[tier],catalog);
  return {models,fallbacks:[]};
 }
 // Only the live invocation sees this map. Duplicate names keep the existing
 // routing adapter from escalating twice to the same model.
 const fallbacks=[];
 let availableTier;
 for(const rank of tiers){
  if(eligible(catalog,rank,models[rank]))availableTier=rank;
  else{
   if(!availableTier)throw new ModelUnavailable(rank,models[rank],catalog);
   fallbacks.push({tier:rank,model:models[rank],availableTier,availableModel:models[availableTier]});
   models[rank]=models[availableTier];
  }
 }
 return {models,fallbacks};
}

function fallbackMessage(fallback){
 const label=tier=>tier[0].toUpperCase()+tier.slice(1);
 return `Sutra Auto: ${label(fallback.tier)} (${fallback.model||'unconfigured'}) is unavailable. Work routed to ${label(fallback.tier)} will use ${label(fallback.availableTier)} (${fallback.availableModel}). Your saved model choices are unchanged.`;
}

module.exports={prefill,validate,forRequest,ModelUnavailable,fallbackMessage,nativeProfile,engineProfile,remoteProfile,remoteRegistryPath,engineApprovals,engineApprovalCandidates};

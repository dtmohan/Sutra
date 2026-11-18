'use strict';
const tiers=['low','mid','high'];
function prefill(current,catalog){
 const names=new Set((catalog.models||[]).filter(m=>m.eligible===true&&!m.tier).map(m=>m.name));
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

module.exports={prefill,validate,forRequest,ModelUnavailable,fallbackMessage};

'use strict';
const tiers=['low','mid','high'];
function prefill(current,catalog){
 const names=new Set((catalog.models||[]).filter(m=>m.eligible===true).map(m=>m.name));
 const result={};
 for(const tier of tiers){
  const saved=typeof current?.[tier]==='string'?current[tier].trim():'';
  result[tier]=saved|| (names.has(catalog.suggested_tiers?.[tier])?catalog.suggested_tiers[tier]:'');
 }
 return result;
}
function validate(selected,catalog){
 if(!selected||typeof selected!=='object'||Object.keys(selected).sort().join(',')!=='high,low,mid')throw Error('Choose all three model tiers.');
 const names=new Set(catalog.models.filter(m=>m.eligible===true).map(m=>m.name));
 for(const tier of tiers)if(!names.has(selected[tier]))throw Error(`The ${tier} tier model is unavailable or not a local chat model. Refresh the list and choose a replacement.`);
 return Object.fromEntries(tiers.map(t=>[t,selected[t]]));
}
module.exports={prefill,validate};

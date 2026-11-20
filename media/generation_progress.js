(function(root,factory){
 'use strict';
 const api=factory();
 if(typeof module==='object'&&module.exports)module.exports=api;
 else root.SutraGenerationProgress=api;
})(typeof globalThis==='object'?globalThis:this,function(){
 'use strict';
 const limits=Object.freeze({pending_chars:4096,digest_units:128,recent_units:32,min_unit_chars:40,
  min_long_units:32,min_observed_chars:2048,feed_chars:16384,duplicate_ratio:0.8});
 const counts=['received_chars','observed_chars','omitted_chars','invalid_chars','invalid_feeds',
  'long_units','duplicate_units','novel_units','recent_units','pending_chars'];
 const flags=['repetition_indicator','current_repetition','alert','alert_emitted','analysis_complete','finished'];
 const notice='Repeated reasoning text observed (literal signal; correctness unverified). Generation remains governed by its existing limits. Details are in Sutra Monitor.';
 const object=value=>value!==null&&typeof value==='object'&&!Array.isArray(value);
 const finite=value=>typeof value==='number'&&Number.isFinite(value);
 const integer=value=>Number.isSafeInteger(value)&&value>=0;
 function kernel(value){
  if(!object(value)||value.adapter!=='karma'||value.profile!=='generation-progress-observation-v1'
   ||value.profile_version!==1||value.observation_only!==true||value.semantic_correctness_verified!==false
   ||!finite(value.k)||value.k<=0||!finite(value.sigma_clause)||value.sigma_clause<=0||value.sigma!==0
   ||!finite(value.omega)||value.omega<0)return undefined;
  if(!((value.decision==='ALLOW'&&value.omega===0)||(value.decision==='BRIDGE'&&Math.abs(value.omega-2*value.k)<1e-12)))return undefined;
  return {adapter:'karma',profile:'generation-progress-observation-v1',profile_version:1,decision:value.decision,
   omega:value.omega,sigma:0,k:value.k,sigma_clause:value.sigma_clause,
   observation_only:true,semantic_correctness_verified:false};
 }
 function normalize(value){
  try{
   if(!object(value)||value.profile!=='literal-generation-progress-v1'||value.semantic_assessment!=='unassessed'||!object(value.limits))return null;
   if(Object.keys(value.limits).length!==Object.keys(limits).length||Object.entries(limits).some(([key,n])=>value.limits[key]!==n))return null;
   const result={profile:'literal-generation-progress-v1'};
   for(const key of counts){if(!integer(value[key]))return null;result[key]=value[key];}
   for(const key of flags){if(typeof value[key]!=='boolean')return null;result[key]=value[key];}
   const duplicate=value.recent_duplicate_fraction,novel=value.recent_novelty_fraction;
   if(!finite(duplicate)||duplicate<0||duplicate>1||!finite(novel)||novel<0||novel>1)return null;
   if(!Number.isSafeInteger(value.observed_chars+value.omitted_chars+value.invalid_chars)
    ||value.received_chars!==value.observed_chars+value.omitted_chars+value.invalid_chars
    ||value.long_units!==value.duplicate_units+value.novel_units||value.long_units>0&&value.novel_units<1
    ||value.long_units*limits.min_unit_chars>value.observed_chars
    ||value.recent_units>Math.min(value.long_units,limits.recent_units)
    ||value.pending_chars>Math.min(value.observed_chars,limits.pending_chars)
    ||value.finished&&value.pending_chars!==0)return null;
   if(value.recent_units===0){if(duplicate!==0||novel!==0)return null;}
   else{
    const repeated=Math.round(duplicate*value.recent_units);
    if(Math.abs(duplicate+novel-1)>2e-6||Math.abs(duplicate-repeated/value.recent_units)>1e-6
     ||repeated>value.duplicate_units||value.recent_units-repeated>value.novel_units)return null;
   }
   const current=value.recent_units>=limits.recent_units&&value.long_units>=limits.min_long_units
    &&value.observed_chars>=limits.min_observed_chars&&duplicate>=limits.duplicate_ratio;
   if(value.current_repetition!==current||value.repetition_indicator!==value.alert_emitted||value.alert&&!value.alert_emitted
    ||current&&!value.repetition_indicator||value.repetition_indicator&&(value.long_units<limits.min_long_units
     ||value.observed_chars<limits.min_observed_chars||value.duplicate_units<Math.ceil(limits.recent_units*limits.duplicate_ratio)))return null;
   if(value.analysis_complete!==(value.omitted_chars===0&&value.invalid_chars===0&&value.invalid_feeds===0))return null;
   result.recent_duplicate_fraction=duplicate;result.recent_novelty_fraction=novel;
   result.semantic_assessment='unassessed';result.limits={...limits};
   const frame=kernel(value.kernel);if(frame)result.kernel=frame;
   return result;
  }catch{return null;}
 }
 function describe(raw){
  const value=normalize(raw);if(!value)return '';
  const parts=[];
  if(value.repetition_indicator)parts.push('Repeated reasoning text observed (literal signal; correctness unverified).');
  if(value.recent_units)parts.push('Recent literal matches: '+(100*value.recent_duplicate_fraction).toFixed(1)+'% of '+value.recent_units+' long text units.');
  parts.push(value.observed_chars+' of '+value.received_chars+' received characters analyzed; '+value.omitted_chars+' omitted; '+value.invalid_chars+' invalid.');
  if(!value.analysis_complete)parts.push('Literal character analysis has incomplete coverage.');
  parts.push('This measures text repetition, not understanding or task progress.');
  return parts.join(' ');
 }
 return {normalize,describe,notice,limits};
});

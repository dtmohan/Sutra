(function(root,factory){
 'use strict';
 const api=factory();
 if(typeof module==='object'&&module.exports)module.exports=api;
 else root.SutraCoherence=api;
})(typeof globalThis==='object'?globalThis:this,function(){
 'use strict';
 const MAX_ROWS=12,MAX_ISSUES=16,MAX_INLINE=3;
 const object=value=>value!==null&&typeof value==='object'&&!Array.isArray(value);
 const text=(value,limit=200)=>typeof value==='string'?value.slice(0,limit*2).replace(/[\u0000-\u001f\u007f]/g,' ').replace(/\s+/g,' ').trim().slice(0,limit):'';
 const code=value=>typeof value==='string'&&/^[a-z][a-z0-9_]{0,79}$/.test(value)?value:'';
 const count=value=>Number.isSafeInteger(value)&&value>=0?value:undefined;
 const coverageKeys=['sources','items','edges','required_questions','host_closed_questions','answered_claimed_unverified',
  'unsupported_claims','model_reported_unresolved_criteria','contexts','source_linked_claims','analyzed_items',
  'required_questions_open','omitted_items','omitted_sources'];
 const labels={
  missing_reference:'Evidence reference is unavailable',
  missing_source:'Source evidence is unavailable',
  source_binding_mismatch:'Evidence belongs to a different task or chat',
  unknown_context:'A context reference is unavailable',
  source_digest_mismatch:'Source text differs from its recorded digest',
  invalid_source_span:'A quotation does not match its source span',
  invalid_reference:'Evidence reference is invalid',
  cross_context_reference:'A reference crosses its recorded context',
  cross_context_support:'Support crosses its recorded context',
  stale_source:'Source evidence has a newer version',
  stale_evidence:'Source evidence has a newer version',
  circular_support:'A support chain is circular',
  unsupported_claim:'A claim lacks independent source support',
  possible_contradiction:'Possible conflicting statements',
  conflicting_values:'Possible conflicting values',
  value_conflict_indicator:'Possible conflicting values in the same scope',
  contradiction_indicator:'Possible conflicting statements',
  commitment_unmet:'A requested deliverable remains unresolved',
  unresolved_commitment:'A requested deliverable remains unresolved',
  unresolved_claim:'A claim needs further evidence',
  hypothetical_premise:'A hypothetical premise is being used as factual support',
  unknown_question:'A requested question is unavailable',
  inactive_obligation:'A requested question belongs to a different active context',
  invalid_answer_reference:'An answer reference does not match a question',
  invalid_supersession:'A claim update does not match the recorded scope',
  unverified_supersession:'A proposed claim update is unverified',
  quotation_absent:'A quotation is absent from the supplied source',
  source_state:'Delivered evidence needs a source-state check',
  unknown_claim:'A verdict names an unavailable claim',
  duplicate_stage_item:'A structured item identifier is duplicated',
  input_limit:'The diagnostic allowance did not cover every item',
  completion_conflict:'A completion claim conflicts with recorded remaining work',
  scope_mismatch:'An item does not match its recorded scope',
  binding_mismatch:'An item does not match its recorded task',
  truncated_analysis:'The diagnostic allowance did not cover every item'
 };
 function issueLabel(issue){
  const label=labels[issue.code]||issue.code.replaceAll('_',' ');
  return label+(issue.kind==='semantic_indicator'?' (unverified)':'');
 }
 function normalize(value){
  try{
   if(!object(value)||value.format!==1||value.profile!=='stage-text-coherence-v1'||value.mode!=='observe_only')return null;
   const status=['assessed','partial','unavailable','malformed'].includes(value.status)?value.status:'unavailable';
   const binding=object(value.binding)?value.binding:{};
   const issues=[];
   for(const raw of Array.isArray(value.issues)?value.issues.slice(0,MAX_ISSUES):[]){
    if(!object(raw))continue;
    const name=code(raw.code),kind=raw.kind==='semantic_indicator'||raw.kind==='indicator'?'semantic_indicator':raw.kind==='structural'?'structural':null;
    if(!name||!kind)continue;
    issues.push({code:name,kind,detail:text(raw.detail??raw.message,400),
     items:(Array.isArray(raw.items)?raw.items:[]).slice(0,8).map(value=>text(value,96)).filter(Boolean)});
   }
   const coverage={};
   if(object(value.coverage))for(const key of coverageKeys){
    const n=count(value.coverage[key]);if(n!==undefined)coverage[key]=n;
   }
   const kernel=object(value.kernel)&&['ALLOW','BRIDGE','FIN','COMPOUND_FIN'].includes(value.kernel.decision)
    ? {decision:value.kernel.decision,observation_only:true}:undefined;
   return {format:1,profile:'stage-text-coherence-v1',mode:'observe_only',
    status:status==='malformed'?'unavailable':status,invocation_id:text(value.invocation_id,128),
    frame_hash:typeof value.frame_hash==='string'&&/^[a-f0-9]{64}$/.test(value.frame_hash)?value.frame_hash:'',
    role:text(value.role,64),model:text(value.model,128),
    binding:{session:text(binding.session,128),task:text(binding.task,128),chat:text(binding.chat,128)},
    issues,coverage,kernel,omitted_issues:count(value.omitted_issues)||0,
    analysis_limits:{structured_only:true,semantic_correctness_verified:false}};
  }catch{return null;}
 }
 function recent(rows){
  const result=[];
  for(const row of Array.isArray(rows)?rows.slice(0,MAX_ROWS):[]){
   if(!object(row))continue;
   const observation=normalize(row.observation);if(!observation)continue;
   result.push({observation,gateway_status:['accepted','quarantined'].includes(row.gateway_status)?row.gateway_status:'unknown'});
  }
  return result;
 }
 function collect(previous,value){
  const observation=normalize(value),rows=(Array.isArray(previous)?previous.slice(-MAX_ROWS):[]).map(normalize).filter(Boolean);
  if(!observation)return rows;
  const identity=observation.invocation_id||observation.frame_hash;
  const remaining=identity?rows.filter(row=>(row.invocation_id||row.frame_hash)!==identity):rows;
  return [...remaining,observation].slice(-MAX_ROWS);
 }
 function inline(rows){
  const issues=[],seen=new Set();
  for(const value of Array.isArray(rows)?rows.slice(-MAX_ROWS):[]){
   const observation=normalize(value);if(!observation||observation.status==='unavailable')continue;
   for(const issue of observation.issues){
    const identity=issue.code+':'+issue.kind;
    if(seen.has(identity))continue;seen.add(identity);issues.push(issue);
   }
  }
  if(!issues.length)return '';
  // Only bounded host labels reach Markdown. Diagnostic source text stays in
  // the Monitor as text nodes and cannot become a link, command or action.
  const lines=['**Coherence indicators · observe only**','',...issues.slice(0,MAX_INLINE).map(issue=>'- '+issueLabel(issue))];
  if(issues.length>MAX_INLINE)lines.push('- '+(issues.length-MAX_INLINE)+' more indicator types are recorded in Sutra Monitor.');
  lines.push('','These flags do not verify correctness. Scoped details are in Sutra Monitor.','');
  return lines.join('\n')+'\n';
 }
 return {normalize,recent,collect,inline,issueLabel,limits:Object.freeze({rows:MAX_ROWS,issues:MAX_ISSUES,inline:MAX_INLINE})};
});

(function(root,factory){
 'use strict';
 const api=factory();
 if(typeof module==='object'&&module.exports)module.exports=api;
 else root.SutraReviewProgress=api;
})(typeof globalThis==='object'?globalThis:this,function(){
 'use strict';
 const MAX_ROWS=12;
 const object=value=>value!==null&&typeof value==='object'&&!Array.isArray(value);
 const count=value=>Number.isSafeInteger(value)&&value>=0;
 const optionalCount=value=>count(value)?value:null;
 const identifier=(value,maxLength)=>typeof value==='string'&&value.length<=maxLength
  &&!/[\u0000-\u001f\u007f]/.test(value)&&value.trim()?value:'';
 function normalize(row){
  if(!object(row)||row.profile!=='child-checkpoint-progress-v1'||row.mode!=='observe_only'
   ||row.semantic_assessment!=='unassessed'||!count(row.prior_checkpoints))return null;
  const invocationId=identifier(row.invocation_id,128),model=identifier(row.model,160);
  if(!invocationId||!model||!['accepted','quarantined'].includes(row.gateway_status))return null;
  const accepted=row.gateway_status==='accepted';
  if(accepted?!['completed','continued'].includes(row.checkpoint):row.checkpoint!=='withheld')return null;
  let evidence=null,nextChecks=null;
  if(accepted){
   const e=row.evidence,n=row.next_checks;
   if(!object(e)||!object(n)||![e.new_citations,e.retained_citations,e.citations,n.new,n.repeated,n.retired].every(count)
    ||e.new_citations!==e.citations-e.retained_citations||!count(n.new+n.repeated))return null;
   evidence={new_citations:e.new_citations,retained_citations:e.retained_citations,citations:e.citations};
   nextChecks={new:n.new,repeated:n.repeated,retired:n.retired};
  }
  const cost=object(row.cost)?row.cost:{};
  return {profile:'child-checkpoint-progress-v1',mode:'observe_only',invocation_id:invocationId,model,
   gateway_status:row.gateway_status,prior_checkpoints:row.prior_checkpoints,checkpoint:row.checkpoint,
   evidence,next_checks:nextChecks,semantic_assessment:'unassessed',
   cost:{input_tokens:optionalCount(cost.input_tokens),output_tokens:optionalCount(cost.output_tokens),
    elapsed_seconds:Number.isFinite(cost.elapsed_seconds)&&cost.elapsed_seconds>=0?cost.elapsed_seconds:null}};
 }
 function recent(rows){
  const result=[],seen=new Set();
  for(const value of Array.isArray(rows)?rows.slice(0,MAX_ROWS):[]){
   const row=normalize(value);
   if(!row||seen.has(row.invocation_id))continue;
   seen.add(row.invocation_id);
   result.push(row);
  }
  return result;
 }
 return {recent};
});

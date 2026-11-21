'use strict';

// Transport planning only. The backend owns task disposition and every source,
// model and action admission still passes through its existing gateway.
const crypto = require('node:crypto');
const ID = /^[a-f0-9]{32}$/, HASH = /^[a-f0-9]{64}$/;
const START_EVENTS = new Set(['start','start_existing','start_review','step','resume',
  'grant','capability_grant','approve_document','supply_evidence','restart_review','continue_parent_report','accept_provenance']);
const DISPOSITIONS = new Set(['continue','wait','approval','evidence','finished','stopped']);
const BASE_FIELDS = ['models','tier','armed','chat_key','expected_authority',
  'reviewer_model','require_distinct_reviewer'];
function canonical(value) {
  if (Array.isArray(value)) return '['+value.map(canonical).join(',')+']';
  if (value && typeof value==='object') return '{'+Object.keys(value).sort()
    .filter(key=>value[key]!==undefined).map(key=>JSON.stringify(key)+':'+canonical(value[key])).join(',')+'}';
  return JSON.stringify(value);
}
function runner(value, request) {
  if (value === undefined) return undefined;
  if (!value || typeof value!=='object' || Array.isArray(value) ||
      Object.keys(value).sort().join(',')!=='mode,version' || value.version!==1 ||
      value.mode!=='approved_limits' || request?.op!=='orchestrate' || !START_EVENTS.has(request.event)) {
    throw new Error('Invalid approved task-runner request');
  }
  return {version:1,mode:'approved_limits'};
}
function plan(data, previous, requestId) {
  const value=data?.continuation;
  if (!value || value.version!==1 || !DISPOSITIONS.has(value.disposition) ||
      !ID.test(value.flow_id||'') || !HASH.test(value.cursor||'') || !HASH.test(value.fingerprint||'') ||
      value.flow_id!==data.flow_id || value.cursor!==data.cursor ||
      canonical(value.binding??null)!==canonical(data.binding??data.bound_task??null) ||
      !['step','resume','continuation_probe',null].includes(value.next_event) ||
      typeof value.reason!=='string' || value.reason.length>2000) {
    throw new Error('The backend returned an invalid task continuation checkpoint');
  }
  const kernel=value.kernel, receipt=value.receipt;
  const expected=value.disposition==='continue'?'ALLOW':['wait','approval','evidence'].includes(value.disposition)?'BRIDGE':'FIN';
  if (!kernel || kernel.adapter!=='karma' || kernel.profile!=='task-continuation-v1' ||
      kernel.decision!==expected || kernel.grants_permissions!==false || kernel.replenishes_budget!==false ||
      !receipt || receipt.version!==1 || !Number.isSafeInteger(receipt.seq) || receipt.seq<1 || !HASH.test(receipt.hash||'') ||
      !ID.test(value.runner_request_id||'') || requestId && value.runner_request_id!==requestId) {
    throw new Error('The task continuation has no matching kernel decision and chained receipt');
  }
  if (previous && (previous.flow_id!==value.flow_id ||
      previous.binding && canonical(previous.binding)!==canonical(value.binding))) {
    throw new Error('The task identity changed during automatic continuation');
  }
  if (value.disposition==='continue' && !['step','resume'].includes(value.next_event) ||
      value.disposition==='wait' && value.next_event!=='continuation_probe' ||
      !['continue','wait'].includes(value.disposition) && value.next_event!==null) {
    throw new Error('The continuation disposition and next operation disagree');
  }
  return JSON.parse(JSON.stringify(value));
}
function nextRequest(initial, continuation, event=continuation.next_event) {
  if (!['step','resume','continuation_probe','runner_summary'].includes(event)) {
    throw new Error('The task runner cannot issue that operation');
  }
  const request={op:'orchestrate',event,flow_id:continuation.flow_id,
    cursor:continuation.cursor,binding:continuation.binding,continuation_receipt:continuation.receipt};
  for(const key of BASE_FIELDS)if(initial[key]!==undefined)request[key]=initial[key];
  if(event==='resume')request.continuation_fingerprint=continuation.fingerprint;
  return request;
}
function checkpoint(data, continuation) {
  return {flow_id:continuation.flow_id,cursor:continuation.cursor,binding:continuation.binding,
    state:String(data.state||''),disposition:continuation.disposition,reason:continuation.reason,
    kernel:continuation.kernel,receipt:continuation.receipt,runner_request_id:continuation.runner_request_id};
}
function activity(data, continuation) {
  const observedStop=data.continuation_probe?.observation?.blocker==='unknown_outcome';
  const progress=data.progress || (continuation.disposition==='wait'
    ? 'Waiting for resources. Sutra will continue after a fresh readiness check. Stop the task before changing models.'
    : observedStop ? continuation.reason : '');
  return {kind:'workflow',version:1,flow_id:continuation.flow_id,cursor:continuation.cursor,
    state:String(data.state||''),disposition:continuation.disposition,
    progress:String(progress).slice(0,500)};
}
function digest(plan) {return crypto.createHash('sha256').update(canonical(plan)).digest('hex');}
module.exports={runner,plan,nextRequest,checkpoint,activity,digest};

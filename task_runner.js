'use strict';

// Transport planning only. The backend owns task disposition and every source,
// model and action admission still passes through its existing gateway.
const crypto = require('node:crypto');
const ID = /^[a-f0-9]{32}$/, HASH = /^[a-f0-9]{64}$/;
const START_EVENTS = new Set(['start','start_existing','start_review','step','resume',
  'grant','capability_grant','approve_document','supply_evidence','restart_review','continue_parent_report','accept_provenance']);
const DISPOSITIONS = new Set(['continue','wait','approval','evidence','finished','stopped']);
const MEMORY_REFUSALS = new Set(['BRIDGE','FIN','COMPOUND_FIN']);
const SECTION_ID = /^(?:[a-f0-9]{32}|[a-f0-9]{64})$/;
const MAX_REPORT_SECTIONS = 32;
const GIB = 1024 ** 3;
const RESUMING = 'Readiness check passed. Sutra is rechecking admission and resuming the saved task.';
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
function reportSection(section) {
  if (!section || typeof section.receipt_id!=='string' || !SECTION_ID.test(section.receipt_id) ||
      typeof section.markdown!=='string' || !section.markdown.trim() || Buffer.byteLength(section.markdown)>32768) return;
  return {receipt_id:section.receipt_id,markdown:section.markdown};
}
function reportSections(data) {
  const sections=data?.report_sections;
  if (!Array.isArray(sections) || sections.length>MAX_REPORT_SECTIONS) return [];
  const validated=sections.map(reportSection);
  if (validated.some(section=>!section) || new Set(validated.map(section=>section.receipt_id)).size!==validated.length) return [];
  return validated;
}
function amount(bytes) {
  if (bytes > 0 && bytes < GIB / 10) return `${Math.max(1, Math.round(bytes / 1048576))} MiB`;
  return `${(bytes / GIB).toFixed(2)} GiB`;
}
function allocationMessage(label, available, reserve, increment) {
  if (![available,reserve,increment].every(value=>Number.isSafeInteger(value)&&value>=0)) return `${label} needs more headroom.`;
  const required=reserve+increment, deficit=required-available;
  const cause=deficit>0?`${label} is short by ${amount(deficit)}.`:`${label} needs more headroom.`;
  return `${cause} ${amount(available)} available, with ${amount(required)} required including reserve.`;
}
function waitObservation(data, continuation) {
  const stop=data.stop||{}, observation=data.continuation_probe?.observation;
  const freshBlock=observation?.blocking_measurement;
  const measurement=freshBlock||stop.measurement||{};
  const remote=measurement.profile==='remote-nvidia-v1'&&measurement.provider==='remote_llama';
  const runtime=measurement.runtime||{}, plan=observation?.provider?.plan||runtime.plan||{};
  const freshBlocker=runtime.kind==='busy'?'runtime_busy':runtime.kind==='model_capacity'?'model_admission':
    ['hardware-v1','portable-memory-v1'].includes(measurement.profile)&&measurement.admitted===false
      ? (measurement.available?'hardware_pressure':'measurement_unavailable'):null;
  const blocker=measurement.blocker||(freshBlock&&freshBlocker)||stop.blocker||continuation.wait_kind;
  const stage={CLASSIFYING:'Routing',MAPPING:'Source mapping',CHILDREN:'Child review',SYNTHESIS:'Report synthesis',
    CODING:'Workspace proposal',WORKSPACE:'Workspace setup',NEED_TOOL_APPROVAL:'Tool preparation'}[stop.resume_phase];
  const evidencePrefix=stage?`${stage} is waiting. `:'';
  const result=(condition, message)=>({condition,message:evidencePrefix+message});
  if (blocker==='model_admission'||blocker==='model_capacity') {
    if (remote) {
      const gpu=measurement.gpu_allocation, ram=measurement.host_allocation;
      const blocked=[];
      if (MEMORY_REFUSALS.has(gpu?.decision)) blocked.push(['gpu',allocationMessage('Remote GPU memory',gpu.available_bytes,
        gpu.system_reserve_bytes,plan.estimated_increment_bytes)]);
      if (MEMORY_REFUSALS.has(ram?.decision)) blocked.push(['ram',allocationMessage('Remote host RAM',ram.available_bytes,
        ram.system_reserve_bytes,plan.estimated_host_increment_bytes)]);
      return blocked.length?result('remote-capacity:'+blocked.map(([kind])=>kind).join('+'),blocked.map(([,text])=>text).join(' '))
        :result('remote-capacity','The selected model needs more remote GPU or host memory headroom.');
    }
    return result('local-capacity',allocationMessage('Memory for the selected model',plan.available_bytes,
      plan.system_reserve_bytes,plan.estimated_increment_bytes));
  }
  if (blocker==='runtime_busy') return result(remote?'remote-busy':'local-busy',
    `The ${remote?'remote':'local'} inference slot is busy. Sutra is waiting to start the next model call.`);
  if (blocker==='measurement_unavailable') return result(remote?'remote-readings':'local-readings',
    `Fresh ${remote?'remote GPU':'local hardware'} readings are unavailable.`);
  if (blocker==='hardware_pressure'||blocker==='hardware'&&measurement.available===true) {
    const thermal=['serious','critical'].includes(measurement.thermal_label)?measurement.thermal_label:null;
    const memory=({2:'warning',4:'critical'})[measurement.sample?.memory_pressure_raw];
    const details=[thermal&&`System thermal pressure is ${thermal}.`,memory&&`System memory pressure is ${memory}.`].filter(Boolean);
    return result(`pressure:${thermal||''}:${memory||''}`,details.join(' ')||'The system has insufficient hardware headroom.');
  }
  return result('checking','Sutra is waiting for resources before starting the next model call.');
}
function activity(data, continuation, {wait, delay}={}) {
  const observedStop=data.continuation_probe?.observation?.blocker==='unknown_outcome';
  let progress=data.progress || '';
  if (continuation.disposition==='wait') {
    const reason=wait||waitObservation(data,continuation);
    const next=Number.isSafeInteger(delay)?` The next readiness check is scheduled in about ${delay/1000} s.`
      :' Sutra will continue after a fresh readiness check.';
    progress=`${reason.message}${next} Use Stop to change models.`;
  } else if (continuation.next_event==='resume') progress=RESUMING;
  else if (observedStop) progress=continuation.reason;
  return {kind:'workflow',version:1,flow_id:continuation.flow_id,cursor:continuation.cursor,
    state:String(data.state||''),disposition:continuation.disposition,
    progress:String(progress).slice(0,500)};
}
function digest(plan) {return crypto.createHash('sha256').update(canonical(plan)).digest('hex');}
module.exports={runner,plan,nextRequest,checkpoint,waitObservation,activity,reportSection,reportSections,digest};

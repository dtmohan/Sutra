const vscode=acquireVsCodeApi(), el=id=>document.getElementById(id);
let preservedConstraints={},activeView='dashboard';
let current=null,selectedModel='local-assistant:latest',proposalId=null,proposalKind=null,busy=false,nativeState={},lastReceipt=null,cpuHistory=[],autoRunning=false;
function send(op,data={}){el('error').textContent='';vscode.postMessage({op,...data});}
function updateButtons(){document.querySelectorAll('button,select').forEach(e=>e.disabled=e.id.startsWith('nav-')||e.id.startsWith('usage-')?false:busy);el('diff').disabled=busy||proposalKind!=='edit';el('execute').disabled=busy||!['edit','run_tests'].includes(proposalKind);
  for(const id of ['auto-step','auto-run','auto-open'])el(id).disabled=busy||!current?.auto;el('auto-pause').disabled=!autoRunning;
  for(const id of ['static-build','static-explain'])el(id).disabled=busy||!current?.architecture;el('static-next').disabled=busy||current?.static?.next_offset==null;el('static-open').disabled=busy||!current?.static?.report;
  for(const id of ['review-step','review-auto','review-draft'])el(id).disabled=busy||!current?.architecture;el('review-report').disabled=busy||!current?.report;el('api-build').disabled=busy||!current?.architecture;el('api-verify').disabled=busy||!current?.api?.candidate_id||current.api.status==='withheld';el('api-open').disabled=busy||!current?.api?.report;el('api-next').disabled=busy||current?.api?.position?.next_offset==null;el('use-model-defaults').disabled=busy||!suggestedModels;
  const nativeBusy=busy||nativeState.arming||nativeState.selecting||nativeState.working;
  for(const id of ['native-use','overview-native-use','native-arm','overview-native-arm','native-toggle'])el(id).disabled=!!nativeBusy;
  el('native-disarm').disabled=!!nativeState.arming||!nativeState.armed;
}
function save(op){try{const extra={...preservedConstraints};
  for(const [id,key] of [['constraint-timeout','timeout_ms'],['constraint-attempts','max_attempts'],['constraint-rows','max_rows']]){
    const raw=el(id).value.trim();if(raw){const value=Number(raw);if(!Number.isSafeInteger(value)||value<=0)throw new Error('Task limits must be positive whole numbers.');extra[key]=value;}
  }
  if(el('constraint-format').value.trim())extra.output_format=el('constraint-format').value.trim();
  const constraints={...extra,
  allowed_paths:el('paths').value.split('\n').map(s=>s.trim()).filter(Boolean),
  network_allowed:el('network').checked,dependencies_allowed:el('dependencies').checked,
  edits_allowed:el('edits').checked,tests_allowed:el('tests').checked,
  test_timeout_ms:Number(el('testtimeout').value),max_actions:Number(el('maxactions').value)};
  const tests=el('testpaths').value.split('\n').map(s=>s.trim()).filter(Boolean);if(tests.length)constraints.test_paths=tests;
  send(op,{goal:el('goal').value,constraints});}catch(e){el('error').textContent=e.message;}}
el('save').onclick=()=>save('save');el('new').onclick=()=>save('new');el('refresh').onclick=()=>send('state');
el('model').onchange=()=>send('switch',{model:el('model').value});
el('auto-model').onchange=()=>send('switch',{model:el('auto-model').value});
el('auto-start').onclick=()=>send('auto_new',{goal:el('auto-goal').value,roots:el('auto-roots').value.split(',').map(s=>s.trim()).filter(Boolean),max_tools:Number(el('auto-ops').value),review_calls:Number(el('auto-calls').value),review_tokens:Number(el('auto-tokens').value),review_source_kib:Number(el('auto-source').value),review_seconds:Number(el('auto-seconds').value),review_page_kib:Number(el('auto-page').value)});
el('auto-step').onclick=()=>send('auto_step',{explain:el('auto-explain').checked});el('auto-run').onclick=()=>send('auto_run',{explain:el('auto-explain').checked});el('auto-pause').onclick=()=>send('auto_pause');el('auto-open').onclick=()=>send('open_auto_report');
el('review-model').onchange=()=>send('switch',{model:el('review-model').value});
el('review-start').onclick=()=>send('review_new',{goal:el('review-goal').value,roots:el('review-roots').value.split(',').map(x=>x.trim()).filter(Boolean),max_tools:Number(el('review-budget').value)});
el('review-step').onclick=()=>send('architecture_run',{prompt:'Continue the architecture review using current evidence and constraints.',steps:1});
el('review-auto').onclick=()=>send('architecture_run',{prompt:'Continue the architecture review using current evidence and constraints.',steps:4});
el('review-draft').onclick=()=>send('architecture_run',{prompt:'Report the architecture from the inspected evidence with cited findings and a diagram. State any remaining gaps.',steps:1,mode:'report'});
el('review-report').onclick=()=>send('open_report');
el('api-build').onclick=()=>send('api_build',{files:el('api-files').value.split('\n').map(x=>x.trim()).filter(Boolean),offset:Number(el('api-offset').value)});
el('api-verify').onclick=()=>send('api_verify',{reviewer_model:el('api-model').value});
el('api-open').onclick=()=>send('open_api_report');
el('static-model').onchange=()=>send('switch',{model:el('static-model').value});
function staticRequest(op){send(op,{files:el('static-files').value.split('\n').map(x=>x.trim()).filter(Boolean),offset:Number(el('static-offset').value),model:el('static-model').value});}
el('static-build').onclick=()=>staticRequest('static_build');el('static-explain').onclick=()=>staticRequest('static_explain');
el('static-next').onclick=()=>{el('static-offset').value=current.static.next_offset;staticRequest('static_explain');};el('static-open').onclick=()=>send('open_static_report');
el('api-next').onclick=()=>{el('api-offset').value=current.api.position.next_offset;el('api-build').click();};
el('review').onclick=()=>send('work',{mode:el('mode').value,model:el('model').value,prompt:el('prompt').value,
  pages:[...document.querySelectorAll('#pages input:checked')].map(e=>e.value)});
el('diff').onclick=()=>send('diff',{invocation_id:proposalId});el('execute').onclick=()=>send('execute',{invocation_id:proposalId});
function note(p){if(!p){el('answer').textContent='No current model note. Host action receipts below preserve executed work.';return;}
  const a=p.tool||p.action||p.next_action;const lines=[p.summary,'',...(Array.isArray(p.findings)?p.findings:Object.values(p.findings||{})).map(f=>`${f.page_id}\n“${f.quote}”\n${f.note}\n`),
    'Analysis claimed completed:',...p.completed.map(x=>'• '+x),'','Remaining:',...p.remaining.map(x=>'• '+x),
    '',`Proposed next step: ${a.kind} ${a.target}`];el('answer').textContent=lines.join('\n');}
let lastTemperatureUpdate=0;
function textNode(tag,text,className){const e=document.createElement(tag);e.textContent=text;if(className)e.className=className;return e;}
function summaryRows(id,rows){const dl=el(id);dl.replaceChildren();for(const [name,value] of rows){const row=document.createElement('div');row.append(textNode('dt',name),textNode('dd',String(value)));dl.append(row);}}
function budgetBar(parent,label,used,limit,format=v=>Math.round(v).toLocaleString()){
 if(!Number.isFinite(used)||!Number.isFinite(limit)||limit<=0)return;
 const row=document.createElement('div');row.className='budget-row';const labels=document.createElement('div');labels.className='budget-label';labels.append(textNode('span',label),textNode('span',format(used)+' / '+format(limit)));
 const bar=document.createElement('progress');bar.max=limit;bar.value=Math.min(limit,Math.max(0,used));bar.setAttribute('aria-label',label);row.append(labels,bar);parent.append(row);
}
function decisionLabel(d){return {ALLOW:'Ready',BRIDGE:'Adjustment needed',FIN:'Paused',COMPOUND_FIN:'Multiple limits reached'}[d]||'Unavailable';}
function showDashboard(d){
 showUsage(d.token_usage);
 lastTemperatureUpdate=Date.now();const h=d.hardware||{},s=h.sample||{},b=d.review_budget,c=d.context;showTemperatures(h.temperatures);
 const constrained=b&&b.decision!=='ALLOW'||d.storage&&d.storage.decision!=='ALLOW';
 el('overview-status').textContent=!h.available?'Waiting for system readings':!h.admitted?'Waiting for resources':constrained?'An allocation needs attention':busy||d.adapter_health==='busy'?'Working locally':'Ready for the next request';
 el('debug-state').textContent=!h.available?'Live measurements are unavailable. Sutra will wait for fresh data.':!h.admitted?(h.reason||'Resource checks have paused new work.'):constrained?'Review the allocation below before continuing. Your task is retained.':'Resource checks allow work. Task constraints and budgets still apply.';
 el('sample-age').textContent=h.available?'Updated '+h.age_seconds+'s ago':'No recent sample';
 el('resource').textContent=h.available?'Resources: '+decisionLabel(h.decision):'Resources unavailable';el('resource').className='pill '+(h.admitted?'signal-ok':'signal-stop');
 el('thermal').textContent='OS thermal pressure: '+({nominal:'Normal',fair:'Elevated',serious:'High',critical:'Critical'}[h.thermal_label]||h.thermal_label||'unavailable');
 el('cpu').textContent=h.available&&Number.isFinite(s.cpu_busy_pct)?s.cpu_busy_pct.toFixed(0)+'%':'—';
 if(h.available&&Number.isFinite(s.cpu_busy_pct)){cpuHistory.push(s.cpu_busy_pct);cpuHistory=cpuHistory.slice(-60);el('cpu-line').setAttribute('points',cpuHistory.map((v,i)=>`${i*120/59},${35-v*.35}`).join(' '));}else{cpuHistory=[];el('cpu-line').setAttribute('points','');}
 el('memory').textContent=h.available&&Number.isFinite(h.reclaimable_estimate_gib)?h.reclaimable_estimate_gib.toFixed(1)+' GiB':'—';
 el('pressure').textContent=h.available?'Reclaimable estimate · pressure '+({1:'normal',2:'elevated',4:'critical'}[s.memory_pressure_raw]||'unknown'):'Waiting for memory data';
 const bars=el('budget-bars');bars.replaceChildren();
 if(b){for(const [key,label,format] of [['operations','Repository operations'],['calls','Model calls'],['tokens','Conservative token charge'],['source_bytes','Source processed',v=>(v/1024**2).toFixed(1)+' MiB'],['seconds','Active time',v=>Math.round(v)+'s']])budgetBar(bars,label,b.used[key],b.limits[key],format);}
 else if(d.repository)budgetBar(bars,'Repository operations',d.repository.used,d.repository.limit);
 if(d.action_budget)budgetBar(bars,'Edits & test runs',d.action_budget.used,d.action_budget.limit);
 if(!bars.children.length)bars.append(textNode('p','No task allocation yet.','muted'));
 el('budget').textContent=b?'Task allocation: '+decisionLabel(b.decision)+'. Includes failed attempts and repairs.':'Allocations are set by the active task.';
 summaryRows('adapter-summary',[['Sutra · resources',h.decision||'Unavailable'],['Sutra · task budget',b?.decision||d.action_budget?.decision||d.repository?.decision||'Not allocated'],['Smriti · context',c?'Latest packet measured':'No model packet yet']]);
 const cb=el('context-bars');cb.replaceChildren();if(c)budgetBar(cb,'Context allocation',c.conservative_input_charge+(c.output_reserve||0),c.limit);
 if(d.storage)budgetBar(cb,'Local history',d.storage.used_bytes,d.storage.limit_bytes,v=>(v/1024**2).toFixed(1)+' MiB');
 el('context-status').textContent=c?'Context includes reserved output space. Actual last prompt: '+(c.actual_prompt_tokens??'unavailable')+' tokens.':'A context reading appears after the first model call.';
 const receipts=el('receipt-summary');receipts.replaceChildren();for(const [key,label] of [['accepted','Accepted'],['quarantined','Withheld'],['pending','Pending']]){const item=document.createElement('div');item.append(textNode('strong',String(d.invocations?.[key]||0)),textNode('span',label));receipts.append(item);}
 const trace=el('trace');trace.replaceChildren();if(!d.actions?.length)trace.append(textNode('p','No edits or test runs recorded for this task.','muted'));else{
  const table=document.createElement('table'),head=document.createElement('tr');for(const label of ['Action','File','Result'])head.append(textNode('th',label));table.append(head);
  for(const a of d.actions){const tr=document.createElement('tr');for(const value of [a.kind==='run_tests'?'Test run':a.kind==='edit'?'Code edit':a.kind,a.target,a.result.status])tr.append(textNode('td',value));table.append(tr);}trace.append(table);
 }
 const profiles=d.calibration?.binding?.profiles||[];el('calibration-status').textContent=profiles.length?'Selected calibration: '+profiles.map(p=>p.profile_id+' v'+p.version).join(', ')+'. Evidence declarations are not semantic proof.':'No uncertain-domain calibration selected. Deterministic checks remain active.';
 el('debug-json').textContent=JSON.stringify(d,null,2);
}
function showTaskSummary(st){
 const c=st.contract?.contract,k=c?.constraints;
 el('overview-goal').textContent=c?.goal||'No active task';el('overview-model').textContent='Last task model: '+(st.routing?.model||st.chat?.model||st.model||'not selected');
 const permissions=el('overview-permissions');permissions.replaceChildren();
 if(k)for(const [key,label] of [['edits_allowed','Edits'],['tests_allowed','Tests'],['network_allowed','Network'],['dependencies_allowed','Dependencies']])permissions.append(textNode('span',label+': '+(k[key]?'allowed':'off')));
 el('overview-coverage').textContent=st.auto?st.auto.mapped_files+' / '+st.auto.source_files+' source files mapped. Mapping coverage does not measure review completeness.':st.session?'Task retained. Open Tools to inspect its scope or continue work.':'Start a review from Chat.';
 summaryRows('settings-constraints',k?[['Scope',k.allowed_paths.join(', ')],['Code edits',k.edits_allowed?'Allowed':'Not allowed'],['Test execution',k.tests_allowed?'Allowed':'Not allowed'],['Network',k.network_allowed?'Allowed':'Not allowed'],['Dependencies',k.dependencies_allowed?'Allowed':'Not allowed']]:[['Task','No active task']]);
}
window.addEventListener('message',({data:m})=>{
 if(m.type==='native-status'){
  const d=m.data,r=d.routing;const beganArming=d.arming&&!nativeState.arming,becameArmed=d.armed&&!nativeState.armed;nativeState=d;if(beganArming||becameArmed)el('error').textContent='';updateButtons();
  el('native-status').textContent=d.label+' — '+d.explanation;el('overview-native-status').textContent=d.label;el('overview-native-detail').textContent=d.explanation;
  el('native-default').textContent=(d.defaultsReady?'New sessions: Local chat · Sutra Auto.':'Sutra chat defaults are not fully configured.')+' '+d.reason;
  const last=r?((r.active?'Current Sutra request: ':'Last Sutra request: ')+r.id+' · '+r.stage+' · '+r.model+' · '+(r.finishedAt||r.startedAt)+(r.invocationId?' · protocol invocation '+r.invocationId:'')+(r.conversationBudget?' · conversation calls '+r.conversationBudget.used.calls+'/'+r.conversationBudget.limits.calls+' · '+(r.memoryUsed?'compact memory restored':'new conversation'):'')+(r.message?' · '+r.message:'')):'No Sutra request received since this window loaded.';
  el('native-last-route').textContent=last;
  el('overview-native-last').textContent=last;
 }
  if(m.type==='auto-running'){autoRunning=m.value;updateButtons();}
  if(m.type==='busy'){busy=m.value;updateButtons();el('status').textContent=busy?'Working locally…':'';}
  if(m.type==='error')el('error').textContent=m.message;
  if(m.type==='dashboard')showDashboard(m.data);
  if(m.type==='models'){for(const id of ['model','review-model','api-model','static-model','auto-model']){el(id).replaceChildren(...m.data.map(x=>{const o=document.createElement('option');o.value=x.name;o.textContent=x.name;return o;}));const preferred=id==='api-model'?'qwen3:4b':id==='static-model'&&!current?.static?.model?'qwen3.6:35b':selectedModel;el(id).value=m.data.some(m=>m.name===preferred)?preferred:selectedModel;}}
  if(m.type==='state'){
    current=m.data;showTaskSummary(current);selectedModel=current.model;el('model').value=selectedModel;el('review-model').value=selectedModel;
    el('auto-model').value=selectedModel;
    if(current.auto){const a=current.auto,k=current.contract.contract.constraints;el('auto-goal').value=current.contract.contract.goal;el('auto-roots').value=k.allowed_paths.join(',');for(const [id,key] of Object.entries({'auto-ops':'max_tools','auto-calls':'review_calls','auto-tokens':'review_tokens','auto-source':'review_source_kib','auto-seconds':'review_seconds','auto-page':'review_page_kib'}))el(id).value=k[key];el('auto-state').textContent=`${a.status} · ${a.mapped_files}/${a.source_files} source files mapped · ${a.pending_files} pending · ${a.explained_sites} call sites explained · ${a.metadata_only_files} non-code files cataloged · kernel ${a.budget.decision}`;el('auto-note').textContent=['Next source files:',...a.next_files.map(p=>'• '+p),'','Recent model commentary (not independently verified):',...a.notes.slice(-3).map(n=>n.model+': '+n.summary),'',`Allocation used: ${a.budget.used.calls}/${a.budget.limits.calls} model calls; ${a.budget.used.tokens}/${a.budget.limits.tokens} charged tokens; ${(a.budget.used.source_bytes/1024).toFixed(0)}/${(a.budget.limits.source_bytes/1024).toFixed(0)} KiB source.`,...(Object.keys(a.failures).length?['','Files needing attention:',...Object.entries(a.failures).map(([p,e])=>p+': '+e)]:[]),'',a.scope_notice].join('\n');}else{el('auto-state').textContent='No automatic task yet.';el('auto-note').textContent='';}
    if(current.static?.model)el('static-model').value=selectedModel;
    const sm=current.static;if(sm?.receipt){el('static-files').value=sm.files.join('\n');el('static-offset').value=sm.offset;el('static-state').textContent=`${sm.status} · ${sm.resolved??'?'} resolved / ${sm.calls??sm.total} calls · ${sm.diagnostics??'?'} diagnostics · ${sm.covered_sites.length} call sites explained · ${sm.explanation_status} · receipt ${sm.receipt}${sm.source_changed?' · source changed; progress reset':''}`;el('static-note').textContent=sm.error||sm.commentary||'Deterministic map ready. No model explanation for this snapshot.';}else{el('static-state').textContent='Start an architecture task, then select explicit source files.';el('static-note').textContent='';}
    const route=current.routing,staticCall=current.static?.invocation_id;el('routing').textContent=route?`Routed through Sutra · task ${route.session} · ${route.model} · invocation ${route.invocation_id} · receipt ${route.receipt_id}`:staticCall?`Routed through Sutra · source-map explanation · ${current.static.model} · invocation ${staticCall}`:'Sutra entry point ready. No current accepted model invocation for this view.';
    if(current.architecture){const a=current.architecture,k=current.contract.contract.constraints;el('review-goal').value=current.contract.contract.goal;el('review-roots').value=k.allowed_paths.join(',');el('review-budget').value=k.max_tools;el('review-coverage').textContent=`${a.inspected_files.length}/${a.catalog_files} files with inspected excerpts · tools ${a.budget.used}/${a.budget.limit} · ${a.budget.decision}`;el('review-note').textContent=current.report||current.last_result?.summary||'Catalog recorded. Continue to let the model request files and searches.';}else{el('review-coverage').textContent='';el('review-note').textContent='';}
    const api=current.api;if(api){el('api-state').textContent=`API state: ${api.status} · mapper ${api.candidate_id||'none'} · reviewer ${api.reviewer_id||'none'} · publication receipt ${api.review_id||'none'}`;el('api-note').textContent=api.report||api.error||JSON.stringify(api.decision||api.position||{},null,2);if(api.position){el('api-files').value=api.position.files.join('\n');el('api-offset').value=api.position.offset;}}else{el('api-state').textContent='No API draft for this task.';el('api-note').textContent='';}
    updateButtons();
    if(current.contract){const c=current.contract.contract,k=c.constraints;el('goal').value=c.goal;el('paths').value=k.allowed_paths.join('\n');
      if(k.review_only){el('review-goal').value=c.goal;el('review-roots').value=k.allowed_paths.join(',');el('review-budget').value=k.max_tools;}
      el('network').checked=k.network_allowed;el('dependencies').checked=k.dependencies_allowed;el('edits').checked=!!k.edits_allowed;el('tests').checked=!!k.tests_allowed;
      el('testpaths').value=(k.test_paths||[]).join('\n');el('testtimeout').value=k.test_timeout_ms||10000;el('maxactions').value=k.max_actions||12;
      const extra={...k};for(const name of ['allowed_paths','network_allowed','dependencies_allowed','edits_allowed','tests_allowed','test_paths','test_timeout_ms','max_actions'])delete extra[name];preservedConstraints=extra;
      for(const [id,key] of [['constraint-timeout','timeout_ms'],['constraint-attempts','max_attempts'],['constraint-rows','max_rows'],['constraint-format','output_format']])el(id).value=extra[key]??'';
      const retained=Object.keys(extra).filter(k=>!['timeout_ms','max_attempts','max_rows','output_format'].includes(k));el('preserved-constraints').textContent=retained.length?'Other task-specific constraints are retained automatically.':'';
      el('identity').textContent=`${current.session} · contract version ${current.contract.epoch} · ${current.contract.contract_hash.slice(0,12)}`;
      const checked=new Set([...document.querySelectorAll('#pages input:checked')].map(e=>e.value));el('pages').replaceChildren();
      for(const src of current.sources){for(const p of src.pages){const label=document.createElement('label'),box=document.createElement('input');box.type='checkbox';box.value=p.id;box.checked=checked.has(p.id);label.append(box,document.createTextNode(` ${p.id} · lines ${p.lines.join('–')}`));el('pages').append(label);}}
      if(!checked.size){const first=el('pages').querySelector('input');if(first)first.checked=true;}
      note(current.last_result);proposalId=current.checkpoint;proposalKind=current.last_result?.action?.kind||null;updateButtons();
      el('provenance').textContent=current.checkpoint?`Accepted model proposal · ${current.checkpoint.slice(0,12)}`:'Current host evidence is retained; previous model notes may be stale.';
      el('details').textContent=JSON.stringify({contract:current.contract,execution:current.execution,lastReceipt},null,2);
      if(current.execution){el('action-result').textContent=JSON.stringify(current.execution,null,2);el('action-summary').textContent='Recorded actions are retained in the task history.';}
      if(current.warning)el('error').textContent=current.warning;
      if(current.recovery)el('error').textContent='Interrupted work reconciled: '+current.recovery.map(r=>r.kind+' '+r.status).join(', ');
    }
  }
  if(m.type==='result'){
    const r=m.data;lastReceipt=r;el('status').textContent=r.admission.admitted?(r.review_stage==='api_mapper'?'API draft recorded; independent review required':r.review_stage==='api_reviewer'?'Reviewer opinion recorded; check the API publication decision':`Proposal accepted from ${r.model}`):'Response withheld after validation';
    if(!r.admission.admitted)el('error').textContent=r.admission.errors.join('\n');
  }
  if(m.type==='action'){lastReceipt=m.data;el('action-result').textContent=JSON.stringify(m.data.result,null,2);el('status').textContent='Host action: '+m.data.result.status;el('action-summary').textContent='Last action: '+m.data.result.status;}
});
vscode.postMessage({type:'ready'});

function chatSend(fresh=false){el('error').textContent='';el('chat-progress').textContent='';send('chat_send',{prompt:el('chat-prompt').value,tier:el('chat-tier').value,fresh});}
el('configure-models').onclick=()=>send('configure_models');el('chat-send').onclick=()=>chatSend();el('chat-new').onclick=()=>chatSend(true);el('chat-stop').onclick=()=>send('chat_stop');el('chat-diagram').onclick=()=>send('open_chat_diagram');
el('chat-prompt').onkeydown=e=>{if((e.metaKey||e.ctrlKey)&&e.key==='Enter'){e.preventDefault();chatSend();}};
window.addEventListener('message',({data:m})=>{
 if(m.type==='state'){el('chat-task').textContent=m.data.contract?'Task: '+m.data.contract.contract.goal:'No active task.';el('workflow-notice').hidden=!(m.data.session&&!m.data.auto);if(m.data.chat){el('chat-answer').textContent=m.data.chat.answer?.answer||m.data.chat.warning;}}
 if(m.type==='tier-settings'){el('tier-summary').textContent=['low','mid','high'].map(t=>t+': '+(m.models[t]||'no local chat model available')).join(' · ');showModelChoices(m);}
 if(m.type==='show-section')showSection(m.section);
 if(m.type==='models-saved')el('model-save-status').textContent='Model choices saved.';
 if(m.type==='chat-notice'){el('chat-usage').textContent='';el('chat-progress').textContent='';el('chat-route').textContent='Sutra guidance · no model call';el('chat-answer').textContent=m.data.message;el('chat-diagram').disabled=true;if(m.data.kind==='workflow')el('workflow-notice').hidden=false;}
 if(m.type==='dashboard-error'){expireDashboard('Connection interrupted. Waiting for a fresh reading.');}
 if(m.type==='busy'){el('chat-send').disabled=m.value;el('chat-new').disabled=m.value;el('configure-models').disabled=m.value;el('chat-stop').disabled=!m.value;el('save-model-tiers').disabled=m.value;el('refresh-models').disabled=m.value;}
 if(m.type==='chat-progress')el('chat-progress').textContent=m.message;
 if(m.type==='chat-result'){const d=m.data,p=d.result.accepted_output;el('chat-usage').textContent='Sutra · '+KarmaTokens.compact(d.result.token_usage)+' · Per-stage counts in Usage.';el('chat-progress').textContent='Mapped '+d.coverage.mapped_files+'/'+d.coverage.source_files+' files. Answer excerpts: '+d.source_files.join(', ');
  el('chat-route').textContent=d.route.tier+' · '+d.route.model+' · '+d.route.reason;
  el('chat-answer').textContent=p?p.answer+'\n\nSources:\n'+p.citations.map(c=>c.path+': '+c.quote).join('\n')+'\n\nRemaining:\n'+p.remaining.join('\n'):'Response withheld: '+d.result.admission.errors.join('; ');el('chat-diagram').disabled=!d.diagram;}
});

function showTemperatures(t,reason='No current sensor sample'){
 const table=document.createElement('table');
 for(const group of ['cpu','gpu']){
  const value=t?.[group],available=value?.available&&Number.isFinite(value.maximum_celsius);
  el(group+'-temperature').textContent=available?value.maximum_celsius.toFixed(1)+' °C':'—';
  el(group+'-temperature-info').textContent=available?'Hottest readable sensor · avg '+value.mean_celsius.toFixed(1)+' °C':(value?.reason||reason);
  if(available)for(const r of value.sensors){const tr=document.createElement('tr');for(const label of [group.toUpperCase(),r.key,r.celsius.toFixed(2)+' °C']){const td=document.createElement('td');td.textContent=label;tr.append(td);}table.append(tr);}
 }
 el('temperature-sensors').replaceChildren(table);
}
function expireDashboard(reason){lastTemperatureUpdate=0;showTemperatures(null,reason);el('overview-status').textContent='Waiting for system readings';el('debug-state').textContent=reason;el('sample-age').textContent='No recent sample';el('resource').textContent='Resources unavailable';el('resource').className='pill signal-stop';el('thermal').textContent='OS thermal pressure: unavailable';el('cpu').textContent='—';el('memory').textContent='—';el('pressure').textContent='Waiting for memory data';cpuHistory=[];el('cpu-line').setAttribute('points','');const resource=el('adapter-summary').querySelector('dd');if(resource)resource.textContent='Unavailable';}
setInterval(()=>{if(lastTemperatureUpdate&&Date.now()-lastTemperatureUpdate>8000)expireDashboard('Readings expired. Waiting for a fresh update.');},1000);
let suggestedModels=null;
function showView(view){if(!['dashboard','chat','usage','settings','tools'].includes(view))return;activeView=view;
 for(const [name,id] of Object.entries({dashboard:'dashboard-section',chat:'chat-section',usage:'usage-section',settings:'settings-section',tools:'advanced'})){el(id).hidden=name!==view;el('nav-'+name).setAttribute('aria-pressed',String(name===view));}
 if(view==='tools')el('advanced').open=true;vscode.setState?.({...vscode.getState?.(),view});window.scrollTo?.({top:0,behavior:'instant'});
}
function showSection(section){const view={models:'settings',advanced:'tools',dashboard:'dashboard',chat:'chat'}[section];if(!view)return;showView(view);const id={models:'tier-config',advanced:'advanced',dashboard:'dashboard-section',chat:'chat-section'}[section];const target=el(id);if(target.tagName==='DETAILS')target.open=true;target.scrollIntoView({block:'start'});}
for(const view of ['dashboard','chat','usage','settings','tools'])el('nav-'+view).onclick=()=>showView(view);
el('overview-chat').onclick=()=>showSection('chat');el('overview-settings').onclick=()=>showSection('models');el('settings-task-controls').onclick=()=>showSection('advanced');
showView(vscode.getState?.()?.view||'dashboard');
el('open-current-task').onclick=()=>showSection('advanced');
function showModelChoices(m){
 if(!m.catalog)return;
 const candidates=m.catalog.models.filter(x=>x.eligible===true);
 for(const tier of ['low','mid','high']){
  const select=el('tier-'+tier);select.replaceChildren();
  for(const model of candidates){const o=document.createElement('option');o.value=model.name;o.textContent=model.name+(model.parameter_size?' · '+model.parameter_size:'')+(model.moe?' · MoE':'');select.append(o);}
  const saved=m.models[tier];if(!candidates.some(x=>x.name===saved)){const missing=document.createElement('option');missing.value=saved||'';missing.textContent=saved?saved+' (unavailable — choose a replacement)':'No local chat model available';select.append(missing);}
  select.value=saved||'';
 }
 const defaults=m.catalog.suggested_tiers;
 suggestedModels=defaults&&['low','mid','high'].every(t=>candidates.some(c=>c.name===defaults[t]))?defaults:null;
 el('use-model-defaults').disabled=busy||!suggestedModels;
 const differs=suggestedModels&&['low','mid','high'].some(t=>m.models[t]!==suggestedModels[t]);
 el('model-suggestions').textContent=suggestedModels?'Discovered defaults — '+['low','mid','high'].map(t=>t+': '+suggestedModels[t]).join(' · ')+(differs?'. Your saved choices were preserved. Use discovered defaults, then Save choices, to replace them.':'. These match your saved choices.'):'No complete defaults available; refresh after starting Ollama.';
 el('model-discovery').textContent=candidates.length+' eligible installed models. '+(m.catalog.selection_note||'')+(m.catalog.errors?.length?' Some model metadata could not be read; refresh to retry.':'');
 el('model-save-status').textContent='';
}
el('use-model-defaults').onclick=()=>{if(!suggestedModels)return;for(const tier of ['low','mid','high'])el('tier-'+tier).value=suggestedModels[tier];el('model-save-status').textContent='Suggested choices selected. Click Save choices to apply.';};
for(const tier of ['low','mid','high'])el('tier-'+tier).onchange=()=>{el('model-save-status').textContent='Unsaved model choices. Click Save choices to apply.';};
el('refresh-models').onclick=()=>send('refresh_models');
el('save-model-tiers').onclick=()=>send('save_model_tiers',{models:Object.fromEntries(['low','mid','high'].map(t=>[t,el('tier-'+t).value]))});

window.addEventListener('message',({data:m})=>{
 if(m.type==='runtime-settings'){el('storage-limit').value=m.settings.storageLimitMiB;el('python-path').value=m.settings.pythonPath;}
 if(m.type==='runtime-saved')el('runtime-save-status').textContent='Runtime settings saved. They apply to subsequent requests.';
});
el('save-runtime').onclick=()=>{el('runtime-save-status').textContent='';send('save_runtime_settings',{settings:{storageLimitMiB:Number(el('storage-limit').value),pythonPath:el('python-path').value.trim()}});};
el('overview-native-use').onclick=()=>send('native_use');el('overview-native-arm').onclick=()=>send('native_arm');el('native-use').onclick=()=>send('native_use');el('native-arm').onclick=()=>send('native_arm');el('native-disarm').onclick=()=>send('native_disarm');el('native-toggle').onclick=()=>send('native_toggle');

// Usage filters operate on a bounded, disclosed window of immutable receipts.
let usageData={calls:[],total_calls:0},usageFilters={...vscode.getState?.()?.usageFilters};
function persistUsageFilters(){vscode.setState?.({...vscode.getState?.(),view:activeView,usageFilters});}
for(const key of ['model','stage','status','time']){
 const select=el('usage-'+key);select.value=usageFilters[key]||'';
 select.onchange=()=>{usageFilters[key]=select.value;persistUsageFilters();renderUsage();};
}
el('usage-reset').onclick=()=>{usageFilters={};for(const key of ['model','stage','status','time'])el('usage-'+key).value='';persistUsageFilters();renderUsage();};
function usageTable(id,headers,rows){
 const parent=el(id);parent.replaceChildren();
 if(!rows.length){parent.append(textNode('p','No model calls match these filters.','muted'));return;}
 const table=document.createElement('table'),head=document.createElement('thead'),tr=document.createElement('tr');
 for(const label of headers){const th=textNode('th',label);th.setAttribute('scope','col');tr.append(th);}head.append(tr);table.append(head);
 const body=document.createElement('tbody');for(const values of rows){const row=document.createElement('tr');for(const value of values)row.append(textNode('td',String(value)));body.append(row);}table.append(body);parent.append(table);
}
function showUsage(data){
 usageData=data||{calls:[],total_calls:0};
 el('overview-usage-summary').textContent=KarmaTokens.compact(usageData)+' · latest '+usageData.calls.length+' workspace calls';
 for(const key of ['model','stage']){
  const select=el('usage-'+key),values=[...new Set(usageData.calls.map(c=>c[key]))].sort();
  if(usageFilters[key]&&!values.includes(usageFilters[key]))values.push(usageFilters[key]);
  select.replaceChildren();const all=textNode('option','All '+(key==='model'?'models':'stages'));all.value='';select.append(all);
  for(const value of values){const option=textNode('option',key==='stage'?KarmaTokens.stage(value):value);option.value=value;select.append(option);}
  select.value=usageFilters[key]||'';
 }
 renderUsage();
}
function renderUsage(){
 const rows=usageData.calls.filter(c=>(!usageFilters.model||c.model===usageFilters.model)&&(!usageFilters.stage||c.stage===usageFilters.stage)&&(!usageFilters.status||c.status===usageFilters.status)&&(!usageFilters.time||c.created_at>=Date.now()/1000-Number(usageFilters.time))),t=KarmaTokens.totals(rows);
 for(const key of ['input','output']){const missing=t['missing_'+key+'_tokens'];el('usage-'+key).textContent=KarmaTokens.number(t[key+'_tokens']);el('usage-'+key+'-note').textContent=missing?'Known subtotal · '+missing+' call(s) unknown':rows.length?'Measured across matching calls':'No matching calls';}
 el('usage-calls').textContent=String(rows.length);el('usage-missing').textContent=String(rows.filter(c=>!KarmaTokens.valid(c.input_tokens)||!KarmaTokens.valid(c.output_tokens)).length);
 el('usage-window').textContent=rows.length+' matching calls · latest '+usageData.calls.length+' of '+(usageData.total_calls||0)+' workspace calls. Filters include conversation sessions; repeated source input is counted on each call.';
 const groups=new Map();for(const row of rows){if(!groups.has(row.stage))groups.set(row.stage,[]);groups.get(row.stage).push(row);}
 usageTable('usage-stages',['Stage','Calls','Input tokens','Output tokens'],[...groups].map(([key,calls])=>{const t=KarmaTokens.totals(calls);return [KarmaTokens.stage(key),calls.length,KarmaTokens.metric(t,'input_tokens'),KarmaTokens.metric(t,'output_tokens')];}));
 usageTable('usage-rows',['Time / invocation','Stage / model','Outcome','Input','Output','Budget input / output reserve'],rows.map(c=>[new Date(c.created_at*1000).toLocaleString()+' · '+c.invocation_id,KarmaTokens.stage(c.stage)+' · '+c.model,c.status==='quarantined'?'Withheld':c.status==='pending'?'Pending / interrupted':'Accepted',KarmaTokens.number(c.input_tokens),KarmaTokens.number(c.output_tokens),KarmaTokens.number(c.conservative_input_charge)+' / '+KarmaTokens.number(c.output_reserve)]));
}

el('overview-usage').onclick=()=>send('open_usage');el('usage-dock').onclick=()=>send('open_usage');

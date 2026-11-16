'use strict';
const crypto=require('node:crypto'),path=require('node:path');
const tokens=require('./media/token_usage');
const {NativeMemory,isCompaction}=require('./native_memory');
const hostTokens=require('./host_tokens');
const {profiles}=require('./workspace_authorization');
const {chatActivity}=require('./model_activity');
const ACTION='karma_native_action';
const MODEL_IDS={auto:'karma-auto',low:'karma-low',mid:'karma-mid',high:'karma-high'};
const CHAT_DEFAULTS={'defaultModel':MODEL_IDS.auto,'editor.preferCopilotHarness':false,'defaultToCopilotHarness':false,'editor.localAgent.enabled':true};
function latestRequest(vscode,messages){
  const last=messages.at(-1);
  const results=last?.content?.filter(p=>p instanceof vscode.LanguageModelToolResultPart)||[];
  if(results.length)return {results};
  const user=[...messages].reverse().find(m=>m.role===vscode.LanguageModelChatMessageRole.User&&m.content.some(p=>p instanceof vscode.LanguageModelTextPart));
  let prompt=user?.content.filter(p=>p instanceof vscode.LanguageModelTextPart).map(p=>p.value).join('\n')||'';
  const wrapped=prompt.match(/<userRequest>([\s\S]*?)<\/userRequest>/);
  if(wrapped)prompt=wrapped[1];
  if(!prompt.trim()||prompt.length>1800)throw new Error('Sutra needs a text request of 1–1800 characters. Attach source through the approved workspace scope.');
  const previous=[...messages].reverse().filter(m=>m.role===vscode.LanguageModelChatMessageRole.Assistant);
  const parent=previous.map(m=>m.content.filter(p=>p instanceof vscode.LanguageModelTextPart).map(p=>p.value).join('')).map(text=>{const matches=[...text.matchAll(/\[(?:Karma|Sutra)[^\n]*\]\(karma-preview:\/conversation\/([a-f0-9]{32})\/Response\.md\)/g)];return matches.at(-1)?.[1]||text.match(/^(?:Karma|Sutra) conversation receipt: ([a-f0-9]{32})$/m)?.[1];}).find(Boolean);
  return {prompt,parent};
}
// Model task observations are inert prose. They never create actions or grants.
function observationText(observations, final=false){
  if(!observations)return '';
  const c=observations.commitment,a=observations.assessment;
  if(!c&&!a)return '';
  const safe=value=>String(value||'').replace(/\s+/g,' ').replace(/[\\`*_{}\[\]()<>#|!]/g,'\\$&');
  if(!final){
    if(c?.status!=='recorded')return 'Task commitment: not recorded. Observe-only tracking does not block work.\n\n';
    return '**Task commitment · observe only · v'+c.version+'**\n\n'+safe(c.value.understanding)+'\n\n'+c.value.criteria.map(x=>'- '+safe(x.deliverable)+' — '+safe(x.done_when)).join('\n')+'\n\n';
  }
  const status=observations.delivery_status==='withheld'?'Response withheld.':observations.delivery_status==='awaiting_action'?'Action proposed; execution is pending.':'Response delivered; task success is not verified.';
  const lines=['**Task assessment · observe only**','',status,'','**Asked:** '+safe(c?.request||a?.request)];
  if(a?.status==='recorded'){
    lines.push('','**Model says it delivered:** '+safe(a.value.delivered),'','**Model judgement:** '+safe(a.value.judgement)+' (unverified).',
      '','**Remaining, per model:** '+(a.value.remaining.map(safe).join('; ')||'None reported; this is not verification.'));
    if(a.issues?.length)lines.push('','Observation issues: '+a.issues.map(safe).join('; '));
  }else lines.push('','Completion assessment: '+safe(a?.status||'not recorded')+'.');
  return lines.join('\n')+'\n\n';
}
function install(vscode,context,deps){

  let allocationPending=false;
  let armPromise,accessEpoch=0,armingStep='',armed,activeCancel,arming=false,running=false,selecting=false,lastRoute='',routing,reason='Open Sutra chat to talk. Choose workspace permissions on first use.';
  const memory=new NativeMemory(context);let lastCompaction;
  const tickets=new Map(),completed=new Map(),changed=new vscode.EventEmitter();
  const enabled=()=>vscode.workspace.getConfiguration('karma').get('nativeEnabled',true);
  const authorization=deps.authorization;
  function matchesTask(state,binding){
    return !!binding && state.session===binding.session &&
      state.contract?.contract_hash===binding.contract_hash &&
      state.calibration?.binding?.hash===binding.calibration_hash;
  }
  function clearAccess(why, cancel=true){
    accessEpoch++;
    armed=undefined;
    tickets.clear();
    completed.clear();
    if(cancel)activeCancel?.cancel();
    reason=why;
  }
  function refreshAccess(){
    let grant,scope;
    if(enabled()){
      try{
        scope=authorization.scope();
        grant=authorization.read();
      }catch(error){
        reason=error.message;
      }
    }
    const valid=grant?.scope===scope && typeof scope==='string' &&
      typeof grant.id==='string' && Object.hasOwn(profiles,grant.profile);
    if(armed && (!valid || grant.id!==armed.generation || scope!==armed.scope || grant.profile!==armed.profile)){
      clearAccess('Workspace permissions changed. The saved request and budget are retained.');
    }
    if(valid && !armed){
      armed={scope,workspace:deps.workspace(),generation:grant.id,profile:grant.profile};
      reason='Using saved '+profiles[grant.profile].toLowerCase()+' permissions for this workspace.';
    }
    return armed;
  }
  function status(){
    const access=refreshAccess();
    const chat=vscode.workspace.getConfiguration('chat');
    const defaultsReady=Object.entries(CHAT_DEFAULTS).every(([key,value])=>chat.get(key)===value);
    const live=!!routing?.active;
    const label=!enabled()?'Sutra: off':arming?'Sutra: awaiting permission choice':selecting?'Sutra: opening chat':live?'Sutra: request received':running?'Sutra: action running':
      'Sutra · '+(access?profiles[access.profile]:'chat only')+' · routing unverified';
    const explanation=arming?armingStep:selecting?'Opening Local chat with Sutra Auto. No prompt is being sent.':live?'This request reached Sutra. Protocol admission is checked before model dispatch.':
      'No Sutra request is in flight. The current chat selection is unverified; check for Sutra Auto beneath the prompt. Direct Ollama and other chats bypass Sutra.';
    return {enabled:enabled(),arming,armingStep,selecting,armed:!!access,accessProfile:access?.profile||null,working:allocationPending||running||live,session:routing?.session,scope:'Sutra native chat and its workspace tools',defaultModel:chat.get('defaultModel',''),defaultsReady,reason,lastRoute,label,explanation,routing:routing?{...routing}:null,compaction:lastCompaction,pending:context.workspaceState.get('sutra.pendingOrchestration')||null};
  }
  function publish(){deps.onStatus(status());}
  function revoke(){
    // A new record also invalidates dialogs open in another window.
    authorization.write({id:crypto.randomUUID()});
    clearAccess('Workspace access revoked. The saved request and budget are retained.');
    publish();
  }
  function assertArmed(){
    const access=refreshAccess();
    if(arming)throw new Error('A workspace permission choice is in progress.');
    if(!access)throw new Error('Sutra workspace access is required. Open Sutra: Workspace Permissions.');
    return access;
  }
  function assertCurrent(access,token){
    if(refreshAccess()!==access || !enabled() || token?.isCancellationRequested)
      throw new Error('Sutra request cancelled or workspace permission changed.');
  }
  async function check(t,token){
    const st=await deps.request({op:'state'},token);
    ticket({ticket:t.id});
    if(!matchesTask(st,t.binding)){
      tickets.delete(t.id);
      throw new Error('Task or calibration changed. Request a fresh proposal under the current contract.');
    }
  }
  function beginArm(owner, agentOnly=false){
    if(armPromise)return armPromise;
    armPromise=arm(owner,agentOnly).finally(()=>{armPromise=undefined;});return armPromise;
  }
  async function arm(owner,agentOnly){
    const ownsRequest=()=>owner&&routing===owner.audit&&routing.active&&running&&activeCancel?.token===owner.token;
    if(arming||selecting||deps.isBusy()||((running||routing?.active)&&!ownsRequest()))throw new Error('Wait for the active Sutra request before changing workspace permissions.');
    if(!enabled())throw new Error('Enable Sutra native chat first.');
    const previous=refreshAccess(),epoch=accessEpoch,workspace=deps.workspace();
    const scope=authorization.scope(),record=authorization.read();
    const stillValid=()=>{
      if(epoch!==accessEpoch||!enabled()||deps.workspace()!==workspace||authorization.scope()!==scope||
        authorization.read()?.id!==record?.id||owner&&(!ownsRequest()||owner.token.isCancellationRequested))
        throw new Error('Workspace permission choice cancelled because the request or workspace changed.');
      if(deps.isBusy())throw new Error('Workspace became busy during the permission choice.');
    };
    arming=true;
    armingStep='Choose Sutra workspace permissions in the dialog.';
    const access={id:crypto.randomUUID(),workspace,state:'requested',requestedAt:new Date().toISOString(),source:'vscode_modal'};
    const recordAccess=state=>{
      access.state=state;
      access.updatedAt=new Date().toISOString();
      if(owner){
        owner.audit.workspaceAccess={...access};
        publish();
        deps.onRoute?.({...owner.audit});
      }
    };
    try{
      publish();
      recordAccess('requested');
      const choices=agentOnly?[profiles.workspace_agent]:Object.values(profiles);
      const choice=await vscode.window.showWarningMessage('Workspace permissions for Sutra',{modal:true,detail:
        workspace+'\n\n'+(previous?'Current access: '+profiles[previous.profile]+'\n':'')+
        'Read only lets Sutra read the scoped project files. Workspace agent also enables scoped edits and sandboxed tests. Sutra requests action confirmation through VS Code and confirms actions separately for tool calls outside chat. Your choice is saved for this workspace. Optional tools keep their separate approval rules.'},...choices);
      stillValid();
      const profile=Object.keys(profiles).find(key=>profiles[key]===choice);
      if(!profile||!choices.includes(choice)){
        recordAccess('declined');
        return false;
      }
      if(previous?.profile!==profile){
        authorization.write({id:access.id,scope,profile});
        clearAccess('', false);
        refreshAccess();
      }
      recordAccess('granted');
      return true;
    }catch(error){
      recordAccess('cancelled');
      throw error;
    }finally{
      arming=false;
      armingStep='';
      publish();
    }
  }
  // UI effects only: the host state machine selects work, forks and joins.
  async function approvePermission(descriptor,token){
    let paths=descriptor.paths;
    if(!paths.length){
      const chosen=await vscode.window.showOpenDialog({title:'Select Python files for '+descriptor.operation,defaultUri:vscode.Uri.file(deps.workspace()),canSelectMany:true,filters:{Python:['py']}});
      if(!chosen?.length)return;
      paths=chosen.map(f=>path.relative(deps.workspace(),f.fsPath).split(path.sep).join('/'));
    }
    if(descriptor.operation==='test'&&process.platform!=='darwin')throw new Error('Sandboxed test execution is currently available on macOS only. No test permission was granted.');
    const test_paths=descriptor.operation==='test'?[...paths]:[];
    const label=descriptor.operation==='edit'?'Allow scoped edits':'Allow scoped tests';
    const yes=await vscode.window.showWarningMessage('Sutra needs permission for this request.',{modal:true,detail:'Files: '+paths.join(', ')+'\nTask: '+descriptor.goal+'\nNetwork and dependencies remain off. This file scope persists with the task across model switches. Workspace access can be revoked from Sutra settings. Every edit or test still has its own confirmation.'},label);
    if(yes!==label||token.isCancellationRequested)return;
    return {operation:descriptor.operation,paths,test_paths};
  }
  const pendingKey='sutra.pendingOrchestration';
  async function rememberFlow(flow){
    const terminal=flow.state==='DONE'||flow.state==='CANCELLED';
    const saved=context.workspaceState.get(pendingKey);
    // Finishing a conversational turn must not erase another saved review.
    if(!terminal||saved?.flow_id===flow.flow_id)
      await context.workspaceState.update(pendingKey,terminal?undefined:{...flow,workspace:deps.workspace(),conversation:undefined,routing_step:undefined,child:undefined,data:undefined,mapping:undefined,allocation_proposal:undefined,allocation_amendment:undefined});
    publish();
  }
  async function recoverSaved(discard=false){
    if(allocationPending||running||routing?.active||deps.isBusy())throw new Error('Stop the active request and wait for cleanup before changing the saved workflow.');
    const pending=context.workspaceState.get(pendingKey);
    if(!pending||pending.workspace!==deps.workspace()){void vscode.window.showInformationMessage('No saved workflow in this workspace.');return;}
    if(discard){
      const yes=await vscode.window.showWarningMessage('Discard the saved Sutra request?',{modal:true,detail:'Consumed budgets, receipts and task permissions are retained. Unused child reservations are released. Executed actions are not undone.'},'Discard request');
      if(yes!=='Discard request')return;
      const current=await deps.request({op:'orchestrate',event:'inspect',flow_id:pending.flow_id});
      await rememberFlow(['DONE','CANCELLED'].includes(current.state)?current:await deps.request({op:'orchestrate',event:'cancel',flow_id:current.flow_id,cursor:current.cursor}));
    }else{
      const current=await deps.request({op:'orchestrate',event:'inspect',flow_id:pending.flow_id});
      await rememberFlow(current);

      // Focus the existing chat without replacing its draft, model, or history.
      await vscode.commands.executeCommand('workbench.action.chat.open');
      void vscode.window.showInformationMessage(current.recovery?current.recovery.message+' Send continue in Sutra chat to choose recovery.':'Resume selected. Send continue with a Sutra model to resume the saved request with its original context and remaining budget. Other messages remain separate.');
    }
  }
  async function increaseAllocation(){
    if(allocationPending||running||routing?.active||deps.isBusy())throw new Error('Wait for the current Sutra operation before changing allocation.');
    const pending=context.workspaceState.get(pendingKey),workspace=deps.workspace();
    if(!pending||pending.workspace!==workspace)throw new Error('No saved workflow in this workspace.');
    allocationPending=true;publish();
    try{
      const current=await deps.request({op:'orchestrate',event:'inspect',flow_id:pending.flow_id});
      const preview=await deps.request({op:'orchestrate',event:'allocation_preview',flow_id:current.flow_id,cursor:current.cursor});
      const p=preview.allocation_proposal;
      const labels={tokens:'Model budget units',calls:'Model calls',operations:'Repository operations',source_bytes:'Source bytes',seconds:'Execution seconds'};
      const fmt=x=>Number(x).toLocaleString(undefined,{maximumFractionDigits:1});
      const rows=Object.entries(p.additions).map(([key,add])=>labels[key]+': '+fmt(p.before[key])+' + '+fmt(add)+' = '+fmt(p.after[key])+' total. Spent '+fmt(p.used[key])+'; '+fmt(p.remaining_after[key])+' remains.');
      const yes=await vscode.window.showWarningMessage('Increase this task’s allocation?',{modal:true,detail:
        p.goal+'\n\n'+rows.join('\n')+'\n\nThis adds capacity; it does not reset spending. A new bounded execution can use the added capacity. Context, constraints and permissions are retained. Model context and memory limits still apply. Approval does not start a model call. Continue the saved request afterward.'},'Increase allocation');
      if(yes!=='Increase allocation')return;
      if(deps.workspace()!==workspace||running||routing?.active||deps.isBusy())throw new Error('The workspace or request changed. Review a fresh allocation proposal.');
      const next=await deps.request({op:'orchestrate',event:'allocation_grant',flow_id:preview.flow_id,cursor:preview.cursor,confirmed:true,proposal:p});
      await rememberFlow(next);
      void vscode.window.showInformationMessage('Allocation increased (version '+next.allocation_amendment.version+'). Spending and permissions are unchanged. Send continue in this chat; model and memory checks still apply.');
    }finally{allocationPending=false;publish();}
  }
  let activityProgress;
  async function flowEvent(flow,event,tier,token,extra={}){
    const access=refreshAccess();
    const models=['inspect','pending','cancel'].includes(event)?undefined:await deps.configureModels(false,token);
    const workspaceStep=['step','resume','grant','capability_grant','restart_review'].includes(event) &&
      flow.state!=='CLASSIFYING' && flow.stop?.resume_phase!=='CLASSIFYING';
    if(workspaceStep){
      assertCurrent(access,token);
      assertArmed();
    }
    const binding=flow?.binding||flow?.bound_task;
    const next=await deps.request({op:'orchestrate',event,flow_id:flow?.flow_id,cursor:flow?.cursor,
      armed:!!access,binding,models,tier,...extra},token,activityProgress);
    await rememberFlow(next);return next;
  }
  function showPause(flow,audit,progress){
    const stop=flow.stop||{},failures=stop.failures||[];
    const details=[stop.error,...failures.map(f=>f.path+': '+f.error)].filter(Boolean);
    if(stop.status==='resource_wait'){
      const stage={CLASSIFYING:'Conversation routing',MAPPING:'Source mapping',CHILDREN:'Child review',SYNTHESIS:'Workspace synthesis',CODING:'Workspace coding'}[stop.resume_phase]||'Workflow';
      details.unshift('Host resource check · '+stage+' paused. This is host status, not a model response.');
    }
    const allocationStop=['context_budget','task_budget','budget_stopped'].includes(stop.status)||stop.status==='memory_fault'&&stop.memory_fault?.code==='capacity';
    if(allocationStop)details.unshift('Host allocation check paused the workflow before the next model call. No reasoning stream was generated for that call.');
    const coverage=Number.isInteger(stop.mapped_files)&&Number.isInteger(stop.source_files)?` Mapped ${stop.mapped_files} of ${stop.source_files} source files.`:'';
    if(stop.recovery)details.push('Runtime memory recovery: '+(stop.recovery.error||stop.recovery.action?.replaceAll('_',' ')||'observed')+'. Task allocation is unchanged.');
    if(stop.memory_fault)details.push('Memory recovery: '+stop.memory_fault.recovery.replaceAll('_',' ')+'. Exact saved pages and task grants are retained.');
    const reason=details.length?details.join('\n'):(stop.status||'Inspect Sutra Monitor for details.');
    audit.stage='paused';audit.message=reason;publish();
    const guidance=stop.status==='resource_wait'
      ? 'Check both **System pressure** and the **Last model allocation** in Sutra Monitor. Healthy system pressure alone does not mean the selected model fits. After resources recover, or after selecting a smaller configured model, use **Resume** and send **continue** here. Resume checks a fresh measurement and model allocation before dispatch. Sutra will not automatically retry this stop.'
      : allocationStop?'Choose **Increase allocation…** in Sutra Monitor to review and confirm additional task capacity, then send **continue** here. This preserves all spending and does not override model or hardware limits.':'Resolve the reported cause, then use **Resume** in Sutra Monitor and send **continue** here.';
    const title=stop.resume_phase==='CLASSIFYING'?'Sutra paused the request.':'Sutra paused the review.';
    progress.report(new vscode.LanguageModelTextPart('**'+title+'**'+coverage+'\n\n'+reason+'\n\n'+(stop.failure_count>failures.length?`${stop.failure_count-failures.length} more file failures are recorded in the review details.\n\n`:'')+'The request, context, permissions and consumed budget are saved. '+guidance+' Resume keeps the remaining allocation; it does not reset a limit. Use **Discard** to abandon this request.'));
  }
  async function selectNativeChat(){
    if(selecting)throw new Error('Sutra chat selection is already in progress.');
    selecting=true;publish();
    try{
      if(!enabled())throw new Error('Enable Sutra native chat first.');
      const commands=await vscode.commands.getCommands(true);
      if(!commands.includes('workbench.action.chat.newLocalChat'))throw new Error('This VS Code version cannot open the compatible Local chat. Sutra has not changed your chat preferences.');
      const selector={vendor:'karma',id:MODEL_IDS.auto,family:'karma'};
      const models=await vscode.lm.selectChatModels(selector);
      if(!models.some(m=>m.id===MODEL_IDS.auto))throw new Error('Sutra Auto is not available yet. Reload VS Code and retry.');
      const chat=vscode.workspace.getConfiguration('chat');
      const previous=new Map(),written=[];
      for(const [key,value] of Object.entries(CHAT_DEFAULTS)){
        const inspected=chat.inspect?.(key);
        if(inspected?.policyValue!==undefined&&inspected.policyValue!==value)throw new Error('Your organization manages chat.'+key+'. Sutra cannot save a compatible chat default.');
        previous.set(key,inspected?.globalValue);
      }
      try{
        for(const [key,value] of Object.entries(CHAT_DEFAULTS)){
          if(chat.inspect?.(key)?.globalValue===value)continue;
          await chat.update(key,value,vscode.ConfigurationTarget.Global);written.push(key);
        }
        const effective=vscode.workspace.getConfiguration('chat');
        for(const [key,value] of Object.entries(CHAT_DEFAULTS))if(effective.get(key)!==value)throw new Error('A workspace or managed setting overrides chat.'+key+'. Resolve that override to use Sutra as the saved default.');
        // Reveal the chat panel first so selection cannot target a stale chat editor.
        await vscode.commands.executeCommand('workbench.panel.chat.view.copilot.focus');
        await vscode.commands.executeCommand('workbench.action.chat.newLocalChat');
        await vscode.commands.executeCommand('workbench.action.chat.open',{modelSelector:selector,toolsInclude:[ACTION]});
      }catch(error){
        for(const key of written.reverse()){
          // Preserve a concurrent user change rather than overwriting it in rollback.
          if(vscode.workspace.getConfiguration('chat').inspect?.(key)?.globalValue===CHAT_DEFAULTS[key])await chat.update(key,previous.get(key),vscode.ConfigurationTarget.Global);
        }
        throw error;
      }
      reason='Local chat and Sutra Auto saved for new sessions. Workspace permissions are managed separately.';
      publish();
    }finally{selecting=false;publish();}
  }
  async function useNativeChat(){
    if(arming){void vscode.window.showInformationMessage(armingStep);return;}
    if(running||routing?.active||deps.isBusy())throw new Error('A Sutra operation is running. Wait for it to finish before opening another chat.');
    await selectNativeChat();
  }
  async function trackRequest(model,progress,token,invoke){
    if(allocationPending||routing?.active||running||deps.isBusy())throw new Error('A Sutra request is running. Wait before switching models.');
    const audit={id:crypto.randomUUID(),model:model.id,startedAt:new Date().toISOString(),active:true,stage:'received'};
    routing=audit;publish();deps.onRoute?.({...audit});
    try{
      // Live routing is shown in the status bar and dashboard; keep audit IDs out of the answer body.
      await invoke(audit);
      if(audit.stage==='received')audit.stage='stopped';
    }catch(error){audit.stage=token.isCancellationRequested||/cancel|revoked/i.test(error.message)?'cancelled':'blocked';audit.message=error.message;if(/Ollama|model.*unavailable|model.*discover|No local chat model/i.test(error.message))void vscode.window.showErrorMessage?.(error.message,'Model settings')?.then(choice=>{if(choice==='Model settings')return deps.configureModels(true);}).catch(()=>{});throw error;}
    finally{audit.active=false;audit.finishedAt=new Date().toISOString();publish();deps.onRoute?.({...audit});}
  }
  function ticket(input){
    const t=tickets.get(input?.ticket),access=assertArmed();
    if(!t||t.generation!==access.generation||Date.now()>t.expires)
      throw new Error('Missing, expired or revoked Sutra action ticket.');
    if(access.profile!=='workspace_agent')throw new Error('Edits and tests require the Workspace agent profile.');
    return t;
  }
  const tool={
    prepareInvocation(options){const t=ticket(options.input);const md=new vscode.MarkdownString();md.appendText(t.kind==='edit'?'Apply this exact proposed edit to '+t.preview.target+'?':'Run the permitted Python test '+t.preview.target+' in a read-only snapshot with network denied?');if(t.preview.snapshot_files)md.appendText(' Snapshot files: '+t.preview.snapshot_files.join(', ')+'. '+t.preview.snapshot_note);if(t.preview.diff)md.appendCodeblock(t.preview.diff,'diff');md.isTrusted=false;
      return {invocationMessage:t.kind==='edit'?'Applying governed edit':'Running governed test',confirmationMessages:{title:'Sutra: '+(t.kind==='edit'?'apply edit':'run test'),message:md}};},
    async invoke(options,token){
      if(running||deps.isBusy())throw new Error('A Sutra operation is already running.');
      if(token.isCancellationRequested)throw new Error('Sutra action cancelled.');
      const t=ticket(options.input);await check(t,token);if(ticket(options.input)!==t)throw new Error('Action revoked.');
      // VS Code may skip prepareInvocation confirmations for programmatic calls
      // without a chat token. Require a host dialog for that path as well.
      if(!options.toolInvocationToken){
        const label=t.kind==='edit'?'Apply edit':'Run test';
        const confirmed=await vscode.window.showWarningMessage('Sutra: '+label+' · '+t.preview.target,{modal:true,detail:t.preview.diff||'Run the permitted Python unittest in a read-only snapshot with network denied. Exit status is a process observation; assertions remain unverified.'+(t.preview.snapshot_files?' Snapshot files: '+t.preview.snapshot_files.join(', ')+'. '+t.preview.snapshot_note:'')},label);
        if(confirmed!==label)throw new Error('Sutra action not confirmed.');
        await check(t,token);if(ticket(options.input)!==t)throw new Error('Action revoked.');
      }
      if(running||deps.isBusy())throw new Error('Another Sutra operation started.');
      if(token.isCancellationRequested)throw new Error('Sutra action cancelled.');
      tickets.delete(options.input.ticket);running=true;activeCancel=new vscode.CancellationTokenSource();const cancel=token.onCancellationRequested(()=>activeCancel?.cancel());publish();
      try{const access=assertArmed();const data=await deps.request({op:'native_execute',...t.binding,invocation_id:t.invocation_id},activeCancel.token);const result={...data.action_receipt,assertions_verified:false};assertCurrent(access,activeCancel.token);completed.set(t.callId,{prompt:t.prompt,conversationId:t.conversationId,result,generation:t.generation,binding:t.binding});deps.publishReview(data);
        return new vscode.LanguageModelToolResult([new vscode.LanguageModelTextPart(JSON.stringify({receipt:result.id,status:result.result.status,target:t.preview.target,returncode:result.result.returncode,assertions_verified:false,note:'A zero exit status is a process observation, not verified test assertions.'}))]);
      }finally{cancel.dispose();activeCancel.dispose();activeCancel=undefined;running=false;publish();}
    }
  };
  const provider={onDidChangeLanguageModelChatInformation:changed.event,
    provideLanguageModelChatInformation(){if(!enabled())return [];const models=deps.modelSettings();return ['auto','low','mid','high'].filter(id=>id==='auto'||models[id]).map(id=>({id:MODEL_IDS[id],name:id==='auto'?'Sutra Auto':'Sutra '+id[0].toUpperCase()+id.slice(1),family:'karma',version:require('./package.json').version,maxInputTokens:64000,maxOutputTokens:4096,detail:id==='auto'?'Governed local routing':models[id]||'Configure this tier',tooltip:'Chat normally with saved workspace permissions. Every local call uses the invocation gateway.',capabilities:{toolCalling:true,imageInput:false}}));},
    provideTokenCount(model,input,token){if(token?.isCancellationRequested)throw new Error('Token count cancelled.');return hostTokens.count(input);},
    async provideLanguageModelChatResponse(model,messages,options,progress,token){
      if(isCompaction(vscode,messages)){
        if(token.isCancellationRequested)throw new Error('Sutra compaction cancelled.');
        if(!enabled())throw new Error('Sutra is disabled.');
        const saved=memory.resolve(vscode,messages,options);
        // Compaction is host bookkeeping: carry an authenticated receipt pointer.
        // Never send the summarizer instruction to a model as a new user task.
        if(!saved.parent){
          progress.report(new vscode.LanguageModelTextPart('<summary>Sutra has no authenticated checkpoint in this supplied history. Missing context must be restated; no permissions are granted.</summary>'));
          lastCompaction={status:'missing_checkpoint',time:new Date().toISOString()};publish();
          deps.onRoute?.({id:crypto.randomUUID(),stage:'compaction_missing_checkpoint',active:false,model:model.id});return;
        }
        // The next real invocation revalidates receipt ancestry and quarantine.
        // No bridge/model call here: background compaction cannot race inference.
        progress.report(new vscode.LanguageModelTextPart(memory.summary(saved.parent)));
        lastCompaction={status:'checkpoint_preserved',invocationId:saved.parent,memorySource:saved.source,time:new Date().toISOString()};publish();
        deps.onRoute?.({id:crypto.randomUUID(),stage:'compaction_checkpoint_preserved',active:false,invocationId:saved.parent,memorySource:saved.source,model:model.id});return;
      }
      return trackRequest(model,progress,token,async audit=>{
      if(token.isCancellationRequested)throw new Error('Sutra request cancelled.');
      if(arming||!enabled()){
        audit.stage=arming?'awaiting_confirmation':'needs_arming';
        const guidance=arming?armingStep:'Enable native chat in Sutra Settings.';
        progress.report(new vscode.LanguageModelTextPart('You are connected to Sutra. '+guidance+'\n\nSetup guidance only — no model call, source read, edit, or test was performed.'));
        return;
      }
      if(running||deps.isBusy())throw new Error('A Sutra request is running. Wait before switching models.');
      const request=latestRequest(vscode,messages);
      if(!request.results){const saved=memory.resolve(vscode,messages,options,request.parent);request.parent=saved.parent;audit.memorySource=saved.source;}
      let prompt=request.prompt,conversationId,actionBinding;
      if(request.results){assertArmed();if(request.results.length!==1)throw new Error('Unexpected tool results for Sutra.');const known=completed.get(request.results[0].callId);if(!known||known.generation!==armed.generation)throw new Error('Tool result lacks a current Sutra host receipt.');if(!['applied','process_exited_zero','process_failed'].includes(known.result.result.status)){progress.report(new vscode.LanguageModelTextPart('Sutra stopped after '+known.result.result.status+'. Inspect the host action receipt before continuing.'));return;}prompt=known.prompt;conversationId=known.conversationId;actionBinding=known.binding;}
      let a=refreshAccess();
      const tier=Object.keys(MODEL_IDS).find(key=>MODEL_IDS[key]===model.id);
      if(!tier)throw new Error('Unknown Sutra tier');
      const tools=options.tools||[],canAct=tools.some(t=>t.name===ACTION);
      if(options.toolMode===vscode.LanguageModelChatToolMode.Required&&!canAct)throw new Error('Required tool mode does not include the Sutra action tool.');
      let responseUsage={calls:[]},footerId,footerSignature;
      const recordUsage=result=>{responseUsage=tokens.merge(responseUsage,result?.token_usage);audit.token_usage=responseUsage;};
      running=true;activeCancel=new vscode.CancellationTokenSource();const cancellation=token.onCancellationRequested(()=>activeCancel?.cancel());publish();
      async function allowActionProfile(){
        assertCurrent(a,activeCancel.token);
        if(a.profile==='workspace_agent')return true;
        if(!await beginArm({audit,token:activeCancel.token},true)){
          progress.report(new vscode.LanguageModelTextPart('Sutra remains in Read only mode. Edits and tests require Workspace agent permissions.'));
          return false;
        }
        a=assertArmed();
        return true;
      }
      const activityToken=activeCancel.token;let activityOpen=true;
      const renderActivity=chatActivity(vscode,progress,deps.activityLink);
      activityProgress=e=>{
        if(!activityOpen||activityToken.isCancellationRequested)return;
        renderActivity(e);
      };
      try{
        let data,flow,restartReview=false,resuming=false;
        if(!request.results){
          let pending=context.workspaceState.get(pendingKey);
          if(!pending){const saved=await flowEvent(null,'pending',tier,activeCancel.token);if(saved.flow_id)pending={...saved,workspace:deps.workspace()};}
          const resumeRequested=/^(continue|resume|please continue|please resume)[.! ]*$/i.test(prompt.trim());
          if(resumeRequested&&pending?.workspace===deps.workspace()){
            const old=await deps.request({op:'orchestrate',event:'inspect',flow_id:pending.flow_id},activeCancel.token);
            if(['DONE','CANCELLED'].includes(old.state))await rememberFlow(old);
            else if(old.recovery){
              await rememberFlow(old);
              const recovery=old.recovery;
              if(!recovery.restart_allowed){
                progress.report(new vscode.LanguageModelTextPart(recovery.message+'\n\nUse **Discard** in Sutra Monitor, then submit your new request.'));return;
              }
              const choice=await vscode.window.showWarningMessage('This review was started by an older Sutra policy.',{modal:true,detail:recovery.message+'\nSaved request: '+old.prompt},'Restart review');
              if(activeCancel.token.isCancellationRequested||choice!=='Restart review'){
                progress.report(new vscode.LanguageModelTextPart('Saved request retained. '+recovery.message));return;
              }
              flow=old;prompt=old.prompt;restartReview=true;
              conversationId=undefined;footerId=undefined;footerSignature=undefined;
            }else {

              resuming=true;
              flow=old;prompt=old.prompt;conversationId=old.conversation_id;audit.memoryUsed=true;audit.conversationInvocationId=conversationId;
              if(conversationId){footerSignature=await memory.remember(conversationId,options);footerId=conversationId;}
              progress.report(new vscode.LanguageModelTextPart('Resuming the saved Sutra workflow with its context and remaining budget.\n\n'));
            }
          }
          if(!flow)flow=await flowEvent(null,'start',tier,activeCancel.token,{prompt,parent:request.parent});
          if(resuming&&flow.state==='PAUSED'&&flow.stop?.resume_phase==='CLASSIFYING'){
            flow=await flowEvent(flow,'resume',tier,activeCancel.token);
          }
          if(resuming&&flow.state==='CLASSIFYING')flow=await flowEvent(flow,'step',tier,activeCancel.token);
          while(flow.routing_step||flow.state==='CLASSIFYING'){
            assertCurrent(a,activeCancel.token);
            if(flow.routing_step){
              const step=flow.routing_step;recordUsage(step.result);lastRoute=step.route.model;audit.model=lastRoute;
              audit.routingDecision=step.routing_followup;audit.stage='routing';publish();
              progress.report(new vscode.LanguageModelTextPart((step.routing_followup?.kind==='page_in'?'Loading the requested conversation context before choosing a model.':step.routing_followup?.kind==='reasoning'?'Checking a higher supported reasoning setting on the same model against the remaining budget.':step.routing_followup?.kind==='escalate'?'Checking the next model tier against the remaining budget and available memory.':flow.progress)+'\n\n'));
            }
            if(flow.state==='PAUSED'){showPause(flow,audit,progress);return;}
            if(flow.state!=='CLASSIFYING')break;
            flow=await flowEvent(flow,'step',tier,activeCancel.token);
          }
          if(flow.state==='PAUSED'&&flow.stop?.resume_phase==='CLASSIFYING'){showPause(flow,audit,progress);return;}
          const chat=flow.conversation;
          if(chat){
          assertCurrent(a,activeCancel.token);
          const r=chat.result;recordUsage(r);lastRoute=chat.route.model;audit.model=lastRoute;audit.memoryUsed=chat.memory_used;audit.memoryAllocation=chat.memory_allocation;audit.memoryStatus=chat.memory_status;audit.modelCapacity=chat.model_capacity;audit.conversationBudget=chat.budget;audit.invocationId=r.invocation_id;audit.stage=r.admission.admitted?'conversation':'withheld';publish();
          if(!r.admission.admitted){progress.report(new vscode.LanguageModelTextPart('Sutra withheld the conversation response: '+r.admission.errors.join('; ')));return;}
          const p=r.accepted_output;
          if(options.toolMode===vscode.LanguageModelChatToolMode.Required&&flow.state==='DONE'&&!flow.deferred_flow_id)throw new Error('No admitted action is available to satisfy required tool mode.');
          footerSignature=await memory.remember(r.invocation_id,options);footerId=r.invocation_id;
          if(flow.deferred_flow_id){
            const old=await deps.request({op:'orchestrate',event:'inspect',flow_id:flow.deferred_flow_id},activeCancel.token);
            await rememberFlow(old);
            const choice=await vscode.window.showWarningMessage('Another Sutra workspace request is saved.',{modal:true,detail:'New request: '+prompt+'\nSaved request: '+old.prompt+'\nDiscard releases unused child reservations. Consumed budgets, receipts and task permissions remain. To resume the saved request instead, cancel and send continue.'},'Discard and use new request');
            if(activeCancel.token.isCancellationRequested||choice!=='Discard and use new request'){
              progress.report(new vscode.LanguageModelTextPart('Saved request retained. Your new workspace request has not run. Send **continue** to resume the saved request, or use **Discard** in Sutra Monitor before submitting a new workspace request. General conversation can continue.'));return;
            }
            assertCurrent(a,activeCancel.token);
            await flowEvent(old,'cancel',tier,activeCancel.token);
            flow=await flowEvent(null,'start_existing',tier,activeCancel.token,{prompt,conversation_id:r.invocation_id});
          }
          // Routing prose is not an authority on host capabilities or permissions.
          // Present a host-owned transition; publish model prose for conversation.
          progress.report(new vscode.LanguageModelTextPart((flow.state!=='DONE'?'Continuing this request through Sutra’s workspace workflow.':p.answer)+'\n\n'));
          if(p.diagram){try{await deps.presentConversation?.(chat);progress.report(new vscode.LanguageModelTextPart('Diagram opened beside the chat. Use **Sutra: Open Last Diagram** to reopen it.\n\n'));}catch(error){progress.report(new vscode.LanguageModelTextPart('Diagram preview could not open: '+error.message+'\n\n'));}}
          if(flow.state==='DONE')return;
          const commitmentText=observationText(flow.task_observations);
          if(commitmentText)progress.report(new vscode.LanguageModelTextPart(commitmentText));
          conversationId=r.invocation_id;audit.conversationInvocationId=r.invocation_id;
          }
          if(!a){
            audit.stage='awaiting_workspace_permission';publish();
            progress.report(new vscode.LanguageModelTextPart('Choose **Sutra workspace permissions** to continue this request.\n\n'));
            if(!await beginArm({audit,token:activeCancel.token})){
              audit.stage='permission_declined';progress.report(new vscode.LanguageModelTextPart('Workspace access was not granted. No new workspace operation was dispatched. You can keep chatting.'));return;
            }
            a=armed;audit.stage='workspace';publish();
            progress.report(new vscode.LanguageModelTextPart('**Workspace permissions saved.** Continuing your request.\n\n'));
          }else{
            audit.workspaceAccess={state:'reused',workspace:a.workspace,profile:a.profile};publish();
          }

        }
        assertCurrent(a,activeCancel.token);
        assertArmed();
        if(actionBinding){
          const current=await deps.request({op:'state'},activeCancel.token);
          assertCurrent(a,activeCancel.token);
          if(!matchesTask(current,actionBinding))
            throw new Error('Task or calibration changed after the action. Its receipt is retained.');
        }
        if(restartReview)flow=await flowEvent(flow,'restart_review',tier,activeCancel.token);
        if(!flow)flow=await flowEvent(null,'start_existing',tier,activeCancel.token,{prompt,conversation_id:conversationId});
        if(flow.state==='PAUSED')flow=await flowEvent(flow,'resume',tier,activeCancel.token);
        while(!['DONE','AWAITING_ACTION'].includes(flow.state)){
          assertCurrent(a,activeCancel.token);
          if(flow.recovery){audit.stage='recovery_required';audit.message=flow.recovery.message;publish();progress.report(new vscode.LanguageModelTextPart(flow.recovery.message+'\n\nSend **continue** to review the recovery options.'));return;}
          if(flow.state==='PAUSED'){
            showPause(flow,audit,progress);
            return;
          }
          if(['QUARANTINED','CANCELLING','EXECUTING_ACTION'].includes(flow.state))throw new Error('Sutra workflow '+flow.state+'. Saved request retained. Inspect Sutra Monitor, then choose Resume or Discard.');
          let event='step',extra={};
          if(flow.state==='NEED_TOOL_APPROVAL'&&!flow.capability_settled){
            const authorization=await deps.approveCapability(flow.capability_proposal,activeCancel.token);
            assertCurrent(a,activeCancel.token);
            if(!authorization){audit.stage='permission_declined';progress.report(new vscode.LanguageModelTextPart('Tool permission was not granted. The saved request can be resumed or discarded.'));return;}
            event='capability_grant';extra={authorization};
          }
          if(flow.state==='NEED_PERMISSION'){
            if(!await allowActionProfile()){
              audit.stage='permission_declined';
              return;
            }
            const grant=await approvePermission(flow.permission,activeCancel.token);
            assertCurrent(a,activeCancel.token);
            if(!grant){audit.stage='permission_declined';progress.report(new vscode.LanguageModelTextPart('Permission was not granted. No edit or test ran.'));return;}
            event='grant';extra={grant};
          }
          flow=await flowEvent(flow,event,tier,activeCancel.token,extra);
          const bound=flow.binding||flow.bound_task;
          if(bound)audit.session=bound.session;
          audit.orchestration={flowId:flow.flow_id,state:flow.state,children:flow.children};publish();
          if(flow.progress)progress.report(new vscode.LanguageModelTextPart('Host workflow · '+flow.progress+'\n\n'));
          if(flow.child)recordUsage(flow.child.result);
        }
        assertCurrent(a,activeCancel.token);
        if(flow.capability_result){audit.stage='tool_finished';progress.report(new vscode.LanguageModelTextPart(deps.capabilityReport(flow.capability_result)));return;}
        data=flow.data;
        if(!data){progress.report(new vscode.LanguageModelTextPart(flow.state==='AWAITING_ACTION'?'The saved proposal is awaiting action confirmation. Ask for a fresh proposal if its confirmation is no longer available.':'This workflow has finished. Send a new request to continue.'));return;}
        if(data?.notice){audit.stage='guidance';progress.report(new vscode.LanguageModelTextPart(data.notice.message+'\n\nSutra setup guidance — no model invocation.'));return;}
        if(!data?.result)throw new Error(data?.notice?.message||'No governed response available.');
        const reviewLinks=deps.publishReview(data);lastRoute=data.route?.model||data.result.model||'';publish();
        const assessmentText=observationText(flow.task_observations,true);
        const r=data.result;recordUsage(r);audit.invocationId=r.invocation_id;audit.model=lastRoute;audit.stage=r.admission.admitted?'admitted':'withheld';publish();if(!r.admission.admitted){if(assessmentText)progress.report(new vscode.LanguageModelTextPart(assessmentText));progress.report(new vscode.LanguageModelTextPart('Sutra withheld the response: '+r.admission.errors.join('; ')));return;}
        const p=r.accepted_output;if(options.toolMode===vscode.LanguageModelChatToolMode.Required&&!['edit','run_tests'].includes(p.action?.kind))throw new Error('No admitted action is available to satisfy required tool mode.');
        if(data.report_markdown){
          progress.report(new vscode.LanguageModelTextPart(data.report_markdown+'\n\n'));
          if(data.review_diagram){try{await deps.presentReview?.(data);progress.report(new vscode.LanguageModelTextPart('The **source dependency diagram** is open beside this chat. Its arrows show local import candidates, not execution order.\n\n'));}catch(error){progress.report(new vscode.LanguageModelTextPart('Diagram preview unavailable: '+error.message+'\n\n'));}}
          if(reviewLinks?.reportUri)progress.report(new vscode.LanguageModelTextPart(`[Review details and source citations](${reviewLinks.reportUri}) · Reviewed through Sutra with **${lastRoute}**.\n\n`));
        }else progress.report(new vscode.LanguageModelTextPart(`Sutra routing confirmed · ${lastRoute} · task ${flow.binding?.session}\nRequest ${audit.id} · protocol invocation ${r.invocation_id}\n\n${p.answer||p.summary}\n\n`));
        if(assessmentText)progress.report(new vscode.LanguageModelTextPart(assessmentText));
        const evidence=data.coverage?.evidence;
        if(data.coverage?.deferred_files>0)progress.report(new vscode.LanguageModelTextPart(`Source paging: ${data.coverage.fresh_selected_files} fresh files selected; ${data.coverage.deferred_files} mapped files deferred to stay within the current allowance. This response covers the selected evidence.\n\n`));
        if(evidence)progress.report(new vscode.LanguageModelTextPart(`Evidence delivered: ${evidence.fresh_snapshot_files} freshly mapped files; source excerpts from ${evidence.excerpt_files.length} files; ${evidence.child_reports} child reports. This is a bounded review, not an exhaustive semantic audit.\n\n`));
        if(['edit','run_tests'].includes(p.action?.kind)){
          if(!canAct){progress.report(new vscode.LanguageModelTextPart('The proposal is retained. Enable the Sutra action tool in native Agent chat to review and apply it. No action executed.'));return;}
          if(!await allowActionProfile())return;
          assertCurrent(a,activeCancel.token);
          const binding=flow.binding;
          const preview=await deps.request({op:'native_preview',...binding,invocation_id:r.invocation_id},activeCancel.token);
          assertCurrent(a,activeCancel.token);
          const id=crypto.randomUUID(),callId=crypto.randomUUID();
          tickets.set(id,{id,binding,generation:a.generation,expires:Date.now()+300000,invocation_id:r.invocation_id,kind:p.action.kind,preview,prompt,conversationId,callId});
          if(preview.diff)await deps.showDiff(preview,r.invocation_id);
          try{assertCurrent(a,activeCancel.token);}catch(error){tickets.delete(id);throw error;}
          progress.report(new vscode.LanguageModelToolCallPart(callId,ACTION,{ticket:id}));
        }else if(!p.report){progress.report(new vscode.LanguageModelTextPart('Remaining: '+(p.remaining?.join('; ')||'No further step proposed.')+'\n\nModel notes are not proof of correctness. Test receipts report process observations.'));}
      }finally{
        if(responseUsage.calls.length||footerId){
          const detail=tokens.compact(responseUsage),label='Sutra · '+detail+' · conversation details';
          const footer=footerId?`[${label}](karma-preview:/conversation/${footerId}/Response.md?checkpoint=${footerSignature})`:'Sutra · '+detail;
          if(!activeCancel.token.isCancellationRequested)progress.report(new vscode.LanguageModelTextPart('\n\n---\n'+footer+(footerId?(audit.memoryUsed?' · Memory restored':' · New conversation'):'')+' · Open tokens, system and kernel status with **Sutra Monitor** in the status bar.\n'));
        }
        const stopped=activeCancel.token.isCancellationRequested;
        activityOpen=false;activityProgress=undefined;cancellation.dispose();activeCancel.dispose();activeCancel=undefined;running=false;publish();
        if(stopped){try{await rememberFlow(await deps.request({op:'orchestrate',event:'pending'}));}catch{} }
        if(stopped&&context.workspaceState.get(pendingKey))void vscode.window.showInformationMessage('Sutra stopped. The saved request, context and consumed budget are retained. Resume or Discard from Sutra Monitor.');
      }
      });
    }
  };
  context.subscriptions.push(changed,vscode.lm.registerLanguageModelChatProvider('karma',provider),vscode.lm.registerTool(ACTION,tool),
    vscode.commands.registerCommand('karma.useNativeChat',()=>useNativeChat()),
    vscode.commands.registerCommand('karma.resumeWorkflow',()=>recoverSaved()),vscode.commands.registerCommand('karma.discardWorkflow',()=>recoverSaved(true)),
    vscode.commands.registerCommand('karma.increaseAllocation',()=>increaseAllocation().catch(error=>vscode.window.showErrorMessage(error.message))),
    vscode.commands.registerCommand('karma.armNative',()=>beginArm()),vscode.commands.registerCommand('karma.disarmNative',()=>revoke()),
    vscode.commands.registerCommand('karma.toggleNative',async()=>{clearAccess('Native chat setting changed.');await vscode.workspace.getConfiguration('karma').update('nativeEnabled',!enabled(),vscode.ConfigurationTarget.Global);changed.fire();publish();}));
  const listener=vscode.workspace.onDidChangeConfiguration?.(e=>{if(e.affectsConfiguration('karma.nativeEnabled')){clearAccess('Native chat setting changed.');changed.fire();}else if(e.affectsConfiguration('karma.models'))changed.fire();if(e.affectsConfiguration('chat'))publish();});if(listener)context.subscriptions.push(listener);
  // Populate VS Code's model registry at activation, including after reload.
  // Discovery reads this provider's metadata only; it never starts inference.
  if(vscode.lm.selectChatModels)Promise.resolve(vscode.lm.selectChatModels({vendor:'karma'})).catch(error=>{reason='Native model discovery: '+error.message;publish();});
  async function withWorkspaceAccess(token,invoke){
    const access=assertArmed();
    const source=new vscode.CancellationTokenSource();
    const validate=()=>{
      if(refreshAccess()!==access || token?.isCancellationRequested)source.cancel();
    };
    const changes=authorization.onDidChange?.(validate);
    const folders=vscode.workspace.onDidChangeWorkspaceFolders?.(validate);
    const settings=vscode.workspace.onDidChangeConfiguration?.(validate);
    const cancel=token?.onCancellationRequested(validate);
    try{
      validate();
      assertCurrent(access,source.token);
      const result=await invoke(source.token);
      assertCurrent(access,source.token);
      return result;
    }finally{
      for(const subscription of [changes,folders,settings,cancel])subscription?.dispose();
      source.dispose();
    }
  }
  const subscriptions=[
    authorization.onDidChange?.(publish),
    vscode.workspace.onDidChangeWorkspaceFolders?.(publish),
    vscode.workspace.onDidGrantWorkspaceTrust?.(publish),
  ].filter(Boolean);
  publish();
  return {status,publish,withWorkspaceAccess,dispose(){
    for(const subscription of subscriptions)subscription.dispose();
    clearAccess('Extension stopped.');
  }};
}
module.exports={install,latestRequest,observationText};

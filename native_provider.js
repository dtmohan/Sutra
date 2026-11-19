'use strict';
const crypto=require('node:crypto'),path=require('node:path');
const tokens=require('./media/token_usage');
const {NativeMemory,isCompaction}=require('./native_memory');
const hostTokens=require('./host_tokens');
const {profiles}=require('./workspace_authorization');
const {chatActivity}=require('./model_activity');
const {ModelUnavailable,fallbackMessage}=require('./model_selection');
const ACTION='karma_native_action';
const THINK_NEXT='sutraThinkingForNextRequest';
const MODEL_IDS={auto:'karma-auto',low:'karma-low',mid:'karma-mid',high:'karma-high'};
const CHAT_DEFAULTS={'defaultModel':MODEL_IDS.auto,'editor.preferCopilotHarness':false,'defaultToCopilotHarness':false,'editor.localAgent.enabled':true};
const MANIFEST=require('./package.json');
const EXTENSION_ID=MANIFEST.publisher+'.'+MANIFEST.name;
const RESUME_PROMPT=/^Resume saved Sutra request \[([a-f0-9]{32})\]$/;
const classifying=flow=>flow?.state==='CLASSIFYING'||flow?.stop?.resume_phase==='CLASSIFYING';
const evidencePause=flow=>flow?.state==='PAUSED'&&flow.stop?.status==='evidence_bridge';
const documentFilePause=flow=>evidencePause(flow)&&flow.stop.evidence_bridge?.target_kind==='document'&&
  (flow.stop.evidence_bridge.required==='document_file'||/\.docx\b/i.test(flow.prompt||''));
const DOCUMENT_EVIDENCE=/^\s*(?:document|r[eé]sum[eé]|cv|cover[ -]+letter)[ \t]+text:[ \t]*\r?\n\s*\S/i;
const WORKSPACE_EVIDENCE=/^(?:use this workspace[.!?]*|workspace files:[ \t]*\r?\n\s*\S[\s\S]*)$/i;
function latestRequest(vscode,messages){
  const last=messages.at(-1);
  const results=last?.content?.filter(p=>p instanceof vscode.LanguageModelToolResultPart)||[];
  if(results.length)return {results};
  const users=[...messages].reverse().filter(m=>m.role===vscode.LanguageModelChatMessageRole.User&&m.content.some(p=>p instanceof vscode.LanguageModelTextPart));
  const input=message=>{
    const value=message?.content.filter(p=>p instanceof vscode.LanguageModelTextPart).map(p=>p.value).join('\n')||'';
    return value.match(/<userRequest>([\s\S]*?)<\/userRequest>/)?.[1]??value;
  };
  const prompt=input(users[0]),priorPrompt=input(users[1]);
  if(!prompt.trim()||prompt.length>1800)throw new Error('Sutra needs a text request of 1–1800 characters. For a document review, paste one relevant section at a time.');
  const previous=[...messages].reverse().filter(m=>m.role===vscode.LanguageModelChatMessageRole.Assistant);
  const parent=previous.map(m=>m.content.filter(p=>p instanceof vscode.LanguageModelTextPart).map(p=>p.value).join('')).map(text=>{const matches=[...text.matchAll(/\[(?:Karma|Sutra)[^\n]*\]\(karma-preview:\/conversation\/([a-f0-9]{32})\/Response\.md\)/g)];return matches.at(-1)?.[1]||text.match(/^(?:Karma|Sutra) conversation receipt: ([a-f0-9]{32})$/m)?.[1];}).find(Boolean);
  return {prompt,parent,priorPrompt:priorPrompt.length<=1800?priorPrompt:undefined};
}
function conversationMarkdown(output){
  const answer=output.answer;
  if(typeof output.basis!=='string'||!output.basis.trim())return answer;
  const basis=output.basis.trim().replace(/\s+/g,' ').replace(/[\\`*_{}\[\]()<>#|!]/g,'\\$&');
  return answer+'\n\n**Basis for the verdict**\n\n'+basis;
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

  let allocationPending=false,thinkingForNextRequest=false,resumeSubmission;
  let armPromise,accessEpoch=0,armingStep='',armed,activeCancel,arming=false,running=false,selecting=false,lastRoute='',routing,reason='Open Sutra chat to talk. Choose workspace access on first use.';
  const memory=new NativeMemory(context);let lastCompaction;
  const tickets=new Map(),completed=new Map(),changed=new vscode.EventEmitter();
  const enabled=()=>vscode.workspace.getConfiguration('karma').get('nativeEnabled',true);
  const authorization=deps.authorization;
  function setNextThinking(value){
    thinkingForNextRequest=value;
    return vscode.commands.executeCommand('setContext',THINK_NEXT,value);
  }
  function toggleNextThinking(){
    if(!enabled())return;
    const update=setNextThinking(!thinkingForNextRequest);
    void vscode.window.showInformationMessage(thinkingForNextRequest?
      'Thinking is on for your next Sutra request.':'The next Sutra request will use your saved reasoning setting.');
    return update;
  }
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
      clearAccess('Workspace access changed. The saved request and budget are retained.');
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
    const label=!enabled()?'Sutra: off':arming?'Sutra: awaiting workspace access':selecting?'Sutra: opening chat':live?'Sutra: request received':running?'Sutra: action running':
      'Sutra · '+(access?profiles[access.profile]:'chat only')+' · routing unverified';
    const explanation=arming?armingStep:selecting?'Opening Local chat with Sutra Auto. No prompt is being sent.':live?'This request reached Sutra. Protocol admission is checked before model dispatch.':
      'No Sutra request is in flight. The current chat selection is unverified; check for Sutra Auto beneath the prompt. Direct Ollama and other chats bypass Sutra.';
    const pending=context.workspaceState.get(pendingKey)||null;
    const permissionPending=!!pending?.needsWorkspaceAccess&&pending.workspace===deps.workspace()&&
      (!access||pending.requestedAccessProfile==='workspace_agent'&&access.profile!=='workspace_agent');
    return {enabled:enabled(),arming,armingStep,selecting,armed:!!access,accessProfile:access?.profile||null,
      permissionPending,permissionCanChoose:permissionPending&&!arming&&!selecting&&!running&&!live&&!deps.isBusy(),
      working:allocationPending||running||live,session:routing?.session,scope:'Sutra native chat and its workspace tools',defaultModel:chat.get('defaultModel',''),defaultsReady,reason,lastRoute,label,explanation,routing:routing?{...routing}:null,compaction:lastCompaction,pending,openTaskCount:context.workspaceState.get('sutra.openTaskCount')};
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
    if(!access)throw new Error('Workspace access is required. Choose Sutra: Workspace Access…');
    return access;
  }
  function assertCurrent(access,token){
    if(refreshAccess()!==access || !enabled() || token?.isCancellationRequested)
      throw new Error('Sutra request cancelled or workspace permission changed.');
  }
  async function check(t,token){
    const st=await deps.request({op:'state',...t.flowScope},token);
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
    if(arming||selecting||deps.isBusy()||((running||routing?.active)&&!ownsRequest()))throw new Error('Wait for the active Sutra request before changing workspace access.');
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
    armingStep='Choose workspace access in the dialog.';
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
      const choice=await vscode.window.showWarningMessage('Workspace access for Sutra',{modal:true,detail:
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
  const stackKey='sutra.taskStackChat';
  const flowScope=flow=>flow?.stackChat?{chat_key:flow.stackChat,flow_id:flow.flow_id}:{};
  const allocationReasons={
    workspace_limit:'The workspace’s automatic allocation ceiling cannot cover the next step.',
    policy_disabled:'Automatic allocation is disabled or unavailable in this workspace.',
    retry_limit:'Sutra reached its automatic allocation retry limit for this task.',
    no_progress:'The previous increase did not unblock this step.',
    already_amended:'This stop already received an automatic increase.',
    pending_execution:'Unfinished work must settle before Sutra can expand the allocation.',
    another_budget_dimension:'Another allocation limit still blocks the next step.',
    task_binding:'The saved task binding does not permit an automatic increase.'
  };
  function allocationMessage(automatic){
    if(!automatic)return undefined;
    return automatic.message||
      (Object.hasOwn(allocationReasons,automatic.reason)?allocationReasons[automatic.reason]:automatic.reason);
  }
  const resumeUri=id=>`${vscode.env?.uriScheme||'vscode'}://${EXTENSION_ID}/resume/${id}`;
  const accessUri=id=>`${vscode.env?.uriScheme||'vscode'}://${EXTENSION_ID}/access/${id}`;
  async function chooseWorkspaceAccess(link){
    const pending=context.workspaceState.get(pendingKey);
    if(link&&(!pending||pending.workspace!==deps.workspace()||pending.resumeLink!==link))
      throw new Error('This workspace access action is stale. Use the current request in Sutra Monitor.');
    const granted=await beginArm(undefined,pending?.requestedAccessProfile==='workspace_agent');
    if(pending&&context.workspaceState.get(pendingKey)?.flow_id===pending.flow_id){
      const message=granted?'Workspace access saved. Return to the original Sutra chat and send continue.':
        'Workspace access was not granted. The request, context and consumed budget are saved.';
      reason=message;publish();
      void vscode.window.showInformationMessage(message);
    }
    return granted;
  }
  async function requestWorkspaceAccess(flow,audit,progress,agentOnly=false){
    const pending=context.workspaceState.get(pendingKey);
    if(!pending||pending.flow_id!==flow.flow_id)throw new Error('The saved request changed before workspace access could be requested.');
    await context.workspaceState.update(pendingKey,{...pending,needsWorkspaceAccess:true,
      requestedAccessProfile:agentOnly?'workspace_agent':undefined});
    audit.stage='awaiting_workspace_permission';audit.message='Workspace access is required; no model call is waiting for consent.';publish();
    const action=vscode.window.registerUriHandler
      ?'[Choose workspace access]('+accessUri(pending.resumeLink)+')':'Choose **Workspace access** in Sutra Monitor.';
    progress.report(new vscode.LanguageModelTextPart('**Workspace access needed**\n\n'+action+
      (agentOnly?' Scoped edits and tests need Workspace agent access.':' No workspace files have been read.')+
      '\n\nYour request, context and consumed budget are saved. After choosing access, send **continue** in this chat.'));
  }
  function resumeAction(flow){
    const pending=context.workspaceState.get(pendingKey);
    return vscode.window.registerUriHandler&&pending?.flow_id===flow.flow_id&&pending.resumeLink
      ?'[Resume this request]('+resumeUri(pending.resumeLink)+') or send **continue** in this chat.'
      :'Send **continue** in this Sutra chat or choose **Resume** in Sutra Monitor.';
  }
  async function rememberFlow(flow,thinkForTurn,chatKey,stackChat){
    if(flow.task_stack){
      await context.workspaceState.update(stackKey,{chat:stackChat,workspace:deps.workspace(),nativeChat:chatKey});
      await context.workspaceState.update('sutra.openTaskCount',flow.task_stack.open_tasks.length);
      const focused=flow.focused_flow;
      if(!focused){
        if(context.workspaceState.get(pendingKey)?.stackChat===stackChat)await context.workspaceState.update(pendingKey,undefined);
        publish();return;
      }
      flow={...focused,stackChat};
    }
    const terminal=flow.state==='DONE'||flow.state==='CANCELLED';
    const saved=context.workspaceState.get(pendingKey);
    const prior=saved?.flow_id===flow.flow_id?saved:undefined;
    const resumeThinking=classifying(flow)&&(thinkForTurn??(saved?.flow_id===flow.flow_id&&saved.resumeThinking===true));
    const automatic=flow.stop?.automatic_allocation;
    const stop=automatic?{...flow.stop,automatic_allocation:{...automatic,message:allocationMessage(automatic)}}:flow.stop;
    // Finishing a conversational turn must not erase another saved review.
    if(!terminal||saved?.flow_id===flow.flow_id)
      await context.workspaceState.update(pendingKey,terminal?undefined:{...flow,stop,resumeThinking,workspace:deps.workspace(),
        needsWorkspaceAccess:prior?.needsWorkspaceAccess,requestedAccessProfile:prior?.requestedAccessProfile,
        nativeChat:prior?.nativeChat||chatKey,resumeLink:prior?.resumeLink||crypto.randomBytes(16).toString('hex'),
        conversation:undefined,routing_step:undefined,child:undefined,data:undefined,mapping:undefined,claim_review_usage:undefined,allocation_proposal:undefined,allocation_amendment:undefined});
    publish();
  }
  async function openTasks(){
    if(allocationPending||running||routing?.active||deps.isBusy())throw new Error('Wait for the current Sutra operation to finish.');
    const stack=context.workspaceState.get(stackKey);
    if(!stack?.chat||stack.workspace!==deps.workspace()){
      void vscode.window.showInformationMessage('No open tasks in the current Sutra chat.');
      return;
    }
    const request={op:'orchestrate',chat_key:stack.chat};
    const list=await deps.request({...request,event:'task_list'});
    await rememberFlow(list,undefined,stack.nativeChat,stack.chat);
    const rows=list.task_stack.open_tasks;
    if(!rows.length){void vscode.window.showInformationMessage('No open tasks in the current Sutra chat.');return;}
    const item=await vscode.window.showQuickPick(rows.map(row=>({
      label:row.prompt.replace(/\s+/g,' ').trim(),description:row.focused?'Current request':'Parked request',
      detail:row.state+' · '+row.flow_id.slice(0,12),row
    })),{title:'Open Sutra tasks for the last active chat',placeHolder:'Choose a task to focus, resume, or close'});
    if(!item)return;
    const action=await vscode.window.showQuickPick([
      {label:'Focus task',detail:'Select the task for your next instruction.'},
      {label:'Resume task',detail:'Continue the task from its saved checkpoint.'},
      {label:'Close task',detail:'End the task and retain its history and spending.'}
    ],{title:item.label});
    if(!action)return;
    if(stack.workspace!==deps.workspace()||allocationPending||running||routing?.active||deps.isBusy())
      throw new Error('The workspace or current request changed. Open the task list again.');
    const next=await deps.request({...request,event:action.label==='Close task'?'task_close':'task_focus',
      flow_id:item.row.flow_id,cursor:item.row.cursor,stack_cursor:list.task_stack.cursor});
    await rememberFlow(next,undefined,stack.nativeChat,stack.chat);
    if(action.label==='Resume task')await recoverSaved();
  }
  async function recoverSaved(discard=false,link){
    if(!discard&&!enabled())throw new Error('Enable Sutra chat before resuming a request.');
    if(allocationPending||running||routing?.active||deps.isBusy())throw new Error('Stop the active request and wait for cleanup before changing the saved workflow.');
    const pending=context.workspaceState.get(pendingKey);
    if(!pending||pending.workspace!==deps.workspace()||link&&pending.resumeLink!==link){
      void vscode.window.showInformationMessage(link?'This Sutra resume link no longer matches the saved request in this workspace.':'No saved workflow in this workspace.');
      return;
    }
    const sameRequest=()=>deps.workspace()===pending.workspace&&context.workspaceState.get(pendingKey)?.flow_id===pending.flow_id;
    if(discard){
      const yes=await vscode.window.showWarningMessage('Discard the saved Sutra request?',{modal:true,detail:'Consumed budgets, receipts and task permissions are retained. Unused child reservations are released. Executed actions are not undone.'},'Discard request');
      if(yes!=='Discard request')return;
      const current=await deps.request({op:'orchestrate',event:'inspect',flow_id:pending.flow_id,...flowScope(pending)});
      if(!sameRequest()||running||routing?.active)throw new Error('The saved request changed. Review the current request in Sutra Monitor.');
      await rememberFlow(['DONE','CANCELLED'].includes(current.state)?current:await deps.request({op:'orchestrate',event:'cancel',flow_id:current.flow_id,cursor:current.cursor,...flowScope(pending)}),
        undefined,pending.nativeChat,pending.stackChat);
      return;
    }
    if(resumeSubmission?.expires>Date.now())return;
    const submission={id:crypto.randomBytes(16).toString('hex'),workspace:pending.workspace,
      flowId:pending.flow_id,stackChat:pending.stackChat,expires:Date.now()+30_000};
    resumeSubmission=submission;
    try{
      const current=await deps.request({op:'orchestrate',event:'inspect',flow_id:pending.flow_id,...flowScope(pending)});
      if(!sameRequest()||running||routing?.active||deps.isBusy())throw new Error('The saved request changed. Review the current request in Sutra Monitor.');
        await rememberFlow(current,undefined,pending.nativeChat,pending.stackChat);
      if(['DONE','CANCELLED'].includes(current.state)){
        resumeSubmission=undefined;
        void vscode.window.showInformationMessage('This Sutra request has finished.');
        return;
      }
      submission.cursor=current.cursor;
      // The generic Open Chat command cannot target a session in this host.
      // Never open a potentially unrelated/cloud chat or submit into it.
      // An explicit copied instruction is checked against the original axis
      // and inspected cursor when the user submits it in that conversation.
      submission.expires=Date.now()+5*60_000;
      const instruction=`Resume saved Sutra request [${submission.id}]`;
      const choice=await vscode.window.showInformationMessage(
        'Saved request: '+String(pending.prompt||'Sutra request').replace(/\s+/g,' ').slice(0,100)+
        '. Return to its original Local Sutra chat and send continue. This VS Code interface cannot address that chat directly.',
        'Copy resume instruction');
      if(choice==='Copy resume instruction'){
        if(!sameRequest()||resumeSubmission!==submission)throw new Error('The saved request changed before copying.');
        if(!vscode.env?.clipboard?.writeText)throw new Error('Clipboard access is unavailable. Send continue in the original Sutra chat.');
        await vscode.env.clipboard.writeText(instruction);
      }else if(resumeSubmission===submission){
        // Dismissing guidance did not deliver a continuation token. Let the
        // next explicit Resume action offer the notice again immediately.
        resumeSubmission=undefined;
      }
    }catch(error){
      if(resumeSubmission===submission)resumeSubmission=undefined;
      throw error;
    }
  }
  async function increaseAllocation(){
    if(allocationPending||running||routing?.active||deps.isBusy())throw new Error('Wait for the current Sutra operation before changing allocation.');
    const pending=context.workspaceState.get(pendingKey),workspace=deps.workspace();
    if(!pending||pending.workspace!==workspace)throw new Error('No saved workflow in this workspace.');
    allocationPending=true;publish();
    try{
      const scope=flowScope(pending);
      const current=await deps.request({op:'orchestrate',event:'inspect',flow_id:pending.flow_id,...scope});
      const preview=await deps.request({op:'orchestrate',event:'allocation_preview',flow_id:current.flow_id,cursor:current.cursor,...scope});
      const p=preview.allocation_proposal;
      const labels={tokens:'Model budget units',calls:'Model calls',operations:'Repository operations',source_bytes:'Source bytes',seconds:'Execution seconds'};
      const fmt=x=>Number(x).toLocaleString(undefined,{maximumFractionDigits:1});
      const rows=Object.entries(p.additions).map(([key,add])=>labels[key]+': '+fmt(p.before[key])+' + '+fmt(add)+' = '+fmt(p.after[key])+' total. Spent '+fmt(p.used[key])+'; '+fmt(p.remaining_after[key])+' remains.');
      const target = p.child_allocation
        ? 'This adds capacity to the saved child and its parent execution. Its checkpoint, context, permissions and recorded spending are retained.'
        : 'This adds capacity to the task. A new bounded execution can use the added capacity. Context, constraints, permissions and spending are retained.';
      const yes=await vscode.window.showWarningMessage('Increase this task’s allocation?',{modal:true,detail:
        p.goal+'\n\n'+rows.join('\n')+'\n\n'+target+' Model context and memory limits still apply. Continue the saved request after approval.'},'Increase allocation');
      if(yes!=='Increase allocation')return;
      if(deps.workspace()!==workspace||running||routing?.active||deps.isBusy())throw new Error('The workspace or request changed. Review a fresh allocation proposal.');
      const next=await deps.request({op:'orchestrate',event:'allocation_grant',flow_id:preview.flow_id,cursor:preview.cursor,confirmed:true,proposal:p,...scope});
      await rememberFlow(next,undefined,pending.nativeChat,pending.stackChat);
      void vscode.window.showInformationMessage('Allocation increased (version '+next.allocation_amendment.version+'). Choose Resume or send continue in your Sutra chat.');
    }finally{allocationPending=false;publish();}
  }
  let activityProgress;
  async function flowEvent(flow,event,tier,token,extra,thinkForTurn,chatKey,stackChat,onFallback){
    const access=refreshAccess();
    const models=['inspect','pending','cancel','locate_document','approve_document'].includes(event)?undefined:await deps.configureModels(false,token,{tier,onFallback});
    const workspaceStep=['step','resume','grant','capability_grant','restart_review','accept_provenance'].includes(event) && !classifying(flow);
    if(workspaceStep){
      assertCurrent(access,token);
      assertArmed();
    }
    const binding=flow?.binding||flow?.bound_task;
    const next=await deps.request({op:'orchestrate',event,flow_id:flow?.flow_id,cursor:flow?.cursor,
      ...(thinkForTurn&&['start','step'].includes(event)?{think_for_turn:true}:{}),
      ...(flow?.state==='CLASSIFYING'&&event==='step'?{conversation_step:true}:{}),
      armed:!!access,binding,models,tier,...(stackChat?{chat_key:stackChat}:{}),...extra},token,activityProgress);
    await rememberFlow(next,event==='pending'?undefined:thinkForTurn,event==='pending'&&!next.task_stack?undefined:chatKey,stackChat);return next;
  }
  function showHostProgress(flow, progress){
    if(flow.progress){
      if(routing){routing.progress=flow.progress;publish();}
    }
  }
  function showPause(flow,audit,progress){
    const stop=flow.stop||{},failures=stop.failures||[];
    if(evidencePause(flow)){
      const reason=stop.error||'Supply the evidence requested for this task.';
      audit.stage='paused';audit.message=reason;publish();
      const pending=context.workspaceState.get(pendingKey);
      const link=vscode.window.registerUriHandler&&pending?.flow_id===flow.flow_id&&pending.resumeLink
        ? '\n\n[Saved evidence request]('+resumeUri(pending.resumeLink)+')' : '';
      const reply=stop.evidence_bridge?.target_kind==='document'?'Begin a reply in this chat with `Document text:`.':'Reply in this chat to continue.';
      const guidance=stop.evidence_bridge?.required==='document_file'
        ?'Send **continue** to try the file lookup again, or paste an excerpt under `Document text:`.':reply;
      progress.report(new vscode.LanguageModelTextPart('**Evidence needed**\n\n'+reason+'\n\n'+guidance+' Your remaining budget is saved.'+link));
      return;
    }
    const details=[stop.error,...failures.map(f=>f.path+': '+f.error)].filter(Boolean);
    if(stop.status==='resource_wait'){
      const stage={CLASSIFYING:'Conversation routing',MAPPING:'Source mapping',CHILDREN:'Child review',SYNTHESIS:'Workspace synthesis',CODING:'Workspace coding'}[stop.resume_phase]||'Workflow';
      const blocker=stop.blocker||stop.measurement?.blocker;
      const label={runtime_busy:'Runtime is busy',runtime_timeout:'Runtime readiness timed out',runtime_uncertain:'Runtime ownership needs inspection',
        runtime_starting:'Runtime is starting',model_admission:'Model allocation blocked',measurement_unavailable:'Fresh measurement unavailable',
        hardware_pressure:'Hardware pressure'}[blocker]||'Host admission check';
      details.unshift(label+' · '+stage+' paused.');
      const observed=stop.measurement?.observed_at||stop.measurement?.sample?.wall_time;
      if(Number.isFinite(observed))details.push('Blocking observation: '+new Date(observed*1000).toLocaleString()+'. Monitor readings show the current state and can differ.');
    }
    const allocationStop=['context_budget','task_budget','budget_stopped'].includes(stop.status)||stop.status==='memory_fault'&&stop.memory_fault?.code==='capacity';
    if(allocationStop)details.unshift('Host allocation check paused the workflow before the next model call. No reasoning stream was generated for that call.');
    const coverage=Number.isInteger(stop.mapped_files)&&Number.isInteger(stop.source_files)?` Mapped ${stop.mapped_files} of ${stop.source_files} source files.`:'';
    if(stop.recovery)details.push('Runtime memory recovery: '+(stop.recovery.error||stop.recovery.action?.replaceAll('_',' ')||'observed')+'. Task allocation is unchanged.');
    if(stop.memory_fault)details.push('Memory recovery: '+stop.memory_fault.recovery.replaceAll('_',' ')+'. Exact saved pages and task grants are retained.');
    const automatic=stop.automatic_allocation;
    const autoMessage=allocationMessage(automatic);
    if(autoMessage&&!details.includes(autoMessage))details.push(autoMessage);
    const reason=details.length?details.join('\n'):(stop.status||'Inspect Sutra Monitor for details.');
    audit.stage='paused';audit.message=reason;publish();
    let guidance='Resolve the reported cause before resuming.';
    if(stop.status==='resource_wait'){
      const blocker=stop.blocker||stop.measurement?.blocker;
      guidance=blocker==='runtime_busy'?'Wait for the active runtime operation to finish, then continue here. No model switch or memory release was attempted.':
        ['runtime_timeout','runtime_uncertain','runtime_starting'].includes(blocker)?'Refresh the engine worker status in Sutra model settings. Readiness or ownership must be verified before retrying.':
        blocker==='measurement_unavailable'?'Refresh the engine and telemetry status. A fresh admission measurement is required; this does not establish memory or thermal pressure.':
        'Check the saved blocking observation and **Last model allocation** in Sutra Monitor. Resume checks fresh measurements; healthy current pressure alone does not establish that this model fits.';
    }else if(stop.status==='child_slice_timeout'){
      const checkpoint=stop.slice_ordinal>1?'Resume from the last completed child checkpoint.':'Resume to retry the first child slice.';
      guidance=checkpoint+' You can select a different Sutra model tier before resuming.';
    }else if(stop.status==='claim_review_output_limit'){
      const page=Number.isInteger(stop.review_page)&&Number.isInteger(stop.total_pages)
        ? `claim-review page ${stop.review_page} of ${stop.total_pages}` : 'the unfinished claim-review page';
      guidance='The draft and completed claim-review pages are saved. Sutra will request thinking off when it retries '+page+'.';
    }else if(stop.child_allocation_granted){
      guidance='Child capacity was increased. Resume the saved checkpoint in this chat.';
    }else if(stop.resume_phase==='CHILDREN'&&stop.memory_fault?.code==='capacity'){
      guidance=stop.child_context?.task_allocation_need
        ? 'Choose **Increase allocation…** in Sutra Monitor to add the measured child capacity and keep its checkpoint.'
        : 'Select a Sutra model with a larger context window and send **continue**.';
    }else if(stop.resume_phase==='CHILDREN'&&allocationStop){
      guidance='Inspect the child allocation limit in Sutra Monitor. Its last checkpoint and spending are saved.';
    }else if(automatic?.status==='model_context'){
      guidance='Select a Sutra tier with a larger context window and send **continue** to use that tier.';
    }else if(allocationStop){
      guidance='Choose **Increase allocation…** in Sutra Monitor to review and confirm additional task capacity.';
    }
    const title=stop.resume_phase==='CLASSIFYING'?'Sutra paused the request.':'Sutra paused the review.';
    progress.report(new vscode.LanguageModelTextPart('**'+title+'**'+coverage+'\n\n'+reason+'\n\n'+(stop.failure_count>failures.length?`${stop.failure_count-failures.length} more file failures are recorded in the review details.\n\n`:'')+'The request, context, permissions and consumed budget are saved. '+guidance+'\n\n'+resumeAction(flow)));
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
      reason='Local chat and Sutra Auto saved for new sessions. Workspace access are managed separately.';
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
    }catch(error){
      audit.stage=token.isCancellationRequested||/cancel|revoked/i.test(error.message)?'cancelled':'blocked';
      audit.message=error.message;
      if(error instanceof ModelUnavailable||/Ollama|model.*unavailable|model.*discover|No local chat model/i.test(error.message)){
        void vscode.window.showErrorMessage?.(error.message,'Model settings')?.then(choice=>{
          if(choice==='Model settings')return deps.configureModels(true);
        }).catch(()=>{});
      }
      if(error instanceof ModelUnavailable){
        if(!token.isCancellationRequested)progress.report(new vscode.LanguageModelTextPart(error.message));
        return;
      }
      throw error;
    }
    finally{
      audit.active=false;audit.finishedAt=new Date().toISOString();publish();deps.onRoute?.({...audit});
      if(audit.stage==='awaiting_workspace_permission'){
        const pending=context.workspaceState.get(pendingKey),link=pending?.resumeLink;
        // Consent runs outside the model response, so dismissing its dialog
        // cannot be reported by VS Code as an interrupted model generation.
        void Promise.resolve(vscode.window.showInformationMessage('Workspace access is needed for the saved Sutra request.',
          'Choose workspace access')).then(choice=>{
          if(choice==='Choose workspace access'&&context.workspaceState.get(pendingKey)?.resumeLink===link)
            return chooseWorkspaceAccess(link);
        }).catch(error=>vscode.window.showErrorMessage?.(error.message));
      }
    }
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
      try{const access=assertArmed();const data=await deps.request({op:'native_execute',...t.binding,...t.flowScope,invocation_id:t.invocation_id},activeCancel.token);const result={...data.action_receipt,assertions_verified:false};assertCurrent(access,activeCancel.token);completed.set(t.callId,{prompt:t.prompt,conversationId:t.conversationId,result,generation:t.generation,binding:t.binding,thinkForTurn:t.thinkForTurn,flowScope:t.flowScope,nativeChat:t.nativeChat});if(data.task_stack)await rememberFlow(data,undefined,t.nativeChat,t.flowScope.chat_key);deps.publishReview(data);
        return new vscode.LanguageModelToolResult([new vscode.LanguageModelTextPart(JSON.stringify({receipt:result.id,status:result.result.status,target:t.preview.target,returncode:result.result.returncode,assertions_verified:false,note:'A zero exit status is a process observation, not verified test assertions.'}))]);
      }finally{cancel.dispose();activeCancel.dispose();activeCancel=undefined;running=false;publish();}
    }
  };
  const provider={onDidChangeLanguageModelChatInformation:changed.event,
    provideLanguageModelChatInformation(){if(!enabled())return [];const models=deps.modelSettings();return ['auto','low','mid','high'].filter(id=>id==='auto'||models[id]).map(id=>({id:MODEL_IDS[id],name:id==='auto'?'Sutra Auto':'Sutra '+id[0].toUpperCase()+id.slice(1),family:'karma',version:MANIFEST.version,maxInputTokens:64000,maxOutputTokens:4096,detail:id==='auto'?'Governed local routing':models[id]||'Configure this tier',tooltip:'Chat normally with saved workspace access. Every local call uses the invocation gateway.',capabilities:{toolCalling:true,imageInput:false}}));},
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
      const chatKey=memory.chatKey(options);
      if(!request.results){const saved=memory.resolve(vscode,messages,options,request.parent);request.parent=saved.parent;audit.memorySource=saved.source;}
      let prompt=request.prompt,conversationId,actionBinding,actionScope,thinkForTurn=thinkingForNextRequest;
      if(request.results){
        assertArmed();
        if(request.results.length!==1)throw new Error('Unexpected tool results for Sutra.');
        const known=completed.get(request.results[0].callId);
        if(!known||known.generation!==armed.generation)throw new Error('Tool result lacks a current Sutra host receipt.');
        const owner=known.nativeChat||known.flowScope?.chat_key;
        if(chatKey&&owner&&owner!==chatKey)throw new Error('Tool result belongs to another Sutra chat.');
        if(!['applied','process_exited_zero','process_failed'].includes(known.result.result.status)){
          progress.report(new vscode.LanguageModelTextPart('Sutra stopped after '+known.result.result.status+'. Inspect the host action receipt before continuing.'));
          return;
        }
        prompt=known.prompt;conversationId=known.conversationId;actionBinding=known.binding;actionScope=known.flowScope;thinkForTurn=known.thinkForTurn;
      }
      let a=refreshAccess();
      const tier=Object.keys(MODEL_IDS).find(key=>MODEL_IDS[key]===model.id);
      if(!tier)throw new Error('Unknown Sutra tier');
      const tools=options.tools||[],canAct=tools.some(t=>t.name===ACTION);
      if(options.toolMode===vscode.LanguageModelChatToolMode.Required&&!canAct)throw new Error('Required tool mode does not include the Sutra action tool.');
      if(!request.results&&thinkForTurn)void setNextThinking(false);
      let responseUsage={calls:[]},footerId,footerSignature;
      const recordUsage=result=>{responseUsage=tokens.merge(responseUsage,result?.token_usage);audit.token_usage=responseUsage;};
      function recordConversation(chat){
        const result=chat.result;
        recordUsage(result);
        lastRoute=chat.route.model;
        Object.assign(audit,{model:lastRoute,memoryUsed:!!(chat.memory_used||audit.memoryUsed),
          memoryAllocation:chat.memory_allocation,memoryStatus:chat.memory_status,modelCapacity:chat.model_capacity,
          conversationBudget:chat.budget,invocationId:result.invocation_id,
          stage:result.admission.admitted?'conversation':'withheld'});
        publish();
        return result;
      }
      running=true;activeCancel=new vscode.CancellationTokenSource();const cancellation=token.onCancellationRequested(()=>activeCancel?.cancel());publish();
      let savedClassifierThinking;
      const resumeToken=prompt.trim().match(RESUME_PROMPT)?.[1];
      const requestedResume=/^(continue|resume|please continue|please resume)[.! ]*$/i.test(prompt.trim())||!!resumeToken;
      const suppliedKind=!request.results&&(DOCUMENT_EVIDENCE.test(prompt)?'document':WORKSPACE_EVIDENCE.test(prompt.trim())?'workspace':null);
      const evidenceReply=flow=>evidencePause(flow)&&suppliedKind===flow.stop.evidence_bridge?.target_kind;
      const hasFlowLink=flow=>!!flow?.resumeLink&&messages.some(message=>message.role===vscode.LanguageModelChatMessageRole.Assistant&&
        message.content.some(part=>part instanceof vscode.LanguageModelTextPart&&part.value.includes(resumeUri(flow.resumeLink)+')')));
      let stackChat=request.results?actionScope?.chat_key:
        chatKey||(audit.memorySource==='signed_checkpoint'?memory.taskStack(request.parent):undefined);
      if(!stackChat&&resumeToken&&resumeSubmission?.id===resumeToken)stackChat=resumeSubmission.stackChat;
      const cached=context.workspaceState.get(pendingKey);
      if(!stackChat&&cached?.workspace===deps.workspace()&&evidencePause(cached)&&(requestedResume||evidenceReply(cached))&&hasFlowLink(cached))
        stackChat=cached.stackChat;
      const unidentifiedResume=!stackChat&&requestedResume&&!!context.workspaceState.get(pendingKey)?.stackChat;
      if(!stackChat&&!request.results)stackChat=crypto.randomBytes(32).toString('hex');
      const rememberMemory=id=>memory.remember(id,options,stackChat);
      let legacyResume=false,managed=false;
      const reportModelFallbacks=rows=>{
        const observed=audit.modelFallbacks??=[];
        for(const row of rows){
          if(observed.some(saved=>saved.tier===row.tier&&saved.model===row.model&&saved.availableModel===row.availableModel))continue;
          observed.push(row);
          progress.report(new vscode.LanguageModelTextPart(fallbackMessage(row)+'\n\n'));
        }
      };
      const advance=async(flow,event,extra)=>{
        const next=await flowEvent(flow,event,tier,activeCancel.token,extra,
          classifying(flow)?savedClassifierThinking??thinkForTurn:thinkForTurn,chatKey,legacyResume?undefined:flow?.stackChat||stackChat,
          reportModelFallbacks);
        managed ||= !!next.task_stack;
        return next;
      };
      function resumeUnavailable(message){
        audit.stage='resume_unavailable';
        audit.message=message;
        progress.report(new vscode.LanguageModelTextPart(message));
      }
      function showRequestNotice(flow){
        if(!flow.request_notice)return false;
        assertCurrent(a,activeCancel.token);
        audit.stage='guidance';
        audit.message=flow.request_notice;
        progress.report(new vscode.LanguageModelTextPart(flow.request_notice));
        return true;
      }
      async function readDocumentEvidence(flow){
        const workspace=deps.workspace();
        const located=await advance(flow,'locate_document');
        const lookup=located.stop?.document_lookup;
        if(!lookup||!['found','ambiguous'].includes(lookup.status))return located;
        assertCurrent(a,activeCancel.token);
        let file=lookup.paths[0];
        if(lookup.status==='ambiguous'){
          const choice=await vscode.window.showQuickPick(lookup.paths,{title:'Choose the DOCX to review'});
          assertCurrent(a,activeCancel.token);
          if(!choice)return located;
          file=choice;
        }
        const decision=await vscode.window.showWarningMessage('Read this document for the saved review?',{
          modal:true,detail:file+'\n\nSutra will read this file once and send its extracted text to the selected local model.'
        },'Read document');
        assertCurrent(a,activeCancel.token);
        if(decision!=='Read document'||deps.workspace()!==workspace)return located;
        const next=await advance(located,'approve_document',{confirmed:true,document_path:file});
        if(next.state==='CLASSIFYING')
          progress.report(new vscode.LanguageModelTextPart('Document read approved. Continuing the saved review.\n\n'));
        return next;
      }
      async function offerAction(flow,invocationId,kind){
        if(!canAct){
          progress.report(new vscode.LanguageModelTextPart('Enable the Sutra action tool in native Agent chat to review the saved proposal.'));
          return;
        }
        if(!await allowActionProfile(flow))return;
        assertCurrent(a,activeCancel.token);
        const binding=flow.binding;
        const scope=managed?{chat_key:stackChat,flow_id:flow.flow_id}:{};
        const preview=await deps.request({op:'native_preview',...binding,...scope,invocation_id:invocationId},activeCancel.token);
        assertCurrent(a,activeCancel.token);
        const id=crypto.randomUUID(),callId=crypto.randomUUID();
        tickets.set(id,{id,binding,generation:a.generation,expires:Date.now()+300000,invocation_id:invocationId,
          kind:kind||(preview.runner?'run_tests':'edit'),preview,prompt,conversationId:flow.conversation_id||conversationId,
          callId,thinkForTurn,flowScope:scope,nativeChat:chatKey});
        if(preview.diff)await deps.showDiff(preview,invocationId);
        try{assertCurrent(a,activeCancel.token);}catch(error){tickets.delete(id);throw error;}
        progress.report(new vscode.LanguageModelToolCallPart(callId,ACTION,{ticket:id}));
      }
      function sameChat(pending,explicit){
        if(pending.stackChat&&pending.stackChat!==stackChat)return false;
        if(pending.nativeChat&&chatKey)return pending.nativeChat===chatKey;
        if(pending.stackChat===stackChat&&stackChat)return true;
        if(!explicit)return true; // Older saved reviews still accept an intentional exact continue.
        if(pending.conversation_id&&request.parent===pending.conversation_id&&audit.memorySource==='signed_checkpoint')return true;
        // The host-generated pause link can bind a conversation without a session ID or receipt.
        return hasFlowLink(pending);
      }
      async function allowActionProfile(flow){
        assertCurrent(a,activeCancel.token);
        if(a.profile==='workspace_agent')return true;
        await requestWorkspaceAccess(flow,audit,progress,true);
        return false;
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
          if(unidentifiedResume){
            resumeUnavailable('Choose the saved request in Open Sutra tasks, or return to the chat that started it.');
            return;
          }
          let pending=context.workspaceState.get(pendingKey);
          const resumeId=prompt.trim().match(RESUME_PROMPT)?.[1];
          let submission;
          if(resumeId){
            submission=resumeSubmission;
            if(submission?.id===resumeId)resumeSubmission=undefined;
            if(submission?.id!==resumeId||submission.expires<=Date.now()||submission.workspace!==deps.workspace()||
              pending?.flow_id!==submission.flowId||pending.workspace!==submission.workspace){
              resumeUnavailable('This Sutra resume action has expired. Use the current saved request in Sutra Monitor.');
              return;
            }
          }
          if(!pending||pending.stackChat&&pending.stackChat!==stackChat){
            const saved=await advance(null,'pending');
            pending=saved.flow_id?{...saved,workspace:deps.workspace(),stackChat:saved.task_stack?stackChat:undefined}:undefined;
          }
          const resumeRequested=!!resumeId||/^(continue|resume|please continue|please resume)[.! ]*$/i.test(prompt.trim());
          const supply=pending?.workspace===deps.workspace()&&evidenceReply(pending)&&sameChat(pending,true);
          if((resumeRequested||supply)&&pending?.workspace===deps.workspace()){
            if(!sameChat(pending,!!submission||supply)){
              resumeUnavailable('Open the originating Sutra chat and send **continue** there to resume this request.');
              return;
            }
            legacyResume=!pending.stackChat;
            const old=await deps.request({op:'orchestrate',event:'inspect',flow_id:pending.flow_id,...flowScope(pending)},activeCancel.token);
            managed ||= !!old.task_stack;
            if(submission&&(old.cursor!==submission.cursor||['DONE','CANCELLED'].includes(old.state))){
              resumeUnavailable('The saved Sutra request advanced. Choose Resume from the current pause in Sutra Monitor.');
              return;
            }
            if(showRequestNotice(old))return;
            if(evidencePause(old)){
              if(supply&&evidenceReply(old)){
                flow=await advance(old,'supply_evidence',{evidence_text:prompt});
                if(!evidencePause(flow))progress.report(new vscode.LanguageModelTextPart('Continuing the saved request with the supplied evidence.\n\n'));
              }else if(documentFilePause(old)){
                flow=old;
              }else{
                showPause(old,audit,progress);
                return;
              }
              prompt=old.prompt||prompt;
              conversationId=old.conversation_id;
              audit.memoryUsed=true;
            }else if(supply){
              resumeUnavailable('The saved evidence request has changed. Review its current state in Sutra Monitor.');
              return;
            }else if(['DONE','CANCELLED'].includes(old.state)){
              await rememberFlow(old,undefined,chatKey,pending.stackChat);
              if(old.conversation?.document_verification)flow=old;
            }
            else if(old.recovery){
              await rememberFlow(old,undefined,chatKey,pending.stackChat);
              const recovery=old.recovery;
              if(!recovery.restart_allowed){
                progress.report(new vscode.LanguageModelTextPart(recovery.message+'\n\nUse **Discard** in Sutra Monitor, then submit your new request.'));return;
              }
              const title=recovery.kind==='provenance_changed'?'Source changed during this review.':'This review was started by an older Sutra policy.';
              const choice=await vscode.window.showWarningMessage(title,{modal:true,detail:recovery.message+'\nSaved request: '+old.prompt},'Restart review');
              if(activeCancel.token.isCancellationRequested||choice!=='Restart review'){
                progress.report(new vscode.LanguageModelTextPart('Saved request retained. '+recovery.message));return;
              }
              flow=old;prompt=old.prompt;restartReview=true;
              conversationId=undefined;footerId=undefined;footerSignature=undefined;
            }else {

              resuming=true;
              flow=old;prompt=old.prompt;conversationId=old.conversation_id;audit.memoryUsed=true;audit.conversationInvocationId=conversationId;
              if(classifying(old)){
                savedClassifierThinking=pending.resumeThinking===true;
                if(savedClassifierThinking)thinkForTurn=true;
              }
              if(conversationId){
                footerSignature=legacyResume?memory.signature(conversationId):await rememberMemory(conversationId);
                footerId=conversationId;
              }
              progress.report(new vscode.LanguageModelTextPart('Resuming the saved Sutra workflow with its context and remaining budget.\n\n'));
            }
          }
          if(!flow)flow=await advance(null,'start',{prompt,parent:request.parent,
            prior_prompt:!chatKey&&!request.parent?request.priorPrompt:undefined});
          if(resuming&&flow.state==='PAUSED'&&flow.stop?.resume_phase==='CLASSIFYING'){
            flow=await advance(flow,'resume');
          }
          if(resuming&&flow.state==='CLASSIFYING')flow=await advance(flow,'step');
          let documentLookupAttempted=false;
          while(flow.routing_step||flow.state==='CLASSIFYING'||(!documentLookupAttempted&&documentFilePause(flow))){
            assertCurrent(a,activeCancel.token);
            if(flow.document_stage_progress){
              progress.report(new vscode.LanguageModelTextPart(flow.document_stage_progress+'\n\n'));
            }
            if(flow.routing_step){
              const step=flow.routing_step;recordUsage(step.result);lastRoute=step.route.model;audit.model=lastRoute;
              audit.routingDecision=step.routing_followup;audit.stage='routing';publish();
              progress.report(new vscode.LanguageModelTextPart((step.routing_followup?.kind==='page_in'?'Loading the requested conversation context before choosing a model.':step.routing_followup?.kind==='reasoning'?'Checking a higher supported reasoning setting on the same model against the remaining budget.':step.routing_followup?.kind==='escalate'?'Checking the next model tier against the remaining budget and available memory.':flow.progress)+'\n\n'));
            }
            if(!documentLookupAttempted&&documentFilePause(flow)){
              documentLookupAttempted=true;
              if(flow.conversation){
                const classified=recordConversation(flow.conversation);
                footerId=classified.invocation_id;
                footerSignature=await rememberMemory(footerId);
              }
              flow=await readDocumentEvidence(flow);
              continue;
            }
            if(flow.state==='PAUSED'){showPause(flow,audit,progress);return;}
            if(flow.state!=='CLASSIFYING')break;
            flow=await advance(flow,'step');
          }
          if(!flow.conversation&&showRequestNotice(flow))return;
          if(!flow.conversation&&flow.state==='PAUSED'&&(classifying(flow)||evidencePause(flow))){showPause(flow,audit,progress);return;}
          const chat=flow.conversation;
          if(chat){
          assertCurrent(a,activeCancel.token);
          const r=recordConversation(chat);
          if(!r.admission.admitted){progress.report(new vscode.LanguageModelTextPart('Sutra withheld the conversation response: '+r.admission.errors.join('; ')));return;}
          const p=r.accepted_output;
          if(options.toolMode===vscode.LanguageModelChatToolMode.Required&&flow.state==='DONE'&&!flow.deferred_flow_id&&!flow.request_notice)throw new Error('No admitted action is available to satisfy required tool mode.');
          footerSignature=await rememberMemory(r.invocation_id);footerId=r.invocation_id;
          if(showRequestNotice(flow))return;
          if(evidencePause(flow)){showPause(flow,audit,progress);return;}
          if(flow.state==='PAUSED'&&classifying(flow)){showPause(flow,audit,progress);return;}
          if(flow.deferred_flow_id){
            const old=await deps.request({op:'orchestrate',event:'inspect',flow_id:flow.deferred_flow_id},activeCancel.token);
            await rememberFlow(old);
            const choice=await vscode.window.showWarningMessage('Another Sutra workspace request is saved.',{modal:true,detail:'New request: '+prompt+'\nSaved request: '+old.prompt+'\nDiscard releases unused child reservations. Consumed budgets, receipts and task permissions remain. To resume the saved request instead, cancel and send continue.'},'Discard and use new request');
            if(activeCancel.token.isCancellationRequested||choice!=='Discard and use new request'){
              progress.report(new vscode.LanguageModelTextPart('Saved request retained. Your new workspace request has not run. Send **continue** to resume the saved request, or use **Discard** in Sutra Monitor before submitting a new workspace request. General conversation can continue.'));return;
            }
            assertCurrent(a,activeCancel.token);
            await advance(old,'cancel');
            flow=await advance(null,'start_existing',{prompt,conversation_id:r.invocation_id});
            if(showRequestNotice(flow))return;
          }
          // Routing prose is not an authority on host capabilities or permissions.
          // Present a host-owned transition; publish model prose for conversation.
          progress.report(new vscode.LanguageModelTextPart((flow.state!=='DONE'?'Continuing this request through Sutra’s workspace workflow.':conversationMarkdown(p))+'\n\n'));
          if(p.diagram){try{await deps.presentConversation?.(chat);progress.report(new vscode.LanguageModelTextPart('Diagram opened beside the chat. Use **Sutra: Open Last Diagram** to reopen it.\n\n'));}catch(error){progress.report(new vscode.LanguageModelTextPart('Diagram preview could not open: '+error.message+'\n\n'));}}
          if(flow.state==='DONE')return;
          const commitmentText=observationText(flow.task_observations);
          if(commitmentText){audit.taskObservations=flow.task_observations;publish();}
          conversationId=r.invocation_id;audit.conversationInvocationId=r.invocation_id;
          }
          if(!a){
            audit.stage='awaiting_workspace_permission';publish();
            await requestWorkspaceAccess(flow,audit,progress);
            return;
          }else{
            audit.workspaceAccess={state:'reused',workspace:a.workspace,profile:a.profile};publish();
          }

        }
        assertCurrent(a,activeCancel.token);
        assertArmed();
        if(actionBinding){
          const current=await deps.request({op:'state',...actionScope},activeCancel.token);
          assertCurrent(a,activeCancel.token);
          if(!matchesTask(current,actionBinding))
            throw new Error('Task or calibration changed after the action. Its receipt is retained.');
        }
        if(restartReview){
          flow=await advance(flow,'restart_review');
          if(showRequestNotice(flow))return;
          showHostProgress(flow,progress);
        }
        if(!flow)flow=await advance(null,'start_existing',{prompt,conversation_id:conversationId,
          ...(actionScope?.flow_id?{action_flow_id:actionScope.flow_id}:{})});
        if(showRequestNotice(flow))return;
        if(flow.state==='PAUSED'){
          flow=await advance(flow,'resume');
          if(showRequestNotice(flow))return;
          showHostProgress(flow,progress);
        }
        while(!['DONE','AWAITING_ACTION'].includes(flow.state)){
          assertCurrent(a,activeCancel.token);
          if(flow.recovery){audit.stage='recovery_required';audit.message=flow.recovery.message;publish();progress.report(new vscode.LanguageModelTextPart(flow.recovery.message+'\n\n'+resumeAction(flow)));return;}
          if(flow.state==='PAUSED'){
            if(flow.stop?.status==='provenance_confirmation'){
              const choice=await vscode.window.showWarningMessage('Use the open source snapshot for this review?',{modal:true,detail:flow.stop.error},'Review open snapshot');
              assertCurrent(a,activeCancel.token);
              if(choice==='Review open snapshot'){
                flow=await advance(flow,'accept_provenance',{confirmed:true});
                showHostProgress(flow,progress);
                continue;
              }
              audit.stage='paused';
              progress.report(new vscode.LanguageModelTextPart('Review paused before source mapping. Resume to choose this snapshot, or open the intended checkout and submit the review there.\n\n'+resumeAction(flow)));
              return;
            }
            showPause(flow,audit,progress);
            return;
          }
          if(['QUARANTINED','CANCELLING','EXECUTING_ACTION'].includes(flow.state))throw new Error('Sutra workflow '+flow.state+'. Saved request retained. Inspect Sutra Monitor, then choose Resume or Discard.');
          let event='step',extra={};
          if(flow.state==='NEED_TOOL_APPROVAL'&&!flow.capability_settled){
            const authorization=await deps.approveCapability(flow.capability_proposal,activeCancel.token);
            assertCurrent(a,activeCancel.token);
            if(!authorization){audit.stage='permission_declined';progress.report(new vscode.LanguageModelTextPart('Tool permission was not granted.\n\n'+resumeAction(flow)));return;}
            event='capability_grant';extra={authorization};
          }
          if(flow.state==='NEED_PERMISSION'){
            if(!await allowActionProfile(flow)){
              return;
            }
            const grant=await approvePermission(flow.permission,activeCancel.token);
            assertCurrent(a,activeCancel.token);
            if(!grant){audit.stage='permission_declined';progress.report(new vscode.LanguageModelTextPart('Permission was not granted. No edit or test ran.\n\n'+resumeAction(flow)));return;}
            event='grant';extra={grant};
          }
          flow=await advance(flow,event,extra);
          if(showRequestNotice(flow))return;
          const bound=flow.binding||flow.bound_task;
          if(bound)audit.session=bound.session;
          if(flow.claim_review_usage)recordUsage({token_usage:flow.claim_review_usage});
          audit.orchestration={flowId:flow.flow_id,state:flow.state,children:flow.children};publish();
          showHostProgress(flow,progress);
          if(flow.child)recordUsage(flow.child.result);
        }
        assertCurrent(a,activeCancel.token);
        if(flow.capability_result){audit.stage='tool_finished';progress.report(new vscode.LanguageModelTextPart(deps.capabilityReport(flow.capability_result)));return;}
        data=flow.data;
        if(!data&&flow.state==='AWAITING_ACTION'&&flow.pending_action){
          audit.stage='awaiting_action';
          await offerAction(flow,flow.pending_action);
          return;
        }
        if(!data){progress.report(new vscode.LanguageModelTextPart(flow.state==='AWAITING_ACTION'?'The saved proposal is awaiting action confirmation. Ask for a fresh proposal if its confirmation is no longer available.':'This workflow has finished. Send a new request to continue.'));return;}
        if(data?.notice){audit.stage='guidance';progress.report(new vscode.LanguageModelTextPart(data.notice.message+'\n\nSutra setup guidance — no model invocation.'));return;}
        if(!data?.result)throw new Error(data?.notice?.message||'No governed response available.');
        const reviewLinks=deps.publishReview(data);lastRoute=data.route?.model||data.result.model||'';publish();
        const assessmentText=observationText(flow.task_observations,true);
        const r=data.result;recordUsage(r);audit.invocationId=r.invocation_id;audit.model=lastRoute;audit.stage=r.admission.admitted?'admitted':'withheld';publish();if(!r.admission.admitted){audit.taskObservations=flow.task_observations;progress.report(new vscode.LanguageModelTextPart('Sutra withheld the response: '+r.admission.errors.join('; ')));return;}
        const p=r.accepted_output;if(options.toolMode===vscode.LanguageModelChatToolMode.Required&&!['edit','run_tests'].includes(p.action?.kind))throw new Error('No admitted action is available to satisfy required tool mode.');
        if(data.report_markdown){
          progress.report(new vscode.LanguageModelTextPart(data.report_markdown+'\n\n'));
          if(data.review_diagram){try{await deps.presentReview?.(data);progress.report(new vscode.LanguageModelTextPart('The **source dependency diagram** is open beside this chat. Its arrows show local import candidates, not execution order.\n\n'));}catch(error){progress.report(new vscode.LanguageModelTextPart('Diagram preview unavailable: '+error.message+'\n\n'));}}
          if(reviewLinks?.reportUri)progress.report(new vscode.LanguageModelTextPart(`[Review details and source citations](${reviewLinks.reportUri}) · Reviewed through Sutra with **${lastRoute}**.\n\n`));
        }else progress.report(new vscode.LanguageModelTextPart((p.answer||p.summary)+'\n\n'));
        if(assessmentText){audit.taskObservations=flow.task_observations;publish();}
        const evidence=data.coverage?.evidence;
        if(data.coverage?.deferred_files>0)progress.report(new vscode.LanguageModelTextPart(`Source paging: ${data.coverage.fresh_selected_files} fresh files selected; ${data.coverage.deferred_files} mapped files deferred to stay within the current allowance. This response covers the selected evidence.\n\n`));
        if(evidence){
          const followupCount=Array.isArray(evidence.followup_files)?evidence.followup_files.length:evidence.followup_files;
          const followups=Number.isInteger(followupCount)&&followupCount>0?`; governed follow-up reads from ${followupCount} files`:'';
          progress.report(new vscode.LanguageModelTextPart(`Evidence delivered: ${evidence.fresh_snapshot_files} freshly mapped files; source excerpts from ${evidence.excerpt_files.length} files; ${evidence.child_reports} child reports${followups}. This is a bounded review, not an exhaustive semantic audit.\n\n`));
        }
        if(['edit','run_tests'].includes(p.action?.kind)){
          await offerAction(flow,r.invocation_id,p.action.kind);
        }else if(!p.report){progress.report(new vscode.LanguageModelTextPart('Remaining: '+(p.remaining?.join('; ')||'No further step proposed.')+'\n\nModel notes are not proof of correctness. Test receipts report process observations.'));}
      }finally{
        if(responseUsage.calls.length||footerId){
          const detail=tokens.compact(responseUsage),label='Sutra · '+detail+' · conversation details';
          const footer=footerId?`[${label}](karma-preview:/conversation/${footerId}/Response.md?checkpoint=${footerSignature})`:'Sutra · '+detail;
          if(!activeCancel.token.isCancellationRequested)progress.report(new vscode.LanguageModelTextPart('\n\n---\n'+footer+(footerId?(audit.memoryUsed?' · Memory restored':' · New conversation'):'')+' · Open tokens, system and kernel status with **Sutra Monitor** in the status bar.\n'));
        }
        const stopped=activeCancel.token.isCancellationRequested;
        activityOpen=false;activityProgress=undefined;cancellation.dispose();activeCancel.dispose();activeCancel=undefined;running=false;publish();
        if(stopped){try{const key=legacyResume?undefined:stackChat;
          await rememberFlow(await deps.request({op:'orchestrate',event:'pending',...(key?{chat_key:key}:{})}),undefined,chatKey,key);
        }catch{} }
        if(stopped&&context.workspaceState.get(pendingKey))void vscode.window.showInformationMessage('Sutra stopped. The saved request, context and consumed budget are retained. Resume or Discard from Sutra Monitor.');
      }
      });
    }
  };
  if(vscode.window.registerUriHandler)context.subscriptions.push(vscode.window.registerUriHandler({
    handleUri(uri){
      const link=uri.path.match(/^\/resume\/([a-f0-9]{32})$/)?.[1];
      const access=uri.path.match(/^\/access\/([a-f0-9]{32})$/)?.[1];
      if(access&&uri.authority===EXTENSION_ID&&!uri.query&&!uri.fragment)
        return chooseWorkspaceAccess(access).catch(error=>vscode.window.showErrorMessage(error.message));
      if(link&&uri.authority===EXTENSION_ID&&!uri.query&&!uri.fragment)
        return recoverSaved(false,link).catch(error=>vscode.window.showErrorMessage(error.message));
    }
  }));
  context.subscriptions.push(changed,vscode.lm.registerLanguageModelChatProvider('karma',provider),vscode.lm.registerTool(ACTION,tool),
    vscode.commands.registerCommand('karma.useNativeChat',()=>useNativeChat()),
    vscode.commands.registerCommand('karma.toggleThinkingForNextRequest',toggleNextThinking),
    vscode.commands.registerCommand('karma.cancelThinkingForNextRequest',()=>thinkingForNextRequest?toggleNextThinking():undefined),
    vscode.commands.registerCommand('karma.resumeWorkflow',()=>recoverSaved().catch(error=>vscode.window.showErrorMessage?.(error.message))),
    vscode.commands.registerCommand('karma.discardWorkflow',()=>recoverSaved(true).catch(error=>vscode.window.showErrorMessage?.(error.message))),
    vscode.commands.registerCommand('karma.openTasks',()=>openTasks().catch(error=>vscode.window.showErrorMessage(error.message))),
    vscode.commands.registerCommand('karma.increaseAllocation',()=>increaseAllocation().catch(error=>vscode.window.showErrorMessage(error.message))),
    vscode.commands.registerCommand('karma.armNative',()=>chooseWorkspaceAccess().catch(error=>vscode.window.showErrorMessage?.(error.message))),
    vscode.commands.registerCommand('karma.disarmNative',()=>revoke()),
    vscode.commands.registerCommand('karma.toggleNative',async()=>{clearAccess('Native chat setting changed.');await vscode.workspace.getConfiguration('karma').update('nativeEnabled',!enabled(),vscode.ConfigurationTarget.Global);changed.fire();publish();}));
  const listener=vscode.workspace.onDidChangeConfiguration?.(e=>{if(e.affectsConfiguration('karma.nativeEnabled')){if(!enabled())void setNextThinking(false);clearAccess('Native chat setting changed.');changed.fire();}else if(e.affectsConfiguration('karma.models'))changed.fire();if(e.affectsConfiguration('chat'))publish();});if(listener)context.subscriptions.push(listener);
  void setNextThinking(false);
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
  return {status,publish,withWorkspaceAccess,requestWorkspaceAccess:()=>chooseWorkspaceAccess(),dispose(){
    for(const subscription of subscriptions)subscription.dispose();
    clearAccess('Extension stopped.');
  }};
}
module.exports={install,latestRequest,observationText,conversationMarkdown};

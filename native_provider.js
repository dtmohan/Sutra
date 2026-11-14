'use strict';
const crypto=require('node:crypto'),path=require('node:path');
const tokens=require('./media/token_usage');
const {NativeMemory,isCompaction}=require('./native_memory');
const hostTokens=require('./host_tokens');
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
  if(!prompt.trim()||prompt.length>1800)throw new Error('Sutra needs a text request of 1–1800 characters. Attach source through the armed scope.');
  const previous=[...messages].reverse().filter(m=>m.role===vscode.LanguageModelChatMessageRole.Assistant);
  const parent=previous.map(m=>m.content.filter(p=>p instanceof vscode.LanguageModelTextPart).map(p=>p.value).join('')).map(text=>{const matches=[...text.matchAll(/\[(?:Karma|Sutra)[^\n]*\]\(karma-preview:\/conversation\/([a-f0-9]{32})\/Response\.md\)/g)];return matches.at(-1)?.[1]||text.match(/^(?:Karma|Sutra) conversation receipt: ([a-f0-9]{32})$/m)?.[1];}).find(Boolean);
  return {prompt,parent};
}
function install(vscode,context,deps){
  let recoveryChoice;
  let armPromise,accessEpoch=0,armingStep='',armed,activeCancel,arming=false,running=false,selecting=false,lastRoute='',routing,reason='Open Sutra chat to talk. Arm Sutra when you need workspace access.';
  const memory=new NativeMemory(context);let lastCompaction;
  const tickets=new Map(),completed=new Map(),changed=new vscode.EventEmitter();
  const enabled=()=>vscode.workspace.getConfiguration('karma').get('nativeEnabled',true);
  function status(){
    const chat=vscode.workspace.getConfiguration('chat');
    const defaultsReady=Object.entries(CHAT_DEFAULTS).every(([key,value])=>chat.get(key)===value);
    // The stable VS Code API does not expose the focused chat's model or harness.
    // An armed task/default preference is never evidence about another chat.
    const live=!!routing?.active;
    const label=!enabled()?'Sutra: off':arming?'Sutra: finish arming':selecting?'Sutra: opening chat':live?'Sutra: request received':running?'Sutra: action running':armed?'Sutra: armed · routing unverified':'Sutra: chat ready · workspace unarmed';
    const explanation=arming?armingStep:selecting?'Opening Local chat with Sutra Auto. No prompt is being sent.':live?'This request reached Sutra. Protocol admission is checked before model dispatch.':
      'No Sutra request is in flight. The current chat selection is unverified; check for Sutra Auto beneath the prompt. Direct Ollama and other chats bypass Sutra.';
    return {enabled:enabled(),arming,armingStep,selecting,armed:!!armed&&enabled(),working:running||live,session:armed?.session,scope:'Sutra model requests and Sutra action tool only',defaultModel:chat.get('defaultModel',''),defaultsReady,reason,lastRoute,label,explanation,routing:routing?{...routing}:null,compaction:lastCompaction,pending:context.workspaceState.get('sutra.pendingOrchestration')||null};
  }
  function publish(){deps.onStatus(status());}
  function disarm(why='Disarmed by user. Task and budget retained.'){accessEpoch++;armed=undefined;tickets.clear();completed.clear();activeCancel?.cancel();reason=why;publish();}
  const identity=st=>({session:st.session,contract_hash:st.contract?.contract_hash,calibration_hash:st.calibration?.binding?.hash});
  function assertArmed(){if(arming)throw new Error('Task arming is in progress.');if(!enabled()||!armed)throw new Error('Sutra is not armed. Run Sutra: Arm Workspace Access.');if(deps.workspace()!==armed.workspace){disarm('Workspace changed.');throw new Error(reason);}return armed;}
  async function check(token){const a=assertArmed();if(!a.binding)throw new Error('No workspace task is bound yet.');const st=await deps.request({op:'state'},token);if(armed!==a||JSON.stringify(identity(st))!==JSON.stringify(a.binding)){disarm('Task or calibration changed. Re-arm to accept the current contract.');throw new Error(reason);}return st;}
  function setArmingStep(message){armingStep=message;publish();}
  function beginArm(owner){
    if(armPromise)return armPromise;
    armPromise=arm(owner).finally(()=>{armPromise=undefined;});return armPromise;
  }
  async function arm(owner){
    // Only the current provider request may ask for access while it is running.
    // Public commands ignore arguments; another extension cannot supply this owner.
    const ownsRequest=()=>owner&&routing===owner.audit&&routing.active&&running&&activeCancel?.token===owner.token;
    if(arming||selecting||deps.isBusy()||((running||routing?.active)&&!ownsRequest()))throw new Error('Wait for the active Sutra request before arming.');
    if(!enabled())throw new Error('Enable Sutra native chat first.');
    if(armed)return true;
    const epoch=accessEpoch,workspace=deps.workspace();
    const stillValid=()=>{if(epoch!==accessEpoch||!enabled()||deps.workspace()!==workspace||owner&&(!ownsRequest()||owner.token.isCancellationRequested))throw new Error('Arming cancelled: Sutra was disarmed, disabled, or the request/workspace changed.');if(deps.isBusy())throw new Error('Workspace became busy while arming.');};
    let success=false;arming=true;setArmingStep('Confirm Arm Sutra in the dialog. This request will continue here; edits and tests ask when needed.');
    const access={id:crypto.randomUUID(),workspace,state:'requested',requestedAt:new Date().toISOString(),source:'vscode_modal'};
    const recordAccess=state=>{access.state=state;access.updatedAt=new Date().toISOString();if(owner){owner.audit.workspaceAccess={...access};publish();deps.onRoute?.({...owner.audit});}};
    try{
      recordAccess('requested');
      const yes=await vscode.window.showWarningMessage('Arm Sutra for this workspace?',{modal:true,detail:workspace+'\nSutra may read project files for your requests. Existing task scope and permissions are retained. File grants persist with the task across model switches; disarm or reload stops execution until you arm again. Each edit or test still requires action confirmation. Native workspace tasks forbid network access and new dependencies.\n'+(owner?'Your pending request will continue in this chat.':'Your current chat stays open.')},'Arm Sutra');
      stillValid();
      if(yes!=='Arm Sutra'){recordAccess('declined');return false;}
      recordAccess('confirmed');
      const st=await deps.request({op:'state'},owner?.token);
      stillValid();
      const current=st.auto||st.contract?.contract.constraints.native_task;
      armed={workspace,generation:crypto.randomUUID(),session:current?st.session:undefined,binding:current?identity(st):undefined,coding:!!(current&&st.contract.contract.constraints.native_task)};
      reason='Armed for workspace reading. Describe the work in chat; Sutra asks for additional permissions when needed.';
      recordAccess('granted');
      success=true;
      if(!owner)void vscode.window.showInformationMessage('Sutra is armed. Your current chat is unchanged. Use Sutra Auto for governed requests.');
      return true;
    }catch(error){recordAccess('cancelled');throw error;}
    finally{arming=false;armingStep='';if(!success&&epoch===accessEpoch)reason='Arming cancelled. Conversation remains available.';publish();}
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
    const yes=await vscode.window.showWarningMessage('Sutra needs permission for this request.',{modal:true,detail:'Files: '+paths.join(', ')+'\nTask: '+descriptor.goal+'\nNetwork and dependencies remain off. This file scope persists with the task across model switches. Disarm or reload stops execution until you arm again; it does not erase these grants. Every edit/test still gets its own confirmation.'},label);
    if(yes!==label||token.isCancellationRequested)return;
    return {operation:descriptor.operation,paths,test_paths};
  }
  const pendingKey='sutra.pendingOrchestration';
  async function rememberFlow(flow){await context.workspaceState.update(pendingKey,flow.state==='DONE'||flow.state==='CANCELLED'?undefined:{...flow,workspace:deps.workspace(),conversation:undefined,child:undefined,data:undefined,mapping:undefined});publish();}
  async function recoverSaved(discard=false){
    if(running||routing?.active||deps.isBusy())throw new Error('Stop the active request and wait for cleanup before changing the saved workflow.');
    const pending=context.workspaceState.get(pendingKey);
    if(!pending||pending.workspace!==deps.workspace()){void vscode.window.showInformationMessage('No saved workflow in this workspace.');return;}
    if(discard){
      const yes=await vscode.window.showWarningMessage('Discard the saved Sutra request?',{modal:true,detail:'Consumed budgets, receipts and task permissions are retained. Unused child reservations are released. Executed actions are not undone.'},'Discard request');
      if(yes!=='Discard request')return;
      const current=await deps.request({op:'orchestrate',event:'inspect',flow_id:pending.flow_id});
      await rememberFlow(['DONE','CANCELLED'].includes(current.state)?current:await deps.request({op:'orchestrate',event:'cancel',flow_id:current.flow_id,cursor:current.cursor}));recoveryChoice=undefined;
    }else{
      const current=await deps.request({op:'orchestrate',event:'inspect',flow_id:pending.flow_id});
      await rememberFlow(current);
      recoveryChoice=pending.flow_id;
      // Focus the existing chat without replacing its draft, model, or history.
      await vscode.commands.executeCommand('workbench.action.chat.open');
      void vscode.window.showInformationMessage(current.recovery?current.recovery.message+' Send continue in Sutra chat to choose recovery.':'Resume selected. Send your next message with a Sutra model to continue the saved request with its original context and remaining budget.');
    }
  }
  let activityProgress;
  async function flowEvent(flow,event,tier,token,extra={}){
    const models=['inspect','pending','cancel'].includes(event)?undefined:await deps.configureModels(false,token);
    let next;
    try{next=await deps.request({op:'orchestrate',event,flow_id:flow?.flow_id,cursor:flow?.cursor,armed:!!armed,binding:armed?.binding,models,tier,...extra},token,activityProgress);}
    catch(error){if(/Task changed|Workflow task|permissions changed|calibration.*changed/i.test(error.message))disarm('Task changed. Re-arm to accept the current contract.');throw error;}
    await rememberFlow(next);return next;
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
      reason='Local chat and Sutra Auto saved for new sessions. A model preference does not arm a task or verify routing.';
      publish();
    }finally{selecting=false;publish();}
  }
  async function useNativeChat(){
    if(arming){void vscode.window.showInformationMessage(armingStep);return;}
    if(running||routing?.active||deps.isBusy())throw new Error('A Sutra operation is running. Wait for it to finish before opening another chat.');
    await selectNativeChat();
    void vscode.window.showInformationMessage('Opened Local chat with Sutra Auto. This choice is saved for new sessions. You can chat now. Arm Sutra for workspace reading; edits and tests ask when needed; existing conversations keep their own model.', 'Arm Sutra').then(choice=>{
      if(choice==='Arm Sutra')return beginArm();
    }).catch(error=>vscode.window.showErrorMessage(error.message));
  }
  async function trackRequest(model,progress,token,invoke){
    if(routing?.active||running||deps.isBusy())throw new Error('A Sutra request is running. Wait before switching models.');
    const audit={id:crypto.randomUUID(),model:model.id,startedAt:new Date().toISOString(),active:true,stage:'received'};
    routing=audit;publish();deps.onRoute?.({...audit});
    try{
      // Live routing is shown in the status bar and dashboard; keep audit IDs out of the answer body.
      await invoke(audit);
      if(audit.stage==='received')audit.stage='stopped';
    }catch(error){audit.stage=token.isCancellationRequested||/cancel|disarm/i.test(error.message)?'cancelled':'blocked';audit.message=error.message;if(/Ollama|model.*unavailable|model.*discover|No local chat model/i.test(error.message))void vscode.window.showErrorMessage?.(error.message,'Model settings')?.then(choice=>{if(choice==='Model settings')return deps.configureModels(true);}).catch(()=>{});throw error;}
    finally{audit.active=false;audit.finishedAt=new Date().toISOString();publish();deps.onRoute?.({...audit});}
  }
  function ticket(input){const t=tickets.get(input?.ticket),a=assertArmed();if(!t||t.generation!==a.generation||Date.now()>t.expires)throw new Error('Missing, expired or revoked Sutra action ticket.');return t;}
  const tool={
    prepareInvocation(options){const t=ticket(options.input);const md=new vscode.MarkdownString();md.appendText(t.kind==='edit'?'Apply this exact proposed edit to '+t.preview.target+'?':'Run the permitted Python test '+t.preview.target+' in a read-only snapshot with network denied?');if(t.preview.snapshot_files)md.appendText(' Snapshot files: '+t.preview.snapshot_files.join(', ')+'. '+t.preview.snapshot_note);if(t.preview.diff)md.appendCodeblock(t.preview.diff,'diff');md.isTrusted=false;
      return {invocationMessage:t.kind==='edit'?'Applying governed edit':'Running governed test',confirmationMessages:{title:'Sutra: '+(t.kind==='edit'?'apply edit':'run test'),message:md}};},
    async invoke(options,token){
      if(running||deps.isBusy())throw new Error('A Sutra operation is already running.');
      if(token.isCancellationRequested)throw new Error('Sutra action cancelled.');
      const t=ticket(options.input);await check(token);if(ticket(options.input)!==t)throw new Error('Action revoked.');
      // VS Code may skip prepareInvocation confirmations for programmatic calls
      // without a chat token. Require a host dialog for that path as well.
      if(!options.toolInvocationToken){
        const label=t.kind==='edit'?'Apply edit':'Run test';
        const confirmed=await vscode.window.showWarningMessage('Sutra: '+label+' · '+t.preview.target,{modal:true,detail:t.preview.diff||'Run the permitted Python unittest in a read-only snapshot with network denied. Exit status is a process observation; assertions remain unverified.'+(t.preview.snapshot_files?' Snapshot files: '+t.preview.snapshot_files.join(', ')+'. '+t.preview.snapshot_note:'')},label);
        if(confirmed!==label)throw new Error('Sutra action not confirmed.');
        await check(token);if(ticket(options.input)!==t)throw new Error('Action revoked.');
      }
      if(running||deps.isBusy())throw new Error('Another Sutra operation started.');
      if(token.isCancellationRequested)throw new Error('Sutra action cancelled.');
      tickets.delete(options.input.ticket);running=true;activeCancel=new vscode.CancellationTokenSource();const cancel=token.onCancellationRequested(()=>activeCancel?.cancel());publish();
      try{const data=await deps.request({op:'native_execute',...armed.binding,invocation_id:t.invocation_id},activeCancel.token);const result={...data.action_receipt,assertions_verified:false};completed.set(t.callId,{prompt:t.prompt,conversationId:t.conversationId,result,generation:t.generation});deps.publishReview(data);
        return new vscode.LanguageModelToolResult([new vscode.LanguageModelTextPart(JSON.stringify({receipt:result.id,status:result.result.status,target:t.preview.target,returncode:result.result.returncode,assertions_verified:false,note:'A zero exit status is a process observation, not verified test assertions.'}))]);
      }finally{cancel.dispose();activeCancel.dispose();activeCancel=undefined;running=false;publish();}
    }
  };
  const provider={onDidChangeLanguageModelChatInformation:changed.event,
    provideLanguageModelChatInformation(){if(!enabled())return [];const models=deps.modelSettings();return ['auto','low','mid','high'].filter(id=>id==='auto'||models[id]).map(id=>({id:MODEL_IDS[id],name:id==='auto'?'Sutra Auto':'Sutra '+id[0].toUpperCase()+id.slice(1),family:'karma',version:require('./package.json').version,maxInputTokens:64000,maxOutputTokens:4096,detail:id==='auto'?'Governed local routing':models[id]||'Configure this tier',tooltip:'Chat normally; arm Sutra for workspace reading. Every local call uses the invocation gateway.',capabilities:{toolCalling:true,imageInput:false}}));},
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
        const guidance=arming?armingStep:!enabled()?'Enable native chat in Sutra Settings, then arm a task.':'Choose Arm task in Sutra, or run **Sutra: Arm Workspace Access** from the Command Palette. Confirm the task and scope, then resend your request.';
        progress.report(new vscode.LanguageModelTextPart('You are connected to Sutra. '+guidance+'\n\nSetup guidance only — no model call, source read, edit, or test was performed.'));
        return;
      }
      if(running||deps.isBusy())throw new Error('A Sutra request is running. Wait before switching models.');
      const request=latestRequest(vscode,messages);
      if(!request.results){const saved=memory.resolve(vscode,messages,options,request.parent);request.parent=saved.parent;audit.memorySource=saved.source;}
      let prompt=request.prompt,conversationId;
      if(request.results){assertArmed();if(request.results.length!==1)throw new Error('Unexpected tool results for Sutra.');const known=completed.get(request.results[0].callId);if(!known||known.generation!==armed.generation)throw new Error('Tool result lacks a current Sutra host receipt.');if(!['applied','process_exited_zero','process_failed'].includes(known.result.result.status)){progress.report(new vscode.LanguageModelTextPart('Sutra stopped after '+known.result.result.status+'. Inspect the host action receipt before continuing.'));return;}prompt=known.prompt;conversationId=known.conversationId;}
      let a=armed;
      const tier=Object.keys(MODEL_IDS).find(key=>MODEL_IDS[key]===model.id);
      if(!tier)throw new Error('Unknown Sutra tier');
      const tools=options.tools||[],canAct=tools.some(t=>t.name===ACTION);
      if(options.toolMode===vscode.LanguageModelChatToolMode.Required&&!canAct)throw new Error('Required tool mode does not include the Sutra action tool.');
      let responseUsage={calls:[]},footerId,footerSignature;
      const recordUsage=result=>{responseUsage=tokens.merge(responseUsage,result?.token_usage);audit.token_usage=responseUsage;};
      running=true;activeCancel=new vscode.CancellationTokenSource();const cancellation=token.onCancellationRequested(()=>activeCancel?.cancel());publish();
      const activityToken=activeCancel.token;let activityOpen=true;
      activityProgress=e=>{
        if(!activityOpen||activityToken.isCancellationRequested)return;
        // Optional host capability: no proposed-API flags required. Older hosts
        // retain the same diagnostic stream in Sutra Monitor.
        if(typeof vscode.LanguageModelThinkingPart!=='function')return;
        let text='';
        if(e.kind==='start')text=(e.role==='subagent'?'Child review':'Model call')+' · '+e.model+' · '+e.role+'\n'+(e.node_id?'Task '+e.node_id+'\n':'')+'Unverified local-model reasoning.\n';
        else if(e.kind==='thinking')text=e.text;
        else if(e.kind==='finish')text='\nProtocol: '+e.status+' · '+(e.usage?.input_tokens??'?')+' input / '+(e.usage?.output_tokens??'?')+' output tokens (reasoning included).'+(e.display_truncated?' Display shortened; full generation still charged.':'')+'\n';
        if(text)progress.report(new vscode.LanguageModelThinkingPart(text,e.id));
      };
      try{
        let data,flow,restartReview=false;
        if(!request.results){
          let pending=context.workspaceState.get(pendingKey);
          if(!pending){const saved=await flowEvent(null,'pending',tier,activeCancel.token);if(saved.flow_id)pending={...saved,workspace:deps.workspace()};}
          if(pending?.workspace===deps.workspace()){
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
              recoveryChoice=undefined;flow=old;prompt=old.prompt;restartReview=true;
              conversationId=undefined;footerId=undefined;footerSignature=undefined;
            }else {
              const choice=recoveryChoice===old.flow_id||/^(continue|resume|please continue|please resume)[.! ]*$/i.test(prompt)?'Resume saved request':await vscode.window.showWarningMessage('A Sutra request is saved.',{modal:true,detail:'Saved request: '+old.prompt+'\nResume preserves its context and remaining budget. Discard releases unused child reservations; consumed budgets and receipts remain.'},'Resume saved request','Discard and use new request');
              if(activeCancel.token.isCancellationRequested)return;
              recoveryChoice=undefined;
              if(choice==='Resume saved request'){
                flow=old;prompt=old.prompt;conversationId=old.conversation_id;audit.memoryUsed=true;audit.conversationInvocationId=conversationId;
                if(conversationId){footerSignature=await memory.remember(conversationId,options);footerId=conversationId;}
                progress.report(new vscode.LanguageModelTextPart('Resuming the saved Sutra workflow with its context and remaining budget.\n\n'));
              }else if(choice==='Discard and use new request')await flowEvent(old,'cancel',tier,activeCancel.token);
              else {progress.report(new vscode.LanguageModelTextPart('Saved request retained. Use Resume or Discard in Sutra Monitor when ready.'));return;}
            }
          }
          if(!flow)flow=await flowEvent(null,'start',tier,activeCancel.token,{prompt,parent:request.parent});
          const chat=flow.conversation;
          if(chat){
          if(armed!==a||!enabled()||activeCancel.token.isCancellationRequested)throw new Error('Native request disarmed or cancelled. Response withheld.');
          const r=chat.result;recordUsage(r);lastRoute=chat.route.model;audit.model=lastRoute;audit.memoryUsed=chat.memory_used;audit.memoryAllocation=chat.memory_allocation;audit.memoryStatus=chat.memory_status;audit.modelCapacity=chat.model_capacity;audit.conversationBudget=chat.budget;audit.invocationId=r.invocation_id;audit.stage=r.admission.admitted?'conversation':'withheld';publish();
          if(!r.admission.admitted){progress.report(new vscode.LanguageModelTextPart('Sutra withheld the conversation response: '+r.admission.errors.join('; ')));return;}
          const p=r.accepted_output;
          if(options.toolMode===vscode.LanguageModelChatToolMode.Required&&flow.state==='DONE')throw new Error('No admitted action is available to satisfy required tool mode.');
          footerSignature=await memory.remember(r.invocation_id,options);footerId=r.invocation_id;
          // Routing prose is not an authority on host capabilities or permissions.
          // Present a host-owned transition; publish model prose for conversation.
          progress.report(new vscode.LanguageModelTextPart((flow.state!=='DONE'?'Continuing this request through Sutra’s workspace workflow.':p.answer)+'\n\n'));
          if(p.diagram){try{await deps.presentConversation?.(chat);progress.report(new vscode.LanguageModelTextPart('Diagram opened beside the chat. Use **Sutra: Open Last Diagram** to reopen it.\n\n'));}catch(error){progress.report(new vscode.LanguageModelTextPart('Diagram preview could not open: '+error.message+'\n\n'));}}
          if(flow.state==='DONE')return;
          conversationId=r.invocation_id;audit.conversationInvocationId=r.invocation_id;
          }
          if(!a){
            audit.stage='awaiting_workspace_permission';publish();
            progress.report(new vscode.LanguageModelTextPart('Waiting for the **Arm Sutra** confirmation dialog in this VS Code window. No workspace files will be read until you confirm.\n\n'));
            if(!await beginArm({audit,token:activeCancel.token})){
              audit.stage='permission_declined';progress.report(new vscode.LanguageModelTextPart('Workspace access was not granted. No source was read, edited, or tested. You can keep chatting.'));return;
            }
            a=armed;audit.stage='workspace';publish();
            progress.report(new vscode.LanguageModelTextPart('**Workspace access granted for this window.** Continuing your request; edits and tests require separate confirmation.\n\n'));
          }else{
            audit.workspaceAccess={state:'already_armed',workspace:a.workspace};publish();
            progress.report(new vscode.LanguageModelTextPart('Using workspace access already armed for this window.\n\n'));
          }

        }
        assertArmed();
        if(restartReview)flow=await flowEvent(flow,'restart_review',tier,activeCancel.token);
        if(!flow)flow=await flowEvent(null,'start_existing',tier,activeCancel.token,{prompt,conversation_id:conversationId});
        if(flow.state==='PAUSED')flow=await flowEvent(flow,'resume',tier,activeCancel.token);
        while(!['DONE','AWAITING_ACTION'].includes(flow.state)){
          if(armed!==a||activeCancel.token.isCancellationRequested)throw new Error('Native request disarmed or cancelled.');
          if(flow.recovery){audit.stage='recovery_required';audit.message=flow.recovery.message;publish();progress.report(new vscode.LanguageModelTextPart(flow.recovery.message+'\n\nSend **continue** to review the recovery options.'));return;}
          if(flow.state==='PAUSED'){
            const stop=flow.stop||{},failures=stop.failures||[];
            const details=[stop.error,...failures.map(f=>f.path+': '+f.error)].filter(Boolean);
            const coverage=Number.isInteger(stop.mapped_files)&&Number.isInteger(stop.source_files)?` Mapped ${stop.mapped_files} of ${stop.source_files} source files.`:'';
            const reason=details.length?details.join('\n'):(stop.status||'Mapping needs attention. Inspect Sutra Monitor for details.');
            audit.stage='paused';audit.message=reason;publish();
            progress.report(new vscode.LanguageModelTextPart('**Sutra paused the review.**'+coverage+'\n\n'+reason+'\n\n'+(stop.failure_count>failures.length?`${stop.failure_count-failures.length} more file failures are recorded in the review details.\n\n`:'')+'The request, context, permissions and consumed budget are saved. Resolve the reported cause, then use **Resume** in Sutra Monitor and send **continue** here. Resume keeps the remaining allocation; it does not reset a limit. Use **Discard** to abandon this request.'));
            return;
          }
          if(['QUARANTINED','CANCELLING','EXECUTING_ACTION'].includes(flow.state))throw new Error('Sutra workflow '+flow.state+'. Saved request retained. Inspect Sutra Monitor, then choose Resume or Discard.');
          let event='step',extra={};
          if(flow.state==='NEED_PERMISSION'){
            const grant=await approvePermission(flow.permission,activeCancel.token);
            if(!grant||armed!==a){audit.stage='permission_declined';progress.report(new vscode.LanguageModelTextPart('Permission was not granted. No edit or test ran.'));return;}
            event='grant';extra={grant};
          }
          flow=await flowEvent(flow,event,tier,activeCancel.token,extra);
          const bound=flow.binding||flow.bound_task;
          if(bound){a.binding=bound;a.session=bound.session;audit.session=a.session;}
          audit.orchestration={flowId:flow.flow_id,state:flow.state,children:flow.children};publish();
          if(flow.progress)progress.report(new vscode.LanguageModelTextPart(flow.progress+'\n\n'));
          if(flow.child)recordUsage(flow.child.result);
        }
        data=flow.data;
        if(!data){progress.report(new vscode.LanguageModelTextPart(flow.state==='AWAITING_ACTION'?'The saved proposal is awaiting action confirmation. Ask for a fresh proposal if its confirmation is no longer available.':'This workflow has finished. Send a new request to continue.'));return;}
        if(armed!==a||!enabled()||activeCancel.token.isCancellationRequested)throw new Error('Native request disarmed or cancelled. Proposal withheld.');
        if(data?.notice){audit.stage='guidance';progress.report(new vscode.LanguageModelTextPart(data.notice.message+'\n\nSutra setup guidance — no model invocation.'));return;}
        if(!data?.result)throw new Error(data?.notice?.message||'No governed response available.');
        deps.publishReview(data);lastRoute=data.route?.model||data.result.model||'';publish();
        const r=data.result;recordUsage(r);audit.invocationId=r.invocation_id;audit.model=lastRoute;audit.stage=r.admission.admitted?'admitted':'withheld';publish();if(!r.admission.admitted){progress.report(new vscode.LanguageModelTextPart('Sutra withheld the response: '+r.admission.errors.join('; ')));return;}
        const p=r.accepted_output;if(options.toolMode===vscode.LanguageModelChatToolMode.Required&&!['edit','run_tests'].includes(p.action?.kind))throw new Error('No admitted action is available to satisfy required tool mode.');
        progress.report(new vscode.LanguageModelTextPart(`Sutra routing confirmed · ${lastRoute} · task ${a.session}\nRequest ${audit.id} · protocol invocation ${r.invocation_id}\n\n${p.answer||p.summary}\n\n`));
        if(['edit','run_tests'].includes(p.action?.kind)){
          if(!canAct){progress.report(new vscode.LanguageModelTextPart('The proposal is retained. Enable the Sutra action tool in native Agent chat to review and apply it. No action executed.'));return;}
          const preview=await deps.request({op:'native_preview',...a.binding,invocation_id:r.invocation_id},activeCancel.token);if(armed!==a||activeCancel.token.isCancellationRequested)throw new Error('Task disarmed or cancelled during preview.');
          const id=crypto.randomUUID(),callId=crypto.randomUUID();tickets.set(id,{generation:a.generation,expires:Date.now()+300000,invocation_id:r.invocation_id,kind:p.action.kind,preview,prompt,conversationId,callId});
          if(preview.diff)await deps.showDiff(preview,r.invocation_id);
          if(armed!==a||activeCancel.token.isCancellationRequested){tickets.delete(id);throw new Error('Task disarmed or cancelled during diff review.');}
          progress.report(new vscode.LanguageModelToolCallPart(callId,ACTION,{ticket:id}));
        }else{progress.report(new vscode.LanguageModelTextPart('Remaining: '+(p.remaining?.join('; ')||'No further step proposed.')+'\n\nModel notes are not proof of correctness. Test receipts report process observations.'));}
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
    vscode.commands.registerCommand('karma.armNative',()=>beginArm()),vscode.commands.registerCommand('karma.disarmNative',()=>disarm()),
    vscode.commands.registerCommand('karma.toggleNative',async()=>{disarm('Native chat setting changed.');await vscode.workspace.getConfiguration('karma').update('nativeEnabled',!enabled(),vscode.ConfigurationTarget.Global);changed.fire();publish();}));
  const listener=vscode.workspace.onDidChangeConfiguration?.(e=>{if(e.affectsConfiguration('karma.nativeEnabled')){disarm('Native chat setting changed.');changed.fire();}else if(e.affectsConfiguration('karma.models'))changed.fire();if(e.affectsConfiguration('chat'))publish();});if(listener)context.subscriptions.push(listener);
  // Populate VS Code's model registry at activation, including after reload.
  // Discovery reads this provider's metadata only; it never starts inference.
  if(vscode.lm.selectChatModels)Promise.resolve(vscode.lm.selectChatModels({vendor:'karma'})).catch(error=>{reason='Native model discovery: '+error.message;publish();});
  publish();return {status,publish,disarm,dispose(){disarm('Extension stopped.');}};
}
module.exports={install,latestRequest};

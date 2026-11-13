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
    return {enabled:enabled(),arming,armingStep,selecting,armed:!!armed&&enabled(),working:running||live,session:armed?.session,scope:'Sutra model requests and Sutra action tool only',defaultModel:chat.get('defaultModel',''),defaultsReady,reason,lastRoute,label,explanation,routing:routing?{...routing}:null,compaction:lastCompaction};
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
    try{
      const yes=await vscode.window.showWarningMessage('Arm Sutra for this workspace?',{modal:true,detail:workspace+'\nSutra may read project files for your requests. Existing task scope and permissions are retained. Edits and tests require specific permissions and action confirmation. Network access and new dependencies remain off.\n'+(owner?'Your pending request will continue in this chat.':'Your current chat stays open.')},'Arm Sutra');
      stillValid();
      if(yes!=='Arm Sutra')return false;
      const st=await deps.request({op:'state'},owner?.token);
      stillValid();
      const current=st.auto||st.contract?.contract.constraints.native_task;
      armed={workspace,generation:crypto.randomUUID(),session:current?st.session:undefined,binding:current?identity(st):undefined,coding:!!(current&&st.contract.contract.constraints.native_task)};
      reason='Armed for workspace reading. Describe the work in chat; Sutra asks for additional permissions when needed.';
      success=true;
      if(!owner)void vscode.window.showInformationMessage('Sutra is armed. Your current chat is unchanged. Use Sutra Auto for governed requests.');
      return true;
    }finally{arming=false;armingStep='';if(!success&&epoch===accessEpoch)reason='Arming cancelled. Conversation remains available.';publish();}
  }
  async function prepareWorkspace(a,prompt,conversationId,permission,token){
    if(armed!==a||token.isCancellationRequested)throw new Error('Sutra disarmed or cancelled.');
    let st;
    if(a.binding)st=await check(token);else{
      st=await deps.request({op:'state'},token);
      if(!st.auto&&!st.contract?.contract.constraints.native_task){
        st=await deps.request({op:'auto_new',goal:prompt.trim(),roots:['.'],conversation_id:conversationId},token);
      }
      if(armed!==a||token.isCancellationRequested)throw new Error('Sutra disarmed or cancelled.');
      a.binding=identity(st);a.session=st.session;a.coding=!!st.contract.contract.constraints.native_task;
    }
    const k=st.contract.contract.constraints,roots=st.read_scope||k.allowed_paths,operation=permission?.operation||'read';
    const requested=Array.isArray(permission?.paths)?permission.paths:[];
    const missingPaths=a.coding&&requested.some(name=>!k.allowed_paths.includes(name));
    const needed=(operation==='edit'?!k.edits_allowed:operation==='test'?!k.tests_allowed:false)||((operation==='edit'||operation==='test')&&missingPaths);
    const outside=requested.some(name=>!roots.some(root=>root==='.'||name===root||name.startsWith(root+'/')));
    if(outside)throw new Error('The requested files are outside the current task scope. Use task controls to explicitly change scope.');
    if(!needed)return true;
    let paths=requested;
    if(!paths.length){
      const chosen=await vscode.window.showOpenDialog({title:'Select Python files needed for this '+operation,defaultUri:vscode.Uri.file(deps.workspace()),canSelectMany:true,filters:{Python:['py']}});
      if(!chosen?.length)return false;
      paths=chosen.map(f=>path.relative(deps.workspace(),f.fsPath).split(path.sep).join('/'));
    }
    if(a.coding)paths=[...new Set([...k.allowed_paths,...paths])];
    if(paths.length>8||paths.some(n=>n.startsWith('../')||path.isAbsolute(n)||!n.endsWith('.py')||n.includes('\\')||!roots.some(root=>root==='.'||n===root||n.startsWith(root+'/'))))throw new Error('Choose up to eight existing Python files within the current task scope.');
    const tests=operation==='test'?paths.filter(n=>/(^|\/)test[^/]*\.py$/.test(n)):[];
    if(operation==='test'&&!tests.length)throw new Error('Select a Python unittest file named test*.py.');
    const label=operation==='edit'?'Allow scoped edits':'Allow scoped tests';
    const yes=await vscode.window.showWarningMessage('Sutra needs permission for this request.',{modal:true,detail:(operation==='edit'?'Edit scope: ':'Test scope: ')+paths.join(', ')+'\nTask: '+st.contract.contract.goal+'\nNetwork and dependencies remain off. Every proposed edit/test still gets its own confirmation.'},label);
    if(yes!==label||armed!==a||token.isCancellationRequested)return false;
    st=await deps.request({op:'native_permissions',...a.binding,operation,paths,test_paths:tests,conversation_id:conversationId},token);
    if(armed!==a||token.isCancellationRequested)throw new Error('Sutra disarmed or cancelled during permission update.');
    tickets.clear();completed.clear();a.generation=crypto.randomUUID();a.binding=identity(st);a.session=st.session;a.coding=true;publish();
    return true;
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
    }catch(error){audit.stage=token.isCancellationRequested||/cancel|disarm/i.test(error.message)?'cancelled':'blocked';audit.message=error.message;throw error;}
    finally{audit.active=false;audit.finishedAt=new Date().toISOString();publish();deps.onRoute?.({...audit});}
  }
  function ticket(input){const t=tickets.get(input?.ticket),a=assertArmed();if(!t||t.generation!==a.generation||Date.now()>t.expires)throw new Error('Missing, expired or revoked Sutra action ticket.');return t;}
  const tool={
    prepareInvocation(options){const t=ticket(options.input);const md=new vscode.MarkdownString();md.appendText(t.kind==='edit'?'Apply this exact proposed edit to '+t.preview.target+'?':'Run the permitted Python test '+t.preview.target+' in a read-only snapshot with network denied?');if(t.preview.diff)md.appendCodeblock(t.preview.diff,'diff');md.isTrusted=false;
      return {invocationMessage:t.kind==='edit'?'Applying governed edit':'Running governed test',confirmationMessages:{title:'Sutra: '+(t.kind==='edit'?'apply edit':'run test'),message:md}};},
    async invoke(options,token){
      if(running||deps.isBusy())throw new Error('A Sutra operation is already running.');
      if(token.isCancellationRequested)throw new Error('Sutra action cancelled.');
      const t=ticket(options.input);await check(token);if(ticket(options.input)!==t)throw new Error('Action revoked.');
      // VS Code may skip prepareInvocation confirmations for programmatic calls
      // without a chat token. Require a host dialog for that path as well.
      if(!options.toolInvocationToken){
        const label=t.kind==='edit'?'Apply edit':'Run test';
        const confirmed=await vscode.window.showWarningMessage('Sutra: '+label+' · '+t.preview.target,{modal:true,detail:t.preview.diff||'Run the permitted Python unittest in a read-only snapshot with network denied. Exit status is a process observation; assertions remain unverified.'},label);
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
    provideLanguageModelChatInformation(){if(!enabled())return [];const models=deps.modelSettings();return ['auto','low','mid','high'].map(id=>({id:MODEL_IDS[id],name:id==='auto'?'Sutra Auto':'Sutra '+id[0].toUpperCase()+id.slice(1),family:'karma',version:'0.19.1',maxInputTokens:64000,maxOutputTokens:4096,detail:id==='auto'?'Governed local routing':models[id]||'Configure this tier',tooltip:'Chat normally; arm Sutra for workspace reading. Every local call uses the invocation gateway.',capabilities:{toolCalling:true,imageInput:false}}));},
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
      try{
        let data;
        if(!request.results){
          const models=await deps.configureModels(false,activeCancel.token);
          const chat=await deps.request({op:'conversation',prompt,parent:request.parent,models,tier},activeCancel.token);
          if(armed!==a||!enabled()||activeCancel.token.isCancellationRequested)throw new Error('Native request disarmed or cancelled. Response withheld.');
          const r=chat.result;recordUsage(r);lastRoute=chat.route.model;audit.model=lastRoute;audit.memoryUsed=chat.memory_used;audit.memoryAllocation=chat.memory_allocation;audit.memoryStatus=chat.memory_status;audit.modelCapacity=chat.model_capacity;audit.conversationBudget=chat.budget;audit.invocationId=r.invocation_id;audit.stage=r.admission.admitted?'conversation':'withheld';publish();
          if(!r.admission.admitted){progress.report(new vscode.LanguageModelTextPart('Sutra withheld the conversation response: '+r.admission.errors.join('; ')));return;}
          const p=r.accepted_output;
          if(options.toolMode===vscode.LanguageModelChatToolMode.Required&&p.intent!=='workspace')throw new Error('No admitted action is available to satisfy required tool mode.');
          footerSignature=await memory.remember(r.invocation_id,options);footerId=r.invocation_id;
          // Routing prose is not an authority on host capabilities or permissions.
          // Present a host-owned transition; publish model prose for conversation.
          progress.report(new vscode.LanguageModelTextPart((p.intent==='workspace'?'Continuing this request through Sutra’s workspace workflow.':p.answer)+'\n\n'));
          if(p.diagram){try{await deps.presentConversation?.(chat);progress.report(new vscode.LanguageModelTextPart('Diagram opened beside the chat. Use **Sutra: Open Last Diagram** to reopen it.\n\n'));}catch(error){progress.report(new vscode.LanguageModelTextPart('Diagram preview could not open: '+error.message+'\n\n'));}}
          if(p.intent!=='workspace')return;
          if(!a){
            audit.stage='awaiting_workspace_permission';publish();
            progress.report(new vscode.LanguageModelTextPart('Workspace access needs your confirmation. Choose **Arm Sutra** to continue this request here.\n\n'));
            if(!await beginArm({audit,token:activeCancel.token})){
              audit.stage='permission_declined';progress.report(new vscode.LanguageModelTextPart('Workspace access was not granted. No source was read, edited, or tested. You can keep chatting.'));return;
            }
            a=armed;audit.stage='workspace';publish();
          }
          conversationId=r.invocation_id;audit.conversationInvocationId=r.invocation_id;
          if(!await prepareWorkspace(a,prompt,conversationId,p.workspace_request,activeCancel.token)){audit.stage='permission_declined';progress.report(new vscode.LanguageModelTextPart('Permission was not granted. Sutra remains armed for reading; no edit or test ran.'));return;}
          progress.report(new vscode.LanguageModelTextPart('Checking the armed workspace task before accessing source…\n\n'));
        }
        assertArmed();audit.session=a.session;await check(activeCancel.token);
        if(armed!==a||activeCancel.token.isCancellationRequested)throw new Error('Native request disarmed or cancelled.');
        if(a.coding){const models=await deps.configureModels(false,activeCancel.token);data=await deps.request({op:'native_work',...a.binding,prompt,models,tier,conversation_id:conversationId},activeCancel.token);}
        else data=await deps.simpleChat(prompt,{tier,token:activeCancel.token,conversationId});
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
        cancellation.dispose();activeCancel.dispose();activeCancel=undefined;running=false;publish();
      }
      });
    }
  };
  context.subscriptions.push(changed,vscode.lm.registerLanguageModelChatProvider('karma',provider),vscode.lm.registerTool(ACTION,tool),
    vscode.commands.registerCommand('karma.useNativeChat',()=>useNativeChat()),
    vscode.commands.registerCommand('karma.armNative',()=>beginArm()),vscode.commands.registerCommand('karma.disarmNative',()=>disarm()),
    vscode.commands.registerCommand('karma.toggleNative',async()=>{disarm('Native chat setting changed.');await vscode.workspace.getConfiguration('karma').update('nativeEnabled',!enabled(),vscode.ConfigurationTarget.Global);changed.fire();publish();}));
  const listener=vscode.workspace.onDidChangeConfiguration?.(e=>{if(e.affectsConfiguration('karma.nativeEnabled')){disarm('Native chat setting changed.');changed.fire();}else if(e.affectsConfiguration('karma.models'))changed.fire();if(e.affectsConfiguration('chat'))publish();});if(listener)context.subscriptions.push(listener);
  // Populate VS Code's model registry at activation, including after reload.
  // Discovery reads this provider's metadata only; it never starts inference.
  if(vscode.lm.selectChatModels)Promise.resolve(vscode.lm.selectChatModels({vendor:'karma'})).catch(error=>{reason='Native model discovery: '+error.message;publish();});
  publish();return {status,publish,disarm,dispose(){disarm('Extension stopped.');}};
}
module.exports={install,latestRequest};

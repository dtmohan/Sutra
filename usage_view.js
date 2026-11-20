'use strict';
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const tokens=require('./media/token_usage');
const {ActivityArchive,activityUri,parseActivityUri}=require('./model_activity');
const coherence=require('./media/coherence');

function currentArchitectureStop(runs){
  const event=runs?.[0]?.recent_events?.at(-1),stop=event?.data?.architecture_stop;
  if(!stop||typeof stop.message!=='string'||typeof stop.terminal!=='boolean')return undefined;
  const validCount=value=>Number.isSafeInteger(value)&&value>=0;
  const budget=stop.repository_budget;
  return {
    at:Number.isFinite(event.at)?event.at:undefined,run_id:typeof stop.run_id==='string'?stop.run_id:undefined,
    message:stop.message.slice(0,1200),terminal:stop.terminal,
    canRead:stop.reason==='insufficient_evidence'&&!stop.terminal,
    covered:validCount(stop.covered_files)?stop.covered_files:undefined,
    required:validCount(stop.required_files)?stop.required_files:undefined,
    remaining:validCount(budget?.used)&&validCount(budget?.limit)?Math.max(0,budget.limit-budget.used):undefined
  };
}

// Diagnostics are read-only. Recovery controls delegate to validating host commands.
function install(vscode,context,deps){
  let view,ready=false,snapshot,routing,native,error,activity;
  const currentActivity=()=>{
    let directory;try{directory=deps.storage();}catch{return undefined;}
    if(activity?.directory!==directory){activity?.dispose();activity=new ActivityArchive(directory);}
    activity.prune();return activity;
  };
  const item=vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right,19);
  item.name='Sutra Monitor';item.text='$(dashboard) Sutra Monitor';item.command='karma.openMonitor';
  item.tooltip='Open tokens, system telemetry and kernel decisions';item.show();
  const publish=()=>{
    const a=currentActivity();
    if(!ready)return;
    const config=vscode.workspace.getConfiguration('karma');
    view?.webview.postMessage({type:'usage',snapshot,routing,native,error,
      reasoningPreference:config.get('thinking','adaptive'),tierReasoning:config.get('tierReasoning',{}),
      activity:a?.calls||[],activityError:a?.error||''});
  };
  const link=id=>{const a=currentActivity();return a?activityUri(a.directory,id):null;};
  const openDetails=async id=>{
    const uri=link(id);if(!uri)return;
    const doc=await vscode.workspace.openTextDocument(vscode.Uri.parse(uri));
    await vscode.window.showTextDocument(doc,{viewColumn:vscode.ViewColumn.Beside,preview:false,preserveFocus:false});
  };
  const refresh=()=>deps.refresh();
  const provider={resolveWebviewView(current){
    view=current;ready=false;
    const media=vscode.Uri.file(path.join(context.extensionPath,'media'));
    current.webview.options={enableScripts:true,localResourceRoots:[media]};
    const nonce=crypto.randomBytes(18).toString('hex');
    current.webview.html=fs.readFileSync(path.join(context.extensionPath,'media','usage.html'),'utf8')
      .replaceAll('NONCE_VALUE',nonce)
      .replace('TOKENS_URI',current.webview.asWebviewUri(vscode.Uri.file(path.join(media.fsPath,'token_usage.js'))).toString())
      .replace('COHERENCE_URI',current.webview.asWebviewUri(vscode.Uri.file(path.join(media.fsPath,'coherence.js'))).toString())
      .replace('PROGRESS_URI',current.webview.asWebviewUri(vscode.Uri.file(path.join(media.fsPath,'generation_progress.js'))).toString())
      .replace('SCRIPT_URI',current.webview.asWebviewUri(vscode.Uri.file(path.join(media.fsPath,'usage.js'))).toString());
    current.webview.onDidReceiveMessage(message=>{
      if(message?.type==='ready'){ready=true;publish();void refresh();}
      else if(message?.type==='tools')void vscode.commands.executeCommand('karma.manageTools');
      else if(message?.type==='tool_policy')void vscode.commands.executeCommand('karma.configureTools');
      else if(message?.type==='refresh')void refresh();
      else if(message?.type==='open_tasks'&&!native?.working)void vscode.commands.executeCommand('karma.openTasks');
      else if(message?.type==='resume')void vscode.commands.executeCommand('karma.resumeWorkflow');
      else if(message?.type==='stop_runner')void vscode.commands.executeCommand('karma.stopRunner',message.id);
      else if(message?.type==='dismiss_runner')void vscode.commands.executeCommand('karma.dismissRunnerResult',message.id);
      else if(message?.type==='workspace_access'&&native?.permissionCanChoose)
        void vscode.commands.executeCommand('karma.armNative');
      else if(message?.type==='discard')void vscode.commands.executeCommand('karma.discardWorkflow');
      else if(message?.type==='increase_allocation')void vscode.commands.executeCommand('karma.increaseAllocation');
      else if(message?.type==='architecture_read_and_retry'){
        const stop=snapshot?.monitor?.architecture_stop;
        if(!native?.working&&stop?.canRead&&stop.run_id&&message.run_id===stop.run_id)
          void vscode.commands.executeCommand('karma.architectureReadAndRetry',stop.run_id);
      }
      else if(message?.type==='telemetry')void vscode.commands.executeCommand('karma.open');
      else if(message?.type==='reasoning_settings')void vscode.commands.executeCommand('karma.configureReasoning');
      else if(message?.type==='activity_details')void openDetails(message.id);
    },null,context.subscriptions);
    current.onDidChangeVisibility(()=>{if(current.visible)void refresh();},null,context.subscriptions);
    current.onDidDispose(()=>{if(view===current){view=undefined;ready=false;}},null,context.subscriptions);
  }};
  context.subscriptions.push(item,
    vscode.workspace.onDidChangeConfiguration(e=>{if(e.affectsConfiguration('karma.thinking')||e.affectsConfiguration('karma.tierReasoning'))publish();}),
    vscode.window.registerWebviewViewProvider('karma.usage',provider),
    vscode.commands.registerCommand('karma.openMonitor',()=>vscode.commands.executeCommand('karma.usage.focus')),
    vscode.commands.registerCommand('karma.openUsage',()=>vscode.commands.executeCommand('karma.usage.focus')),
    vscode.commands.registerCommand('karma.refreshUsage',refresh),{dispose(){activity?.dispose();}});
  return {
    get visible(){return !!view?.visible;},
    setData(data,workspace){
      const monitor={};
      // Monitoring and bounded, unverified task observations only. Observations
      // include the original request; source and executable action payloads stay out.
      for(const key of ['runner','logical_memory','ram_compartments','task_observations','runtime_memory','orchestration','hardware','kernel','review_budget','repository','action_budget','storage','context','adapter_health','session','contract','diagnostic_errors'])monitor[key]=data[key];
      monitor.coherence_observations=coherence.recent(data.coherence_observations);
      monitor.architecture_stop=currentArchitectureStop(data.runs);
      snapshot={...(data.token_usage||{calls:[],total_calls:0,limit:250}),monitor,workspace,updatedAt:new Date().toISOString()};error=undefined;publish();
    },
    activity(event){
      const a=currentActivity();if(!a)return;a.accept(event);
      const uri=link(event.id);if(uri)deps.activityChanged?.(vscode.Uri.parse(uri));publish();
    },
    activityLink:event=>link(event.id),
    activityDocument(uri){
      const parsed=parseActivityUri(uri),a=currentActivity();
      if(!parsed||!a||path.basename(a.directory)!==parsed.workspace)return 'Reasoning details belong to a different workspace. Open the original workspace to inspect this call.';
      return a.document(parsed.id);
    },
    setError(message){error=message;publish();},
    setStatus(value){
      const r=value?.routing;
      routing=r?{active:!!r.active,workspaceAccess:r.workspaceAccess,stage:r.stage,progress:r.progress,model:r.model,memoryUsed:r.memoryUsed,memorySource:r.memorySource,memoryAllocation:r.memoryAllocation,memoryStatus:r.memoryStatus,modelCapacity:r.modelCapacity,conversationBudget:r.conversationBudget,invocationId:r.invocationId}:undefined;
      native=value?{enabled:value.enabled,armed:value.armed,accessProfile:value.accessProfile,label:value.label,
        permissionPending:value.permissionPending,permissionCanChoose:value.permissionCanChoose,
        compaction:value.compaction,pending:value.pending,working:value.working,openTaskCount:value.openTaskCount}:undefined;publish();
    },
    recordResponse(group){if(group?.calls?.length)item.tooltip='Latest completed stage / response: '+tokens.compact(group)+'\nClick for workspace history and per-stage counts.';},
    reset(){snapshot=undefined;routing=undefined;native=undefined;error=undefined;item.tooltip='Open tokens, system telemetry and kernel decisions';publish();}
  };
}
module.exports={install};

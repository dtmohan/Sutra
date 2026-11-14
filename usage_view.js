'use strict';
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const tokens=require('./media/token_usage');
const {ActivityLog}=require('./model_activity');

// A read-only view of existing receipts. This surface cannot dispatch models or tools.
function install(vscode,context,deps){
  let view,ready=false,snapshot,routing,native,error;const activity=new ActivityLog();
  const item=vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right,19);
  item.name='Sutra Monitor';item.text='$(dashboard) Sutra Monitor';item.command='karma.openMonitor';
  item.tooltip='Open tokens, system telemetry and kernel decisions';item.show();
  const publish=()=>{if(ready)view?.webview.postMessage({type:'usage',snapshot,routing,native,error,activity:activity.calls});};
  const refresh=()=>deps.refresh();
  const provider={resolveWebviewView(current){
    view=current;ready=false;
    const media=vscode.Uri.file(path.join(context.extensionPath,'media'));
    current.webview.options={enableScripts:true,localResourceRoots:[media]};
    const nonce=crypto.randomBytes(18).toString('hex');
    current.webview.html=fs.readFileSync(path.join(context.extensionPath,'media','usage.html'),'utf8')
      .replaceAll('NONCE_VALUE',nonce)
      .replace('TOKENS_URI',current.webview.asWebviewUri(vscode.Uri.file(path.join(media.fsPath,'token_usage.js'))).toString())
      .replace('SCRIPT_URI',current.webview.asWebviewUri(vscode.Uri.file(path.join(media.fsPath,'usage.js'))).toString());
    current.webview.onDidReceiveMessage(message=>{
      if(message?.type==='ready'){ready=true;publish();void refresh();}
      else if(message?.type==='refresh')void refresh();
      else if(message?.type==='resume')void vscode.commands.executeCommand('karma.resumeWorkflow');
      else if(message?.type==='discard')void vscode.commands.executeCommand('karma.discardWorkflow');
      else if(message?.type==='telemetry')void vscode.commands.executeCommand('karma.open');
    },null,context.subscriptions);
    current.onDidChangeVisibility(()=>{if(current.visible)void refresh();},null,context.subscriptions);
    current.onDidDispose(()=>{if(view===current){view=undefined;ready=false;}},null,context.subscriptions);
  }};
  context.subscriptions.push(item,
    vscode.window.registerWebviewViewProvider('karma.usage',provider),
    vscode.commands.registerCommand('karma.openMonitor',()=>vscode.commands.executeCommand('karma.usage.focus')),
    vscode.commands.registerCommand('karma.openUsage',()=>vscode.commands.executeCommand('karma.usage.focus')),
    vscode.commands.registerCommand('karma.refreshUsage',refresh));
  return {
    get visible(){return !!view?.visible;},
    setData(data,workspace){
      const monitor={};
      // Only monitoring fields cross into the view; no source, prompt or action payloads.
      for(const key of ['orchestration','hardware','kernel','review_budget','repository','action_budget','storage','context','adapter_health','session','contract'])monitor[key]=data[key];
      snapshot={...(data.token_usage||{calls:[],total_calls:0,limit:250}),monitor,workspace,updatedAt:new Date().toISOString()};error=undefined;publish();
    },
    activity(event){activity.accept(event);publish();},
    setError(message){error=message;publish();},
    setStatus(value){
      const r=value?.routing;
      routing=r?{active:!!r.active,workspaceAccess:r.workspaceAccess,stage:r.stage,model:r.model,memoryUsed:r.memoryUsed,memorySource:r.memorySource,memoryAllocation:r.memoryAllocation,memoryStatus:r.memoryStatus,modelCapacity:r.modelCapacity,conversationBudget:r.conversationBudget,invocationId:r.invocationId}:undefined;
      native=value?{enabled:value.enabled,armed:value.armed,label:value.label,compaction:value.compaction,pending:value.pending,working:value.working}:undefined;publish();
    },
    recordResponse(group){if(group?.calls?.length)item.tooltip='Latest completed stage / response: '+tokens.compact(group)+'\nClick for workspace history and per-stage counts.';},
    reset(){activity.clear();snapshot=undefined;routing=undefined;native=undefined;error=undefined;item.tooltip='Open tokens, system telemetry and kernel decisions';publish();}
  };
}
module.exports={install};

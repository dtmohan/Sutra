'use strict';
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const tokens=require('./media/token_usage');

// A read-only view of existing receipts. This surface cannot dispatch models or tools.
function install(vscode,context,deps){
  let view,ready=false,snapshot,routing,error;
  const item=vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right,19);
  item.name='Sutra Token Usage';item.text='$(graph) Sutra Usage';item.command='karma.openUsage';
  item.tooltip='Open the token dashboard beside Terminal and Output';item.show();
  const publish=()=>{if(ready)view?.webview.postMessage({type:'usage',snapshot,routing,error});};
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
      else if(message?.type==='telemetry')void vscode.commands.executeCommand('karma.open');
    },null,context.subscriptions);
    current.onDidChangeVisibility(()=>{if(current.visible)void refresh();},null,context.subscriptions);
    current.onDidDispose(()=>{if(view===current){view=undefined;ready=false;}},null,context.subscriptions);
  }};
  context.subscriptions.push(item,
    vscode.window.registerWebviewViewProvider('karma.usage',provider),
    vscode.commands.registerCommand('karma.openUsage',()=>vscode.commands.executeCommand('karma.usage.focus')),
    vscode.commands.registerCommand('karma.refreshUsage',refresh));
  return {
    get visible(){return !!view?.visible;},
    setData(data,workspace){snapshot={...data,workspace,updatedAt:new Date().toISOString()};error=undefined;publish();},
    setError(message){error=message;publish();},
    setRouting(value){routing=value?{active:!!value.active,stage:value.stage,model:value.model}:undefined;publish();},
    recordResponse(group){if(group?.calls?.length)item.tooltip='Latest completed stage / response: '+tokens.compact(group)+'\nClick for workspace history and per-stage counts.';},
    reset(){snapshot=undefined;routing=undefined;error=undefined;item.tooltip='Open the token dashboard beside Terminal and Output';publish();}
  };
}
module.exports={install};

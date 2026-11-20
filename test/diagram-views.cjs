'use strict';
const assert=require('node:assert/strict'),path=require('node:path');
const {install}=require('../diagram_view');
const commands=new Map(),panels=[],state=new Map(),errors=[];
const uri=fsPath=>({fsPath,toString:()=>String(fsPath)});
const vscode={Uri:{file:uri},ViewColumn:{Beside:2},commands:{registerCommand(name,fn){commands.set(name,fn);return{dispose(){}};}},
 window:{showInformationMessage(){},showErrorMessage(e){errors.push(e);},createWebviewPanel(type,title){
  const messages=[],webview={cspSource:'vscode-webview:',asWebviewUri:u=>u,
   onDidReceiveMessage(fn){this.receive=fn;return{dispose(){}};},async postMessage(m){messages.push(m);}};
  const p={title,webview,messages,dispose(){}};panels.push(p);return p;
 }},env:{clipboard:{async writeText(text){vscode.copied=text;}}}};
const context={extensionPath:path.resolve(__dirname,'..'),subscriptions:[],workspaceState:{get:k=>state.get(k),async update(k,v){state.set(k,v);}}};
const flow={title:'Review flow',direction:'TB',note:'Model explanation, not measured calls.',nodes:[{id:'n1',label:'Receive request',kind:'source'},{id:'n2',label:'Return result',kind:'output'}],edges:[{source:'n1',target:'n2',label:'next described step'}]};
const source={...flow,title:'Local source dependencies',direction:'LR',note:'Import syntax, not execution order.',edges:[{source:'n1',target:'n2',label:'local import'}]};
(async()=>{
 const view=install(vscode,context,()=>{throw Error('No conversation read expected');});
 const p=await view.openReview({review_flow_diagram:flow,review_diagram:source,result:{invocation_id:'a'.repeat(32)}});
 await p.webview.receive({type:'ready'});
 assert.deepEqual(p.messages.at(-1).views,['Review flow','Local source dependencies']);
 assert.equal(p.messages.at(-1).provenance,'review-flow');
 assert(p.webview.html.includes('aria-label="Diagram view"'));
 await p.webview.receive({type:'view',index:1});
 assert.equal(p.title,source.title);assert.equal(p.messages.at(-1).provenance,'source');
 await p.webview.receive({type:'copy',direction:'TB'});assert(vscode.copied.includes('local import'));
 const count=p.messages.length;
 for(const index of [-1,2,0.5,'1'])await p.webview.receive({type:'view',index});
 assert.equal(p.messages.length,count);assert.equal(errors.length,4);
 await commands.get('karma.openLastDiagram')();
 const restored=panels.at(-1);await restored.webview.receive({type:'ready'});
 assert.equal(restored.messages.at(-1).selected,1);assert.equal(restored.messages.at(-1).diagram.title,source.title);
 const legacy=await view.openReview({review_diagram:source,result:{invocation_id:'b'.repeat(32)}});
 await legacy.webview.receive({type:'ready'});assert.deepEqual(legacy.messages.at(-1).views,[source.title]);assert.equal(legacy.messages.at(-1).provenance,'source');
 console.log('Diagram views: selection, provenance, copy, reload and invalid indices passed');
})().catch(e=>{console.error(e);process.exitCode=1;});

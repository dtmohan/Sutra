'use strict';
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
function validate(d){
  const string=(s,n)=>typeof s==='string'&&s.length<=n&&!/[\x00-\x1f\x7f]/.test(s);
  if(!d||!string(d.title,100)||!string(d.note,240)||!['LR','TB'].includes(d.direction)||!Array.isArray(d.nodes)||!d.nodes.length||d.nodes.length>16||!Array.isArray(d.edges)||d.edges.length>24)throw Error('Invalid diagram');
  const ids=new Set();for(const n of d.nodes){if(!/^n\d{1,3}$/.test(n.id)||ids.has(n.id)||!string(n.label,100)||!n.label||!['source','device','process','output'].includes(n.kind))throw Error('Invalid diagram node');ids.add(n.id);}
  for(const e of d.edges)if(!ids.has(e.source)||!ids.has(e.target)||!string(e.label,60))throw Error('Invalid diagram edge');
  return d;
}
const label=s=>[...s].map(c=>/[\p{L}\p{N} .,/:_\-]/u.test(c)?c:'#'+c.codePointAt(0)+';').join('');
function mermaidSource(d,direction){
  validate(d);const dir=direction||d.direction;if(!['LR','TB'].includes(dir))throw Error('Invalid diagram direction');
  return ['flowchart '+dir,...d.nodes.map(n=>`  ${n.id}["${label(n.label)}"]:::${n.kind}`),...d.edges.map(e=>`  ${e.source} -->${e.label?'|"'+label(e.label)+'"|':''} ${e.target}`),
    '  classDef source fill:#e7edff,stroke:#536cb8,color:#152347,stroke-width:1.5px',
    '  classDef device fill:#ffffff,stroke:#9caac3,color:#17263e,stroke-width:1.5px',
    '  classDef process fill:#eef2f8,stroke:#7b8fae,color:#17263e,stroke-width:1.5px',
    '  classDef output fill:#e1f3ed,stroke:#398775,color:#123b32,stroke-width:1.5px'].join('\n');
}
function safeSVG(svg){
  if(typeof svg!=='string'||Buffer.byteLength(svg)>2000000||!/^<svg\b/.test(svg)||/<(?:script|foreignObject|iframe|image|use)\b|<!DOCTYPE|<!ENTITY|\son[a-z]+\s*=|(?:href|src)\s*=\s*["'](?!#)|url\(\s*["']?(?!#)[^)]|@import/i.test(svg))throw Error('Unsafe or oversized SVG export');
  return svg;
}
function install(vscode,context,read){
  let last=context.workspaceState?.get('karma.lastDiagram');
  async function open(data){
    const d=validate(data.result.accepted_output.diagram),id=data.result.invocation_id;
    last=id;await context.workspaceState?.update('karma.lastDiagram',id);
    const panel=vscode.window.createWebviewPanel('karma.diagram',d.title,vscode.ViewColumn.Beside,{enableScripts:true,localResourceRoots:[vscode.Uri.file(path.join(context.extensionPath,'media')),vscode.Uri.file(path.join(context.extensionPath,'vendor/mermaid'))]});
    const nonce=crypto.randomBytes(18).toString('base64'),web=panel.webview;
    const resource=name=>web.asWebviewUri(vscode.Uri.file(path.join(context.extensionPath,name))).toString();
    web.html=fs.readFileSync(path.join(context.extensionPath,'media/diagram.html'),'utf8').replaceAll('{{csp}}',web.cspSource).replaceAll('{{nonce}}',nonce).replace('{{css}}',resource('media/diagram.css')).replace('{{mermaid}}',resource('vendor/mermaid/mermaid.min.js')).replace('{{script}}',resource('media/diagram.js'));
    web.onDidReceiveMessage(async message=>{try{
      if(message.type==='ready'){await web.postMessage({type:'diagram',diagram:d,source:mermaidSource(d),receipt:id});return;}
      if(message.type==='layout'){await web.postMessage({type:'source',source:mermaidSource(d,message.direction)});return;}
      if(message.type==='copy'){await vscode.env.clipboard.writeText(mermaidSource(d,message.direction));return;}
      if(message.type==='export'){
        const svg=safeSVG(message.svg),name=d.title.replace(/[^a-zA-Z0-9 _-]/g,'').slice(0,60)||'Sutra diagram';
        const uri=await vscode.window.showSaveDialog({title:'Export diagram as SVG',defaultUri:vscode.Uri.file(path.join(require('node:os').tmpdir(),name+'.svg')),filters:{SVG:['svg']}});
        if(uri)await vscode.workspace.fs.writeFile(uri,Buffer.from(svg));
      }
    }catch(error){void vscode.window.showErrorMessage('Sutra diagram: '+error.message);}});
    context.subscriptions.push(panel);return panel;
  }
  context.subscriptions.push(vscode.commands.registerCommand('karma.openLastDiagram',async()=>{if(!last){void vscode.window.showInformationMessage('Ask Sutra Auto to draw a diagram first.');return;}try{await open(await read(last));}catch(e){void vscode.window.showErrorMessage(e.message);}}));
  return {open};
}
module.exports={install,validate,mermaidSource,safeSVG};

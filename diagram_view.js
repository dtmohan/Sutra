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
  // Keep copied Mermaid theme-neutral; the preview/export resolves editor colors.
  return ['flowchart '+dir,...d.nodes.map(n=>`  ${n.id}["${label(n.label)}"]:::${n.kind}`),
    ...d.edges.map(e=>`  ${e.source} -->${e.label?'|"'+label(e.label)+'"|':''} ${e.target}`)].join('\n');
}
function safeSVG(svg){
  if(typeof svg!=='string'||Buffer.byteLength(svg)>2000000||!/^<svg\b/.test(svg)||/<(?:script|foreignObject|iframe|image|use)\b|<!DOCTYPE|<!ENTITY|\son[a-z]+\s*=|(?:href|src)\s*=\s*["'](?!#)|url\(\s*["']?(?!#)[^)]|@import/i.test(svg))throw Error('Unsafe or oversized SVG export');
  return svg;
}
function diagramHTML(root,{cspSource,nonce,stylesheet}){
  const renderer=fs.readFileSync(path.join(root,'vendor/mermaid/mermaid.min.js'),'utf8');
  const controls=fs.readFileSync(path.join(root,'media/diagram.js'),'utf8');
  // VS Code's webview resource stream can duplicate script bytes while delivering local files.
  // Embed only these two installed scripts so execution does not depend on that stream.
  const scripts=[renderer,controls].map((body,index)=>{
    if(/<\/?script(?:[\s/>]|$)/i.test(body))throw Error('Invalid diagram script boundary');
    const type=index===0?'':' type="module"';
    return `<script${type} nonce="${nonce}">\n${body}\n</script>`;
  }).join('\n');
  return fs.readFileSync(path.join(root,'media/diagram.html'),'utf8')
    .replaceAll('{{csp}}',cspSource).replaceAll('{{nonce}}',nonce)
    .replace('{{css}}',stylesheet).replace('{{scripts}}',()=>scripts);
}
function install(vscode,context,read){
  let last=context.workspaceState?.get('karma.lastDiagram');
  async function open(data,source=false){
    const d=validate(source?data.review_diagram:data.result.accepted_output.diagram),id=data.result.invocation_id;
    last=source?{kind:'source',id,diagram:d}:id;await context.workspaceState?.update('karma.lastDiagram',last);
    const panel=vscode.window.createWebviewPanel('karma.diagram',d.title,vscode.ViewColumn.Beside,{enableScripts:true,localResourceRoots:[vscode.Uri.file(path.join(context.extensionPath,'media'))]});
    const nonce=crypto.randomBytes(18).toString('base64'),web=panel.webview;
    const stylesheet=web.asWebviewUri(vscode.Uri.file(path.join(context.extensionPath,'media/diagram.css'))).toString();
    const html=diagramHTML(context.extensionPath,{cspSource:web.cspSource,nonce,stylesheet});
    web.onDidReceiveMessage(async message=>{try{
      if(message.type==='ready'){await web.postMessage({type:'diagram',diagram:d,source:mermaidSource(d),receipt:id,provenance:source?'source':'conversation'});return;}
      if(message.type==='layout'){await web.postMessage({type:'source',source:mermaidSource(d,message.direction)});return;}
      if(message.type==='copy'){await vscode.env.clipboard.writeText(mermaidSource(d,message.direction));return;}
      if(message.type==='export'){
        const svg=safeSVG(message.svg),name=d.title.replace(/[^a-zA-Z0-9 _-]/g,'').slice(0,60)||'Sutra diagram';
        const uri=await vscode.window.showSaveDialog({title:'Export diagram as SVG',defaultUri:vscode.Uri.file(path.join(require('node:os').tmpdir(),name+'.svg')),filters:{SVG:['svg']}});
        if(uri)await vscode.workspace.fs.writeFile(uri,Buffer.from(svg));
      }
    }catch(error){void vscode.window.showErrorMessage('Sutra diagram: '+error.message);}});
    web.html=html;
    context.subscriptions.push(panel);return panel;
  }
  context.subscriptions.push(vscode.commands.registerCommand('karma.openLastDiagram',async()=>{if(!last){void vscode.window.showInformationMessage('Ask Sutra Auto to draw a diagram first.');return;}try{if(last.kind==='source')await open({review_diagram:{...last.diagram,note:'Saved review snapshot. '+last.diagram.note.slice(0,210)},result:{invocation_id:last.id}},true);else await open(await read(last));}catch(e){void vscode.window.showErrorMessage(e.message);}}));
  return {open,openReview:data=>open(data,true)};
}
module.exports={install,validate,mermaidSource,safeSVG,diagramHTML};

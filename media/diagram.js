'use strict';
const vscode=acquireVsCodeApi(),el=id=>document.getElementById(id);let source='',svg='',sequence=0,scale=1,natural={width:1,height:1};
let renderer,rendererTheme;
function theme(){
  const style=getComputedStyle(document.body),classes=document.body.classList;
  const dark=classes.contains('vscode-dark')||classes.contains('vscode-high-contrast')||
    (!classes.contains('vscode-light')&&!classes.contains('vscode-high-contrast-light')&&matchMedia('(prefers-color-scheme: dark)').matches);
  function color(name,fallback){
    const value=style.getPropertyValue(name).trim();
    if(/^#[a-f\d]{6}$/i.test(value))return value;
    if(/^#[a-f\d]{3}$/i.test(value))return '#'+[...value.slice(1)].map(c=>c+c).join('');
    const rgb=value.match(/^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?:\s*,[^)]+)?\s*\)$/i);
    return rgb?'#'+rgb.slice(1,4).map(n=>Math.min(255,Number(n)).toString(16).padStart(2,'0')).join(''):fallback;
  }
  return {background:color('--vscode-editor-background',dark?'#1e1e1e':'#ffffff'),
    text:color('--vscode-editor-foreground',dark?'#d4d4d4':'#202020'),
    surface:color('--vscode-input-background',dark?'#313131':'#f2f2f2'),
    border:color('--vscode-contrastBorder',color('--vscode-widget-border',dark?'#a0a0a0':'#737373')),
    accent:color('--vscode-focusBorder',dark?'#75beff':'#0065a9'),
    output:color('--vscode-charts-green',dark?'#89d185':'#237b4b'),
    width:classes.contains('vscode-high-contrast')||classes.contains('vscode-high-contrast-light')?2.5:1.5};
}
function getRenderer(){
  const palette=theme(),key=JSON.stringify(palette);
  if(renderer&&rendererTheme===key)return renderer;
  const mermaid=globalThis.mermaid;
  if(!mermaid)throw Error('The offline renderer did not load');
  const kindStyle=(kind,border)=>`.node.${kind} rect,.node.${kind} polygon,.node.${kind} path{fill:${palette.surface}!important;stroke:${border}!important;stroke-width:${palette.width}px!important}`;
  mermaid.initialize({startOnLoad:false,securityLevel:'strict',theme:'base',htmlLabels:false,
    flowchart:{htmlLabels:false,useMaxWidth:false,nodeSpacing:36,rankSpacing:60,curve:'basis'},
    themeVariables:{fontFamily:'Arial, Helvetica, sans-serif',fontSize:'16px',background:palette.background,
      primaryColor:palette.surface,primaryTextColor:palette.text,primaryBorderColor:palette.border,
      lineColor:palette.text,edgeLabelBackground:palette.background},
    themeCSS:kindStyle('source',palette.accent)+kindStyle('device',palette.border)+
      kindStyle('process',palette.accent)+kindStyle('output',palette.output),
    maxEdges:24,maxTextSize:16000,suppressErrorRendering:true});
  document.documentElement.style.setProperty('--sutra-diagram-background',palette.background);
  renderer=mermaid;rendererTheme=key;
  return renderer;
}
function zoom(){const node=el('canvas').querySelector('svg');if(node){node.style.width=natural.width*scale+'px';node.style.height=natural.height*scale+'px';}}
function fit(){scale=Math.min(1,(el('viewport').clientWidth-70)/natural.width,(el('viewport').clientHeight-70)/natural.height);zoom();}
async function render(value){const run=++sequence;source=value;svg='';el('export').disabled=true;el('error').textContent='';el('source').textContent=source;
 try{const result=await getRenderer().render('karma_graph_'+run,value);if(run!==sequence)return;const doc=new DOMParser().parseFromString(result.svg,'image/svg+xml');if(doc.querySelector('parsererror,script,foreignObject,iframe,image,use'))throw Error('Diagram renderer produced unsupported SVG');
  const node=document.importNode(doc.documentElement,true);for(const element of [node,...node.querySelectorAll('*')])for(const attr of [...element.attributes])if(/^on/i.test(attr.name)||(/href$|^src$/i.test(attr.name)&&!attr.value.startsWith('#')))throw Error('Diagram renderer produced an external reference');
  const walker=document.createTreeWalker(node,NodeFilter.SHOW_TEXT);while(walker.nextNode()){const text=walker.currentNode;if(text.parentElement?.closest('text,tspan'))text.nodeValue=text.nodeValue.replace(/&#(\d+);/g,(_,code)=>String.fromCodePoint(Number(code)));}
  node.setAttribute('role','img');node.setAttribute('aria-label',el('title').textContent+'. '+el('note').textContent);node.setAttribute('xmlns','http://www.w3.org/2000/svg');node.style.background=theme().background;svg=new XMLSerializer().serializeToString(node);const box=node.viewBox.baseVal;natural={width:box.width||1000,height:box.height||500};el('canvas').replaceChildren(node);el('export').disabled=false;fit();
 }catch(error){if(run===sequence){el('canvas').replaceChildren();el('error').textContent='Could not render this diagram: '+error.message+'. The diagram source remains available below.';}}
}
const refreshTheme=()=>{if(source)render(source);};
new MutationObserver(refreshTheme).observe(document.body,{attributes:true,attributeFilter:['class','style']});
matchMedia('(prefers-color-scheme: dark)').addEventListener('change',refreshTheme);
window.addEventListener('message',e=>{const m=e.data;if(m.type==='diagram'){el('title').textContent=m.diagram.title;el('note').textContent=m.diagram.note;el('direction').value=m.diagram.direction;el('provenance').textContent=m.provenance==='source'?'Source snapshot · local import candidates from fresh source; runtime loading is unverified.':'Conceptual diagram · supplied description; relationships are not independently verified.';el('receipt').textContent=(m.provenance==='source'?'Review invocation: ':'Conversation receipt: ')+m.receipt;render(m.source);}if(m.type==='source')render(m.source);});
el('fit').onclick=fit;el('plus').onclick=()=>{scale=Math.min(3,scale*1.25);zoom();};el('minus').onclick=()=>{scale=Math.max(.1,scale/1.25);zoom();};el('direction').onchange=()=>vscode.postMessage({type:'layout',direction:el('direction').value});el('copy').onclick=()=>vscode.postMessage({type:'copy',direction:el('direction').value});el('export').onclick=()=>{if(svg)vscode.postMessage({type:'export',svg});};window.addEventListener('resize',fit);vscode.postMessage({type:'ready'});

'use strict';
const vscode=acquireVsCodeApi(),el=id=>document.getElementById(id),T=KarmaTokens;
const node=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
let snapshot,routing,error,filters={...vscode.getState()?.filters};
for(const key of ['model','stage','status','time']){el(key).value=filters[key]||'';el(key).onchange=()=>{filters[key]=el(key).value;vscode.setState({filters});render();};}
el('reset').onclick=()=>{filters={};for(const key of ['model','stage','status','time'])el(key).value='';vscode.setState({filters});render();};
el('refresh').onclick=()=>vscode.postMessage({type:'refresh'});
el('telemetry').onclick=()=>vscode.postMessage({type:'telemetry'});
function fillOptions(key){
 const values=[...new Set((snapshot?.calls||[]).map(c=>c[key]))].sort();if(filters[key]&&!values.includes(filters[key]))values.push(filters[key]);
 el(key).replaceChildren();const all=node('option','All '+(key==='model'?'models':'stages'));all.value='';el(key).append(all);
 for(const value of values){const option=node('option',key==='stage'?T.stage(value):value);option.value=value;el(key).append(option);}el(key).value=filters[key]||'';
}
function render(){
 el('error').hidden=!error;el('error').textContent=error?(snapshot?'Refresh unavailable — showing the last recorded snapshot. ':'')+error:'';
 el('activity').textContent=error?'Refresh unavailable':routing?.active?'Request in progress':snapshot?'Up to date':'Waiting for data';el('activity').dataset.active=String(!!routing?.active);
 if(!snapshot)return;
 el('scope').textContent=snapshot.workspace+' · updated '+new Date(snapshot.updatedAt).toLocaleTimeString();
 const rows=snapshot.calls.filter(c=>(!filters.model||c.model===filters.model)&&(!filters.stage||c.stage===filters.stage)&&(!filters.status||c.status===filters.status)&&(!filters.time||c.created_at>=Date.now()/1000-Number(filters.time))),t=T.totals(rows);
 for(const key of ['input','output']){el(key).textContent=T.number(t[key+'_tokens']);const missing=t['missing_'+key+'_tokens'];el(key+'-note').textContent=!rows.length?'No matching measurements':missing?'Known subtotal · '+missing+' unknown':'Measured across matching calls';}
 el('calls').textContent=T.number(rows.length);el('unknown').textContent=T.number(rows.filter(c=>!T.valid(c.input_tokens)||!T.valid(c.output_tokens)).length);
 el('window').textContent=rows.length+' matching calls · showing latest '+snapshot.calls.length+' of '+snapshot.total_calls+' calls in this workspace. Filters apply within this window.';
 el('empty').hidden=!!rows.length;el('visuals').hidden=!rows.length;el('details').hidden=!rows.length;
 el('empty-title').textContent=snapshot.calls.length?'No calls match these filters':'No model calls yet';
 el('empty-detail').textContent=snapshot.calls.length?'Reset the filters to see recorded calls.':'Send a message using Sutra Auto. Recorded token counts will appear here.';
 const recent=[...rows].sort((a,b)=>a.created_at-b.created_at).slice(-20),max=Math.max(1,...recent.flatMap(c=>[c.input_tokens,c.output_tokens].filter(T.valid)));
 el('timeline').replaceChildren();
 for(const c of recent){const pair=node('div',undefined,'call-bar');pair.dataset.unknown=String(!T.valid(c.input_tokens)||!T.valid(c.output_tokens));pair.title=T.stage(c.stage)+' · '+c.model+' · '+T.number(c.input_tokens)+' input / '+T.number(c.output_tokens)+' output';
  for(const [key,cls] of [['input_tokens','in'],['output_tokens','out']]){if(!T.valid(c[key]))continue;const bar=node('span',undefined,cls);bar.style.height=(100*c[key]/max)+'%';pair.append(bar);}el('timeline').append(pair);
 }
 el('timeline').setAttribute('aria-label','Last '+recent.length+' matching calls. '+T.metric(t,'input_tokens')+' input and '+T.metric(t,'output_tokens')+' output tokens across all matching calls.');
 const groups=new Map();for(const c of rows){if(!groups.has(c.stage))groups.set(c.stage,[]);groups.get(c.stage).push(c);}
 const totals=[...groups].map(([stage,calls])=>({stage,...T.totals(calls)})).sort((a,b)=>(b.input_tokens||0)+(b.output_tokens||0)-(a.input_tokens||0)-(a.output_tokens||0));
 const stageMax=Math.max(1,...totals.flatMap(g=>[g.input_tokens,g.output_tokens].filter(T.valid)));el('stages').replaceChildren();
 for(const g of totals){const line=node('div',undefined,'stage'),heading=node('div',undefined,'heading');heading.append(node('span',T.stage(g.stage)),node('span',T.metric(g,'input_tokens')+' in / '+T.metric(g,'output_tokens')+' out'));line.append(heading);
  for(const [key,cls] of [['input_tokens','in'],['output_tokens','out']]){const p=node('progress',undefined,cls);p.max=stageMax;p.value=g[key]||0;p.setAttribute('aria-label',T.stage(g.stage)+' '+key+': '+T.metric(g,key));line.append(p);}el('stages').append(line);
 }
 el('rows').replaceChildren();for(const c of rows){const tr=node('tr');const values=[new Date(c.created_at*1000).toLocaleString(),c.model+' / '+T.stage(c.stage),c.status==='accepted'?'Accepted':c.status==='quarantined'?'Withheld':'Pending / interrupted',T.number(c.input_tokens),T.number(c.output_tokens),T.number(c.conservative_input_charge)+' / '+T.number(c.output_reserve)];
  values.forEach((value,i)=>tr.append(node('td',value,i>=3?'num':undefined)));tr.title='Invocation '+c.invocation_id;el('rows').append(tr);
 }
}
window.addEventListener('message',({data:m})=>{if(m.type!=='usage')return;snapshot=m.snapshot;routing=m.routing;error=m.error;for(const key of ['model','stage'])fillOptions(key);if(!snapshot){for(const key of ['input','output','calls','unknown'])el(key).textContent='—';for(const key of ['input','output'])el(key+'-note').textContent='Waiting for measurements';el('scope').textContent='This workspace · waiting for recorded calls…';el('visuals').hidden=true;el('details').hidden=true;el('empty').hidden=true;el('window').textContent='';}render();});
vscode.postMessage({type:'ready'});

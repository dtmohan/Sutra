'use strict';
(function(root){
  const labels={conversation:'Conversation / routing',parser:'Task parsing',selector:'Page selection',worker:'Worker',coder:'Coding',reviewer:'Code review',chat_reviewer:'Workspace review',architect:'Architecture review',api_mapper:'API mapping',api_reviewer:'API verification',static_explainer:'Source explanation'};
  const valid=n=>Number.isSafeInteger(n)&&n>=0;
  const number=n=>valid(n)?n.toLocaleString('en-US'):'—';
  const stage=s=>labels[s]||s;
  function merge(...groups){const rows=new Map();for(const g of groups)for(const row of g?.calls||[])rows.set(row.invocation_id,row);return {calls:[...rows.values()]};}
  function totals(calls){const result={calls:calls.length};for(const field of ['input_tokens','output_tokens']){const known=calls.map(c=>c[field]).filter(valid);result[field]=known.length?known.reduce((a,b)=>a+b,0):null;result['missing_'+field]=calls.length-known.length;}return result;}
  function metric(t,field){return number(t[field])+(t['missing_'+field]&&t[field]!==null?' + unknown':'');}
  function compact(group){const t=totals(group?.calls||[]);if(!t.calls)return 'Token usage unavailable';return `${metric(t,'input_tokens')} in · ${metric(t,'output_tokens')} out · ${t.calls} call${t.calls===1?'':'s'}`;}
  function markdown(group){
    const calls=group?.calls||[];if(!calls.length)return 'No token measurements recorded.';
    const escape=v=>String(v).replace(/[\r\n|]/g,' ').replace(/</g,'&lt;');
    return '**Measured tokens: '+compact(group)+'**\n\n| Stage | Model | Result | Input | Output |\n| --- | --- | --- | ---: | ---: |\n'+calls.map(c=>`| ${escape(stage(c.stage))} | ${escape(c.model)} | ${escape(c.status)} | ${number(c.input_tokens)} | ${number(c.output_tokens)} |`).join('\n')+'\n\nCounts are reported by Ollama for model calls, including structured output and repairs. Missing measurements are unknown, not zero. They are separate from conservative budget charges.\n';
  }
  const api={valid,number,stage,merge,totals,metric,compact,markdown};
  if(typeof module==='object'&&module.exports)module.exports=api;else root.KarmaTokens=api;
})(typeof globalThis==='object'?globalThis:this);

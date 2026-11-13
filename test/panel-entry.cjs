'use strict';
// Execute the shipped panel script against a small DOM fixture to exercise user actions.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=path.resolve(__dirname,'..'),html=fs.readFileSync(path.join(root,'media/panel.html'),'utf8');
class Element{
 constructor(tag,id=''){this.tagName=tag.toUpperCase();this.id=id;this.children=[];this.value='';this.textContent='';this.disabled=false;this.hidden=false;this.open=false;}
 append(...items){this.children.push(...items);}replaceChildren(...items){this.children=items;}
 setAttribute(){}querySelector(){return null;}scrollIntoView(){this.scrolled=true;}
}
const elements=new Map();for(const match of html.matchAll(/<([a-z0-9]+)\b[^>]*\bid="([^"]+)"/g)){
 assert(!elements.has(match[2]),'Duplicate ID '+match[2]);elements.set(match[2],new Element(match[1],match[2]));
}
const el=id=>{assert(elements.has(id),'Missing panel element '+id);return elements.get(id);};
const listeners=[],sent=[];
const document={addEventListener(){},getElementById:el,createElement:tag=>new Element(tag),createTextNode:text=>text,
 querySelectorAll:selector=>selector==='button,select'?[...elements.values()].filter(e=>['BUTTON','SELECT'].includes(e.tagName)):[]};
vm.runInNewContext(fs.readFileSync(path.join(root,'media/panel.js'),'utf8'),{KarmaTokens:require('../media/token_usage'),document,window:{addEventListener(event,fn){if(event==='message')listeners.push(fn);}},
 acquireVsCodeApi:()=>({postMessage:m=>sent.push(m)}),setInterval(){},Date,console},{filename:'panel.js'});
const receive=m=>listeners.forEach(fn=>fn({data:m}));
const catalog={models:['small','moe','large'].map(name=>({name,eligible:true})),suggested_tiers:{low:'small',mid:'moe',high:'large'}};
receive({type:'tier-settings',models:{low:'large',mid:'large',high:'large'},catalog});
assert.equal(el('tier-low').value,'large');assert.match(el('model-suggestions').textContent,/saved choices were preserved/);
const before=sent.length;el('use-model-defaults').onclick();
assert.equal(sent.length,before,'Draft defaults must not persist settings');assert.equal(el('tier-low').value,'small');assert.equal(el('tier-mid').value,'moe');
assert.match(el('model-save-status').textContent,/Click Save/);
el('save-model-tiers').onclick();assert.equal(sent.at(-1).op,'save_model_tiers');assert.equal(sent.at(-1).models.low,'small');
receive({type:'models-saved'});assert.match(el('model-save-status').textContent,/saved/);
receive({type:'show-section',section:'models'});assert(el('tier-config').open&&el('tier-config').scrolled);
receive({type:'state',data:{session:'legacy-task',model:'large'}});assert.equal(el('workflow-notice').hidden,false);
el('open-current-task').onclick();assert(el('advanced').open&&el('advanced').scrolled);
el('chat-prompt').value='Review this codebase';el('chat-tier').value='auto';
el('chat-send').onclick();assert.equal(sent.at(-1).fresh,false);el('chat-new').onclick();assert.equal(sent.at(-1).fresh,true);
receive({type:'chat-notice',data:{kind:'workflow',message:'Open current task controls'}});assert.equal(el('chat-answer').textContent,'Open current task controls');assert.equal(el('error').textContent,'');
receive({type:'tier-settings',models:{},catalog:{models:[],suggested_tiers:{}}});receive({type:'busy',value:false});assert(el('use-model-defaults').disabled);assert(el('save-model-tiers').disabled);
receive({type:'state',data:{session:null,model:'large'}});assert.equal(el('workflow-notice').hidden,true);
console.log(JSON.stringify({passed:true,checks:['saved choices remain selected with visible discovered defaults','defaults stay in draft until Save choices','first-open section scroll and expansion','legacy task banner opens current controls','Send preserves task; Start separate review explicitly sets fresh','guidance is not rendered as an error','no defaults button without eligible models']}));

'use strict';
// Fixture exercise of the shipped webview. No engine, Python setup or model is started.
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=path.resolve(__dirname,'..');
const html=fs.readFileSync(path.join(root,'media/panel.html'),'utf8');
class Element{
 constructor(tag,id=''){this.tagName=tag.toUpperCase();this.id=id;this.children=[];this.value='';this.textContent='';this.disabled=false;this.hidden=false;}
 append(...items){this.children.push(...items);}replaceChildren(...items){this.children=items;}
 setAttribute(name,value){(this.attributes??={})[name]=value;}
 querySelector(){return null;}scrollIntoView(){}focus(){}
}
const elements=new Map();
for(const match of html.matchAll(/<([a-z0-9]+)\b[^>]*\bid="([^"]+)"/g)){
 assert(!elements.has(match[2]),'Duplicate ID '+match[2]);
 elements.set(match[2],new Element(match[1],match[2]));
}
const el=id=>{assert(elements.has(id),'Missing element '+id);return elements.get(id);};
const listeners=[],sent=[];
const document={addEventListener(){},getElementById:el,createElement:tag=>new Element(tag),createTextNode:text=>text,
 querySelectorAll:selector=>selector==='button,select'?[...elements.values()].filter(e=>['BUTTON','SELECT'].includes(e.tagName)):[]};
vm.runInNewContext(fs.readFileSync(path.join(root,'media/panel.js'),'utf8'),{
 KarmaTokens:require('../media/token_usage'),document,
 window:{addEventListener(event,fn){if(event==='message')listeners.push(fn);}},
 acquireVsCodeApi:()=>({postMessage:m=>sent.push(m)}),setInterval(){},Date,console
},{filename:'panel.js'});
const receive=m=>listeners.forEach(fn=>fn({data:m}));
const last=()=>JSON.parse(JSON.stringify(sent.at(-1)));
const model={name:'sutra:engine/local-16k',display_name:'Sutra engine · Local 16K',context_tokens:16384,provider:'sutra_engine',eligible:true};
const other={name:'sutra:engine/local-32k',display_name:'<img src=x onerror=alert(1)>',context_tokens:32768,provider:'sutra_engine',eligible:true};
const state={models:[model,other],status:{running:true,busy:false,phase:'idle',profile:model.name,context_tokens:16384},environment_ready:true,message:'Sutra worker ready.'};
assert(html.indexOf('id="engine-manager"')<html.indexOf('id="strata-manager"'),'The Sutra worker is the primary runtime control');
assert.match(html,/<p id="engine-status" role="status" aria-live="polite">/);
assert.match(html,/<label for="engine-profile">Sutra worker model<\/label>/);
assert.match(html,/does not download model weights/);
receive({type:'engine-state',data:{models:[model],status:{running:false,phase:'unloaded'},environment_ready:false}});
assert.equal(el('engine-runtime-state').textContent,'Setup needed');
assert(el('engine-load').disabled);assert(el('engine-unload').disabled);
assert(!el('engine-setup').disabled);assert(!el('engine-refresh').disabled);
el('engine-setup').onclick();assert.deepEqual(last(),{op:'engine_setup'});
assert(el('engine-setup').disabled);assert(el('engine-refresh').disabled);
assert.equal(el('engine-manager').attributes['aria-busy'],'true');
receive({type:'engine-state',data:state});
assert.equal(el('engine-profile').value,model.name);
assert.equal(el('engine-runtime-state').textContent,'Loaded');
assert.match(el('engine-profile-info').textContent,/16,384 context tokens.*Loaded by Sutra/);
assert(!el('engine-load').disabled);assert(!el('engine-unload').disabled);
assert.equal(el('engine-profile').children[1].textContent,other.display_name);
assert.equal(el('engine-profile').children[1].children.length,0,'Registry names are text, never HTML');
const beforeSelection=sent.length;
el('engine-profile').value=other.name;el('engine-profile').onchange();
assert.equal(sent.length,beforeSelection,'Selecting a model never loads it or saves tier choices');
assert.match(el('engine-profile-info').textContent,/32,768 context tokens.*Available to load/);
el('engine-load').onclick();assert.deepEqual(last(),{op:'engine_load',ref:other.name});
assert(el('engine-load').disabled);assert(el('engine-unload').disabled);assert(el('strata-load').disabled);
assert.match(el('engine-status').textContent,/Checking resources and loading/);
receive({type:'engine-state',data:{...state,busy:true,status:{...state.status,busy:true,phase:'loading'}}});
assert.equal(el('engine-runtime-state').textContent,'Loading');
assert(el('engine-load').disabled);assert(!el('engine-refresh').disabled,'Read-only refresh remains possible during a busy snapshot');
el('engine-refresh').onclick();assert.deepEqual(last(),{op:'engine_refresh'});
receive({type:'engine-state',data:{...state,error:'Memory admission denied.'}});
assert.equal(el('engine-profile').value,other.name,'Fresh status preserves the selected registry profile');
assert.equal(el('engine-status').textContent,'Memory admission denied.');
assert(!el('engine-load').disabled);assert(!el('engine-refresh').disabled);
receive({type:'busy',value:true});assert(el('engine-load').disabled);assert(el('engine-setup').disabled);
receive({type:'busy',value:false});assert(!el('engine-load').disabled);
receive({type:'native-status',data:{working:true,label:'Working',explanation:'Turn active'}});
assert(el('engine-unload').disabled);assert(el('engine-profile').disabled);
assert(!el('engine-refresh').disabled);
receive({type:'native-status',data:{working:false,label:'Ready',explanation:'Turn complete'}});
assert(!el('engine-unload').disabled);
const saved={low:'old-low',mid:'old-mid',high:'old-high'};
receive({type:'tier-settings',models:saved,catalog:{models:[{...model,ready:true},{...other,ready:false}],suggested_tiers:{}}});
assert.equal(el('tier-low').value,'old-low','Discovery preserves the saved tiers');
assert.match(el('tier-low').children[0].textContent,/Sutra engine · Local 16K · loaded/);
assert.match(el('tier-low').children[1].textContent,/loads on demand/);
assert(!el('engine-use-tiers').disabled);
const beforeDraft=sent.length;el('engine-use-tiers').onclick();
assert.equal(sent.length,beforeDraft,'Assigning a registered model only changes the tier draft');
for(const tier of ['low','mid','high'])assert.equal(el('tier-'+tier).value,other.name);
assert.match(el('model-save-status').textContent,/shares its model weights and capabilities.*Save choices/);
el('save-model-tiers').onclick();assert.deepEqual(last(),{op:'save_model_tiers',models:{low:other.name,mid:other.name,high:other.name}});
el('engine-unload').onclick();assert.deepEqual(last(),{op:'engine_unload'});
assert(el('save-model-tiers').disabled);assert(el('tier-low').disabled);
receive({type:'error',message:'Worker stopped during unload. Refresh status.'});
assert(!el('engine-refresh').disabled);assert(!el('engine-load').disabled);
assert.equal(el('engine-status').textContent,'Worker stopped during unload. Refresh status.');
receive({type:'engine-state',data:{...state,status:{running:false,busy:false,phase:'unloaded',profile:null}}});
assert.equal(el('engine-runtime-state').textContent,'Unloaded');assert(el('engine-unload').disabled);
receive({type:'tier-settings',models:saved,catalog:{models:[],suggested_tiers:{}}});
assert(el('engine-use-tiers').disabled,'A missing eligible catalog entry cannot be assigned to tiers');
receive({type:'engine-state',data:{models:[],status:{running:false},environment_ready:true,error:true,message:'No model registered.'}});
assert.equal(el('engine-status').textContent,'No model registered.','Boolean error flags do not replace the readable message');
assert.equal(el('engine-profile').value,'');assert(el('engine-load').disabled);assert(el('engine-profile').disabled);
console.log('PASS Sutra engine manager registration, host-only intents, safe labels, shared busy state, recovery and draft-only tier assignment');

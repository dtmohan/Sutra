'use strict';
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
const state={available:true,busy:false,runtime_state:'ready',selected_profile:'current',message:'Native runtime is ready.',start_on_startup:true,
 profiles:[{id:'current',name:'High 32K',model:'large',context_tokens:32768,selected:true},
  {id:'other',name:'<img src=x onerror=alert(1)>',model:'other model',context_tokens:16384,state:'stopped'}],
 models:[{id:'disk-model',name:'Native model <script>bad()</script>',context_tokens:32768}]};
receive({type:'strata-state',data:{available:false,message:'Locate Strata to continue.'}});
assert(el('strata-load').disabled);assert(el('strata-unload').disabled);
assert(!el('strata-locate').disabled);assert(!el('strata-refresh').disabled);
el('strata-locate').onclick();assert.equal(sent.at(-1).op,'strata_locate');
assert(el('strata-refresh').disabled,'A pending host picker blocks competing operations');
receive({type:'strata-state',data:state});
assert.equal(el('strata-profile').value,'current');
assert.equal(el('strata-runtime-state').textContent,'Loaded');
assert.match(el('strata-profile-info').textContent,/32,768 context tokens/);
assert(!el('strata-load').disabled);assert(!el('strata-unload').disabled);
assert(el('strata-autostart').checked);assert(!el('strata-autostart').disabled);
assert.equal(el('strata-installed-models').children[0].textContent,'Native model <script>bad()</script> · 32,768 context tokens');
assert.equal(el('strata-profile').children[1].textContent,'<img src=x onerror=alert(1)>');
assert.equal(el('strata-profile').children[1].children.length,0,'Names are text, never parsed HTML');
const before=sent.length;el('strata-profile').value='other';el('strata-profile').onchange();
assert.equal(sent.length,before,'Selecting a profile never starts it or changes tier settings');
assert.match(el('strata-profile-info').textContent,/16,384 context tokens/);
el('strata-load').onclick();
assert.equal(JSON.stringify(sent.at(-1)),JSON.stringify({op:'strata_load',id:'other'}));
assert(el('strata-load').disabled);assert(el('strata-unload').disabled);
assert.equal(el('strata-manager').attributes['aria-busy'],'true');
assert.match(el('strata-status').textContent,/Checking resources and loading/);
receive({type:'strata-state',data:{...state,busy:true,runtime_state:'starting',message:'Loading native weights…'}});
assert.equal(el('strata-runtime-state').textContent,'Loading');
assert(el('strata-locate').disabled);
receive({type:'strata-state',data:{...state,error:'Memory admission denied.'}});
assert.equal(el('strata-profile').value,'other','Refresh keeps a selected profile when still present');
assert.equal(el('strata-status').textContent,'Memory admission denied.');
assert(!el('strata-refresh').disabled);
receive({type:'busy',value:true});assert(el('strata-load').disabled);assert(el('strata-setup').disabled);
receive({type:'busy',value:false});assert(!el('strata-load').disabled);
receive({type:'native-status',data:{working:true,label:'Working',explanation:'Turn active',reason:''}});
assert(el('strata-unload').disabled,'Native chat activity blocks runtime mutation');
receive({type:'native-status',data:{working:false,label:'Ready',explanation:'Turn complete',reason:''}});
assert(!el('strata-unload').disabled);
el('strata-autostart').checked=false;el('strata-autostart').onchange();
assert.equal(JSON.stringify(sent.at(-1)),JSON.stringify({op:'strata_autostart',enabled:false}));
assert(el('strata-autostart').disabled);receive({type:'strata-state',data:{...state,start_on_startup:false}});
assert(!el('strata-autostart').checked);
el('strata-add-config').onclick();
assert.equal(JSON.stringify(sent.at(-1)),JSON.stringify({op:'strata_add_config'}),'The host chooses paths');
receive({type:'error',message:'Picker cancelled'});assert(!el('strata-refresh').disabled);
receive({type:'strata-state',data:{...state,profiles:[],runtime_state:'stopped'}});
assert(el('strata-load').disabled);assert(el('strata-unload').disabled);
assert.equal(el('strata-runtime-state').textContent,'Unloaded');
assert.match(html,/<p id="strata-status" role="status" aria-live="polite">/);
assert.match(html,/<label for="strata-profile">/);
const nativeCatalog={models:[
 {name:'strata:profile/native-small',display_name:'Strata · Small native',eligible:true,active:true,ready:false,load_on_demand:true},
 {name:'strata:profile/native-large',display_name:'Strata · Large native',eligible:true,ready:true,load_on_demand:true}
],suggested_tiers:{},errors:[{model:'ollama',error:'Ollama is unavailable.'}]};
const savedModels={low:'old-low',mid:'old-mid',high:'strata:high'};
receive({type:'tier-settings',models:savedModels,catalog:nativeCatalog});
assert.equal(el('tier-low').value,'old-low','Discovery must preserve existing tier choices');
for(const tier of ['low','mid','high']){
 assert.equal(el('tier-'+tier).children[0].value,'strata:profile/native-small');
 assert.match(el('tier-'+tier).children[0].textContent,/Small native · loads on demand/);
 assert.match(el('tier-'+tier).children[1].textContent,/Large native · loaded/);
}
const beforeDraft=sent.length;el('use-native-profile').onclick();
assert.equal(sent.length,beforeDraft,'Using a native profile changes only the draft');
for(const tier of ['low','mid','high'])assert.equal(el('tier-'+tier).value,'strata:profile/native-small');
assert.match(el('model-save-status').textContent,/same model weights.*Save choices/);
el('save-model-tiers').onclick();
assert.equal(sent.at(-1).op,'save_model_tiers');
for(const tier of ['low','mid','high'])assert.equal(sent.at(-1).models[tier],'strata:profile/native-small');
receive({type:'busy',value:true});assert(el('use-native-profile').disabled);
receive({type:'busy',value:false});assert(!el('use-native-profile').disabled);
receive({type:'tier-settings',models:savedModels,catalog:{models:[],errors:[],suggested_tiers:{}}});
assert(el('use-native-profile').disabled,'A missing native profile cannot be assigned');
console.log('PASS Strata management UI selection, host-only intents, text safety, loading feedback and shared busy controls');

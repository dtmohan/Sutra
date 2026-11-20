'use strict';
const assert=require('node:assert/strict');
const C=require('../media/coherence');
const observation=(extra={})=>({format:1,profile:'stage-text-coherence-v1',mode:'observe_only',status:'partial',
 invocation_id:'a'.repeat(32),binding:{session:'session-a',task:'task-a',chat:'chat-a'},role:'chat_reviewer',
 coverage:{sources:2,items:3,required_questions_open:1},issues:[],...extra});

// Missing historical diagnostics and incomplete semantic coverage stay quiet.
for(const value of [undefined,null,{},[],{...observation(),format:2},{...observation(),mode:'enforce'}]){
 assert.equal(C.normalize(value),null);assert.equal(C.inline([value]),'');
}
assert.equal(C.inline([observation()]),'');
assert.equal(C.inline([observation({status:'unavailable',issues:[{code:'missing_source',kind:'structural'}]})]),'');
const getter={get format(){throw Error('bad diagnostic');}};
assert.equal(C.normalize(getter),null);
assert.doesNotThrow(()=>C.collect([null,{},getter],observation()));

// Inline text uses inert host labels, never document/model prose or URI syntax.
const unsafe='<img src=x onerror=alert(1)> [run](command:evil) https://example.invalid';
const withIssues=observation({request:unsafe,sources:[{text:'PRIVATE SOURCE'}],
 issues:[{code:'circular_support',kind:'structural',detail:unsafe,items:['claim-a','claim-b']},
  {code:'possible_contradiction',kind:'semantic_indicator',detail:unsafe}]});
const inline=C.inline([withIssues]);
assert(inline.includes('Coherence indicators · observe only'));
assert(inline.includes('A support chain is circular'));
assert(inline.includes('Possible conflicting statements (unverified)'));
assert(inline.includes('do not verify correctness'));
for(const text of ['<img','command:','https://','PRIVATE SOURCE','[run]'])assert(!inline.includes(text));
const normalized=C.normalize(withIssues);
assert.equal(normalized.issues[0].detail,unsafe);
assert(!('request' in normalized));assert(!('sources' in normalized));
assert.deepEqual(normalized.analysis_limits,{structured_only:true,semantic_correctness_verified:false});

// Malformed or oversized metadata has fixed work/display bounds.
const huge='x'.repeat(1000000);
const many=observation({model:huge,binding:{session:huge,task:huge,chat:huge},
 coverage:{sources:0,invalid:-1,nan:NaN,infinite:Infinity,bool:true,text:'2'},
 issues:Array.from({length:1000},(_,i)=>({code:'issue_'+i,kind:'semantic_indicator',detail:huge,items:Array(100).fill(huge)}))});
const bounded=C.normalize(many);
assert.equal(bounded.model.length,128);assert.equal(bounded.binding.task.length,128);
assert.equal(bounded.issues.length,C.limits.issues);assert.equal(bounded.issues[0].detail.length,400);
assert.equal(bounded.issues[0].items.length,8);assert.equal(bounded.issues[0].items[0].length,96);
assert.deepEqual(bounded.coverage,{sources:0});
assert.equal(C.recent(Array(1000).fill({observation:many,gateway_status:'execute'})).length,C.limits.rows);
assert(C.recent([{observation:many,gateway_status:'execute'}])[0].gateway_status==='unknown');
assert.equal(C.inline([many]).split('\n').filter(line=>line.startsWith('- ')).length,4,'Three issue labels and one bounded remainder note');
assert(C.inline([many]).length<1000);
assert.equal(C.normalize(observation({issues:[{code:'[run](command:bad)',kind:'structural'},null,{code:'ok',kind:'permission'}]})).issues.length,0);

// Repeated stage forwarding does not create repeated notices or unbounded state.
let collected=[];
for(let i=0;i<30;i++)collected=C.collect(collected,observation({invocation_id:String(i),issues:[{code:'circular_support',kind:'structural'}]}));
assert.equal(collected.length,12);
collected=C.collect(collected,observation({invocation_id:'29',issues:[{code:'circular_support',kind:'structural'}]}));
assert.equal(collected.length,12);
assert.equal(C.inline(collected).split('\n').filter(line=>line.startsWith('- ')).length,1);
assert.equal(C.inline(C.collect([],observation())).length,0);
assert.equal(C.normalize(observation({kernel:{decision:'BRIDGE',grant:true}})).kernel.observation_only,true);
assert(!('grant' in C.normalize(observation({kernel:{decision:'BRIDGE',grant:true}})).kernel));
console.log(JSON.stringify({passed:true,checks:['legacy and partial-only quiet','bounded malformed metadata','inert escaped inline labels','semantic uncertainty','scoped display metadata','whole-turn deduplication and limits']},null,2));

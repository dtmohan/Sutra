const assert=require('node:assert/strict');
const {controller,report}=require('../capabilities');
function fixture(){
  const cfg={'tools.mode':'ask','tools.preapproved':{}},saved={},prompts=[];let armed=true,choice='Approve';
  const vscode={workspace:{isTrusted:true,getConfiguration:()=>({get:(k,d)=>cfg[k]??d})},window:{showWarningMessage:async(...args)=>{prompts.push(args);return typeof choice==='function'?choice():choice;}}};
  const context={workspaceState:{get:k=>saved[k],update:async(k,v)=>{saved[k]=v;}}};
  const deps={workspace:()=>'/repo',armed:()=>armed};
  const ui=controller(vscode,context,deps);
  const p={id:'p1',binding:{workspace:'/repo',registry_hash:'registry1'},capability:'ruff',action:'check',version:'1',source_versions:{'code.py':'sha'},cost:{operations:1,seconds:30,source_bytes:20}};
  return {cfg,saved,prompts,vscode,ui,p,context,deps,arm:x=>{armed=x;},choose:x=>{choice=x;}};
}
(async()=>{
 const h=fixture();h.choose(undefined);assert.equal(await h.ui.approve(h.p),null);assert.equal(h.prompts.length,1);
 h.choose('Approve');assert.equal((await h.ui.approve(h.p)).kind,'user');
 h.cfg['tools.mode']='disabled';await assert.rejects(h.ui.approve(h.p),/disabled/);
 h.cfg['tools.mode']='preapproved';h.cfg['tools.preapproved']={ruff:['check']};h.choose('Approve policy');
 assert.equal((await h.ui.approve(h.p)).kind,'workspace_policy');const count=h.prompts.length;
 const reloaded=controller(h.vscode,h.context,h.deps);assert.equal((await reloaded.approve({...h.p,id:'p2'})).kind,'workspace_policy');assert.equal(h.prompts.length,count);
 // Same workspace policy works after a model switch, but no model supplies it.
 h.cfg.model='larger';assert.equal((await reloaded.approve({...h.p,id:'p3'})).kind,'workspace_policy');assert.equal(h.prompts.length,count);
 h.cfg['tools.preapproved']={ruff:['check','install']};assert.equal((await h.ui.approve(h.p)).kind,'workspace_policy');assert.equal(h.prompts.length,count+1);
 await h.ui.approve({...h.p,binding:{...h.p.binding,registry_hash:'registry2'}});assert.equal(h.prompts.length,count+2);
 h.cfg['tools.mode']='ask';h.choose(()=>{h.cfg['tools.mode']='disabled';return 'Approve';});await assert.rejects(h.ui.approve(h.p),/changed/);
 h.cfg['tools.mode']='ask';h.choose('Approve');h.arm(false);await assert.rejects(h.ui.approve(h.p),/Arm/);h.arm(true);
 await assert.rejects(h.ui.approve(h.p,{isCancellationRequested:true}),/cancelled/);
 h.vscode.workspace.isTrusted=false;await assert.rejects(h.ui.approve(h.p),/trusted/);h.vscode.workspace.isTrusted=true;
 h.cfg['tools.preapproved']={shell:['run']};await assert.rejects(h.ui.approve(h.p),/registered/);
 const text=report({capability:'ruff',receipt:'a'.repeat(64),result:{status:'finished',output:JSON.stringify([{filename:'[click](command:evil)',message:'<script>bad</script>',location:{row:2}}])}});
 assert(text.includes('\\[click'));assert(text.includes('\\<script'));assert(!text.includes('[click](command:evil)'));
 console.log('Capability UI: decline, preapproval, reload/model switch, policy/registry changes, cancellation, trust and escaped diagnostics passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});

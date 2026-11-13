const vscode=require('vscode'),fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),assert=require('node:assert/strict');
const {execFile}=require('node:child_process');const exec=require('node:util').promisify(execFile);
exports.run=async()=>{
 const out=process.env.KARMA_SMOKE_OUTPUT,report={checks:[]};const phase=(name,extra={})=>{report.phase=name;Object.assign(report,extra);fs.writeFileSync(out,JSON.stringify(report,null,2)+'\n');};
 const token=new vscode.CancellationTokenSource();
 try{
  const ext=vscode.extensions.getExtension('deepak-local.karma-local-harness'),api=await ext.activate();assert.equal(api.version,require('../package.json').version);assert.deepEqual(Object.keys(api).sort(),['getRoutingStatus','getState','open','version']);
  const workspace=vscode.workspace.workspaceFolders[0].uri.fsPath,storage=path.join(process.env.KARMA_SMOKE_PROFILE,'User/globalStorage/deepak-local.karma-local-harness',crypto.createHash('sha256').update(workspace).digest('hex').slice(0,24));
  await vscode.workspace.getConfiguration('karma').update('models',{low:'qwen3:4b',mid:'qwen3.6:35b',high:'local-assistant:latest'},vscode.ConfigurationTarget.Global);
  await exec(process.env.KARMA_SMOKE_PYTHON||(process.platform==='win32'?'python':'python3'),['-B',path.join(process.env.KARMA_SMOKE_DRIVER,'native-prepare.py'),ext.extensionPath,storage,workspace]);
  const models=await vscode.lm.selectChatModels({vendor:'karma'});report.models=models.map(m=>({id:m.id,name:m.name}));assert.equal(models.length,4);
  const tool=vscode.lm.tools.find(t=>t.name==='karma_native_action');assert(tool);report.checks.push('real VS Code discovers all four native models and governed action tool');
  const low=models.find(m=>m.name==='Sutra Low'),high=models.find(m=>m.name==='Sutra High');assert(low&&high);
  async function send(model,prompt){const r=await model.sendRequest(Array.isArray(prompt)?prompt:[vscode.LanguageModelChatMessage.User(prompt)],{tools:[tool]},token.token);const parts=[];for await(const part of r.stream)parts.push(part);return parts;}
  assert((await send(low,'Fix retry')).some(p=>p.value?.includes('Arm Task and Open Chat')));report.checks.push('real native model API refuses inference while disarmed');
  phase('arming',{storage});await vscode.commands.executeCommand('karma.armNative');const before=await api.getState();assert(before.native.armed);report.binding={session:before.session,contract:before.contract.contract_hash,calibration:before.calibration.binding.hash};
  report.checks.push('native QuickPick arming uses explicit task and permission confirmation');
  let continuation;
  for(const [model,prompt,status] of [[low,'Fix the off-by-one range(MAX_ATTEMPTS - 1) loop in src/retry.py with a minimal edit, then run its permitted tests. Leave test files unchanged.','applied'],[high,'Continue the pinned task. The host already applied the fix; run the permitted tests/test_retry.py. Leave all source unchanged.','process_exited_zero']]){
   phase('model '+model.name);const parts=await send(model,continuation||prompt);const call=parts.find(p=>p instanceof vscode.LanguageModelToolCallPart);report.last_response=parts.filter(p=>p instanceof vscode.LanguageModelTextPart).map(p=>p.value).join('');assert(call,report.last_response);assert.equal(call.name,'karma_native_action');
   phase('confirm '+status);const result=await vscode.lm.invokeTool(call.name,{input:call.input},token.token);const receipt=JSON.parse(result.content.map(p=>p.value||'').join(''));assert.equal(receipt.status,status);assert.equal(receipt.assertions_verified,false);
   report[status]=receipt;continuation=[vscode.LanguageModelChatMessage.Assistant([call]),vscode.LanguageModelChatMessage.User([new vscode.LanguageModelToolResultPart(call.callId,result.content)])];const current=await api.getState();assert.equal(current.session,before.session);assert.deepEqual(current.contract,before.contract);assert.deepEqual(current.calibration.binding,before.calibration.binding);assert(current.native.armed);
   report.checks.push(model.name+' completed governed '+status+' with unchanged task, constraints and calibration (high tier continues from tool receipt)');
   await assert.rejects(vscode.lm.invokeTool(call.name,{input:call.input},token.token),/ticket/);report.checks.push('native tool replay refused');
  }
  assert(fs.readFileSync(path.join(workspace,'src/retry.py'),'utf8').includes('range(MAX_ATTEMPTS):'));
  await vscode.commands.executeCommand('karma.disarmNative');assert(!(await api.getState()).native.armed);assert((await send(high,'Continue')).some(p=>p.value?.includes('Arm Task and Open Chat')));report.checks.push('status reports disarmed; native API blocks further calls');
  report.passed=true;phase('complete');
 }catch(e){report.passed=false;report.error=String(e.stack||e);phase('failed');throw e;}finally{token.dispose();fs.writeFileSync(out,JSON.stringify(report,null,2)+'\n');}
};

// Real VS Code smoke test for an extracted runtime-only VSIX.
const vscode=require('vscode'),fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),assert=require('node:assert/strict');
const {execFile}=require('node:child_process');const {promisify}=require('node:util');const exec=promisify(execFile);
exports.run=async()=>{
 const report={checks:[]},out=process.env.KARMA_SMOKE_OUTPUT;
 try{
  const extension=vscode.extensions.getExtension('deepak-local.karma-local-harness');
  const workspace=vscode.workspace.workspaceFolders[0].uri.fsPath;
  const storage=path.join(process.env.KARMA_SMOKE_PROFILE,'User','globalStorage','deepak-local.karma-local-harness',crypto.createHash('sha256').update(workspace).digest('hex').slice(0,24));
  const {ActivityArchive,activityUri}=require(path.join(extension.extensionPath,'model_activity'));
  const history=new ActivityArchive(storage),parent='1'.repeat(32),child='2'.repeat(32);
  history.accept({kind:'start',id:parent,model:'fixture-small',role:'conversation'});
  history.accept({kind:'thinking',id:parent,text:'Router fixture retained separately'});
  history.accept({kind:'finish',id:parent,status:'accepted'});
  history.accept({kind:'start',id:child,model:'fixture-large',role:'subagent',node_id:'child-fixture'});
  history.accept({kind:'thinking',id:child,text:'Child fixture reasoning survives reload'});history.dispose();
  const api=await extension.activate();
  const details=await vscode.workspace.openTextDocument(vscode.Uri.parse(activityUri(storage,child)));
  assert(details.getText().includes('Child fixture reasoning survives reload'));assert(details.getText().includes('Status: interrupted'));
  assert(!details.getText().includes('Router fixture'));assert.equal(details.uri.scheme,'karma-preview');assert.equal(details.languageId,'plaintext');
  await vscode.window.showTextDocument(details,{viewColumn:vscode.ViewColumn.Beside,preview:false});
  const foreign=await vscode.workspace.openTextDocument(vscode.Uri.parse('karma-preview:/activity/'+'f'.repeat(24)+'/'+child+'/Reasoning.txt'));
  assert(foreign.getText().includes('different workspace'));assert(!foreign.getText().includes('Child fixture reasoning'));
  report.checks.push('packaged reasoning details open in a read-only plain-text pane with restored child identity, interrupted status and workspace isolation');
  assert.equal(api.version,extension.packageJSON.version);assert.deepEqual(Object.keys(api).sort(),['getRoutingStatus','getState','open','version']);report.checks.push('packaged extension activates with restricted API');
  assert.equal(typeof vscode.chat?.createChatParticipant,'function');
  assert(extension.packageJSON.contributes.chatParticipants.some(p=>p.id==='karma.local'&&p.name==='sutra'));
  for(const name of ['karma.configureModels','karma.openAdvanced','karma.newChatReview','karma.resumeWorkflow','karma.discardWorkflow'])assert((await vscode.commands.getCommands(true)).includes(name));
  for(const command of ['help','models','dashboard','status','new'])assert(extension.packageJSON.contributes.chatParticipants[0].commands.some(c=>c.name===command));
  const html=fs.readFileSync(path.join(extension.extensionPath,'media/panel.html'),'utf8');
  assert(html.indexOf('id="chat-prompt"')<html.indexOf('id="advanced"'));assert(!/<details id="advanced"[^>]*\bopen\b/.test(html));
  for(const id of ['workflow-notice','open-current-task','use-model-defaults','model-suggestions'])assert(html.includes('id="'+id+'"'));
  for(const id of ['nav-dashboard','nav-chat','nav-settings','nav-tools','settings-section','troubleshooting','save-runtime'])assert(html.includes('id="'+id+'"'));
  assert(!html.includes('id="extra"'));report.checks.push('tabbed overview, labelled settings and troubleshooting ship without a JSON constraint editor');
  report.checks.push('recovery commands and discover-defaults controls ship in the packaged extension');
  report.checks.push('native chat API registers @sutra; model setup command available','simple prompt precedes collapsed advanced controls');
  assert((await vscode.commands.getCommands(true)).includes('karma.openReport'));
  for(const name of ['importCalibration','selectCalibrations','revokeCalibration','evaluateCalibration'])assert((await vscode.commands.getCommands(true)).includes('karma.'+name));
  // Fixture setup is a direct host request; no public extension mutation API.
  const setup=path.join(process.env.KARMA_SMOKE_DRIVER,'prepare.py');
  await exec(process.env.KARMA_SMOKE_PYTHON||'python3',[setup,extension.extensionPath,storage,workspace]);
  await api.open();await new Promise(resolve=>setTimeout(resolve,2000));
  const configured=vscode.workspace.getConfiguration('karma').get('models');
  assert(['low','mid','high'].every(t=>typeof configured[t]==='string'&&configured[t].length>0));
  report.model_defaults=configured;report.checks.push('installed local model metadata prefills all three tier settings in the isolated profile');
  if(process.platform==='darwin'&&process.arch==='arm64'){
    const native=JSON.parse((await exec(path.join(extension.extensionPath,'bin','telemetry'),['--once'])).stdout);
    if(native.temperatures.machine==='Mac17,13'){
      assert(native.temperatures.cpu.length>0&&native.temperatures.gpu.length>0);
      for(const row of [...native.temperatures.cpu,...native.temperatures.gpu])assert(Number.isFinite(row.celsius)&&row.celsius>0&&row.celsius<=150);
      report.sensor_sample=native.temperatures;report.checks.push('packaged AppleSMC helper reads real CPU and GPU Celsius on Mac17,13');
    }
  }
  const before=await api.getState();assert.equal(before.contract.contract.goal,'Packaged runtime smoke test');assert(before.auto.source_files>0);
  assert.equal(before.calibration.binding.profiles.length,2);assert(before.calibration.receipts_valid);
  assert.equal(before.calibration.last_evaluation.decision,'BRIDGE');assert.equal(before.calibration.last_evaluation.grants_tool_permission,false);
  const token=new vscode.CancellationTokenSource();const result=await vscode.lm.invokeTool('karma_auto_review',{input:{explain:false}},token.token);
  const data=JSON.parse(result.content.map(c=>c.value||'').join(''));report.mapping_outcome=data.auto;assert(data.auto.mapped_files>0);assert.deepEqual(data.state.contract,before.contract);
  assert(data.auto.budget.used.operations>before.auto.budget.used.operations);assert.equal(data.auto.budget.used.calls,0);
  await vscode.commands.executeCommand('karma.openAutomaticReport');report.checks.push('governed agent tool maps fresh source through packaged bridge and bundled TypeScript','task contract and allocation preserved','report opens in VS Code without a model call');
  const after=await api.getState();assert.equal(after.auto.pending_files,0);assert.deepEqual(after.calibration.binding,before.calibration.binding);
  report.checks.push('both domain adapters evaluate selected synthetic profiles through packaged bridge','calibration commands declared and receipts verify','calibration selection preserved during governed source mapping');
  report.calibration=after.calibration;report.state=after.auto;report.passed=true;token.dispose();
 }catch(error){report.passed=false;report.error=String(error.stack||error);throw error;}
 finally{fs.writeFileSync(out,JSON.stringify(report,null,2)+'\n');}
};

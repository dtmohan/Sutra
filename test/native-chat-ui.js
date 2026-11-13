const vscode=require('vscode'),fs=require('node:fs'),assert=require('node:assert/strict');
exports.run=async()=>{
 const out=process.env.KARMA_SMOKE_OUTPUT,report={checks:[]};function save(phase){report.phase=phase;fs.writeFileSync(out,JSON.stringify(report,null,2));}
 try{
 const ext=vscode.extensions.getExtension('deepak-local.karma-local-harness'),api=await ext.activate();const before=await api.getState();assert(!before.native.armed);report.binding=before.contract.contract_hash;report.checks.push('reload disarms while preserving the prior task');
 save('arming');await vscode.commands.executeCommand('karma.armNative');assert((await api.getState()).native.armed);
 await vscode.commands.executeCommand('workbench.action.chat.open');const trigger=out+'.send';if(fs.existsSync(trigger))fs.unlinkSync(trigger);save('ready');while(!fs.existsSync(trigger))await new Promise(r=>setTimeout(r,200));await vscode.commands.executeCommand('workbench.action.chat.open',{query:'Summarize the completed host actions and remaining work. Do not propose another edit or test.',mode:'agent',isPartialQuery:false});save('submitted');
 const flag=out+'.done';if(fs.existsSync(flag))fs.unlinkSync(flag);
 const start=Date.now();while(!fs.existsSync(flag)){if(Date.now()-start>600000)throw Error('Native chat UI response timeout');await new Promise(r=>setTimeout(r,200));}
 const after=await api.getState();assert.deepEqual(after.contract,before.contract);assert.deepEqual(after.calibration.binding,before.calibration.binding);assert(after.native.armed);report.checks.push('ordinary native chat rendered a Sutra response','task and calibration remain unchanged across reload/re-arm and native chat followup');report.passed=true;save('complete');
 }catch(e){report.passed=false;report.error=String(e.stack||e);save('failed');throw e;}
};

// Real VS Code native streaming API with deterministic transport events; no inference.
const vscode=require('vscode'),fs=require('node:fs'),assert=require('node:assert/strict');
exports.run=async context=>{
 const file=process.env.SUTRA_THINK_RESULT,report={passed:false,version:vscode.version};
 const save=()=>fs.writeFileSync(file,JSON.stringify(report,null,2));
 try{
  report.thinkingPartAvailable=typeof vscode.LanguageModelThinkingPart==='function';assert(report.thinkingPartAvailable);
  const {install}=require('../native_provider');let n=0,quiet=true;
  const models={low:'fixture-small',mid:'fixture-mid',high:'fixture-large'};
  install(vscode,context,{workspace:()=>vscode.workspace.workspaceFolders[0].uri.fsPath,isBusy:()=>false,configureModels:async()=>models,modelSettings:()=>models,onStatus:()=>{},activityLink:e=>'karma-preview:/activity/'+'a'.repeat(24)+'/'+e.id+'/Reasoning.txt',
   request:async(m,t,emit)=>{
    if(m.op==='orchestrate'&&m.event==='pending')return {state:'DONE',flow_id:null};
    if(m.op==='orchestrate'&&m.event==='start'){
     let id;for(const role of quiet?['conversation']:['conversation','subagent','chat_reviewer']){
     id=String(++n).padStart(32,'0');
     emit({kind:'start',id,role,model:models[m.tier],node_id:'fixture-child-'+n});
     emit({kind:'control',id,requested:false});
     if(!quiet)emit({kind:'thinking',id,text:'Sutra reasoning display fixture: compare the observed source relationships. This is unverified commentary.\n'});
     await new Promise(r=>setTimeout(r,700));
     emit({kind:'finish',id,status:'accepted',thinking_chars:quiet?0:140,usage:{input_tokens:30,output_tokens:40}});
     }
     return {flow_id:id,state:'DONE',conversation:{route:{model:models[m.tier]},result:{invocation_id:id,admission:{admitted:true},accepted_output:{answer:'Fixture accepted answer '+n+'.',summary:'fixture',intent:'conversation'}}}};
    }
    throw Error('Unexpected '+JSON.stringify(m));
   }});
  const discovered=await vscode.lm.selectChatModels({vendor:'karma'});assert.equal(discovered.length,4);
  const quietResponse=await discovered.find(m=>m.name==='Sutra Low').sendRequest([vscode.LanguageModelChatMessage.User('Hello fixture')],{},new vscode.CancellationTokenSource().token);
  const quietParts=[];for await(const part of quietResponse.stream)quietParts.push(part);
  assert.equal(quietParts.filter(p=>p instanceof vscode.LanguageModelThinkingPart).length,0);
  assert(quietParts.some(p=>p instanceof vscode.LanguageModelTextPart&&p.value.includes('No separate reasoning text received.')));
  report.metadataOnlyCall={thinkingParts:0,explicitAbsence:true};quiet=false;
  report.streams=[];
  for(const name of ['Sutra Low','Sutra High']){
   const model=discovered.find(m=>m.name===name);assert(model);
   const response=await model.sendRequest([vscode.LanguageModelChatMessage.User('Hello fixture')],{},new vscode.CancellationTokenSource().token);
   const parts=[];for await(const part of response.stream)parts.push(part);
   const thinking=parts.filter(p=>p instanceof vscode.LanguageModelThinkingPart);
   assert.equal(new Set(thinking.filter(p=>p.id).map(p=>p.id)).size,3);
   assert.equal(thinking.filter(p=>p.metadata?.vscode_reasoning_done).length,3);
   assert.equal(parts.filter(p=>p instanceof vscode.LanguageModelTextPart&&p.value.includes('/Reasoning.txt')).length,3);
   assert(thinking.length>=2);assert(thinking.some(p=>String(p.value).includes('Sutra reasoning display fixture')));
   assert(parts.filter(p=>p instanceof vscode.LanguageModelTextPart).some(p=>p.value.includes('Fixture accepted answer')));
   assert(!parts.filter(p=>p instanceof vscode.LanguageModelTextPart).some(p=>p.value.includes('unverified commentary')));
   report.streams.push({name,thinkingParts:thinking.length,id:thinking[0].id,header:thinking[0].value});
  }
  assert.notEqual(report.streams[0].id,report.streams[1].id);report.passed=true;report.phase='complete';save();
 }catch(e){report.error=String(e.stack||e);report.phase='failed';save();throw e;}
 finally{setTimeout(()=>vscode.commands.executeCommand('workbench.action.quit'),1500);}
};

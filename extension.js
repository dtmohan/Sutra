const vscode = require('vscode');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {spawn,execFile} = require('node:child_process');

const FIXED_TIERS = ['low', 'mid', 'high'];
const REASONING_PREFERENCES = ['adaptive', 'off', 'on', 'model-default'];
const TIERED_REQUESTS = ['orchestrate', 'chat_answer', 'conversation', 'native_work'];

function tierReasoning(config) {
  const settings = config.get('tierReasoning', {});
  return Object.fromEntries(FIXED_TIERS.map(tier => {
    const choice = settings?.[tier];
    return [tier, REASONING_PREFERENCES.includes(choice) ? choice : 'inherit'];
  }));
}

function reasoningPreference(config, tier) {
  const choice = FIXED_TIERS.includes(tier) ? tierReasoning(config)[tier] : 'inherit';
  return choice === 'inherit' ? config.get('thinking', 'adaptive') : choice;
}

function activate(context) {
  // Detect changed runtime files before loading local modules. This is a
  // consistency check, not protection against replacement of this entry point.
  const integrity=JSON.parse(fs.readFileSync(path.join(context.extensionPath,'runtime-integrity.json'),'utf8'));
  const kernel='harness/stp_canonical/kernel.py';
  if(integrity.format!==2||integrity.sha256[kernel]!=='9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac')throw new Error('Sutra canonical kernel integrity metadata is invalid');
  for(const [name,expected] of Object.entries(integrity.sha256)){
    if(path.isAbsolute(name)||name.split(/[\\/]/).includes('..'))throw new Error('Sutra runtime integrity path is invalid');
    if(crypto.createHash('sha256').update(fs.readFileSync(path.join(context.extensionPath,name))).digest('hex')!==expected)throw new Error('Sutra runtime integrity check failed: '+name);
  }
  const tokens=require('./media/token_usage');
  const modelSelection=require('./model_selection');
  const nativeProvider=require('./native_provider');
  const diagramView=require('./diagram_view');
  const usageView=require('./usage_view');
  const capabilityUI=require('./capabilities');
  const strataStartup=require('./strata_startup');
  const {BridgeWorker}=require('./bridge_worker');
  const output = vscode.window.createOutputChannel('Sutra');
  function providerStartupAdmission(coldMemoryBytes){
    const config=vscode.workspace.getConfiguration('karma');
    const python=config.inspect?.('pythonPath')?.globalValue||(process.platform==='win32'?'python':'python3');
    return new Promise(resolve=>{
      execFile(python,['-B',path.join(context.extensionPath,'harness','provider_startup.py'),
        ...(coldMemoryBytes===null?['--native-fit']:['--cold-memory-bytes',String(coldMemoryBytes)])],
        {timeout:8000,maxBuffer:65536,env:{...process.env,KARMA_NODE:process.execPath,ELECTRON_RUN_AS_NODE:'1'}},
        (error,stdout)=>{
          try{
            const admission=JSON.parse(stdout);
            if(error||typeof admission?.admitted!=='boolean')throw new Error('Startup admission unavailable');
            resolve(admission);
          }catch{resolve({admitted:false,reason:'Startup resource admission is unavailable. Check the Sutra output.'});}
        });
    });
  }
  async function startStrata(manual=false){
    if(manual)return manageStrata('load-selected');
    const config=vscode.workspace.getConfiguration('karma');
    if(config.inspect?.('strata.startOnStartup')?.globalValue!==true||config.inspect?.('models')?.globalValue?.high!=='strata:high'){
      return {status:'disabled',message:'Native Strata startup is not selected.'};
    }
    if(strataChanging||strataSetupRunning||busy||sequence||nativeController?.status().working){
      return {status:'busy',message:'Native model management or a Sutra request is in progress.'};
    }
    strataChanging=true;
    try{
      const result=await strataStartup.start(()=>vscode.workspace.getConfiguration('karma'),
        {remote:!!vscode.env?.remoteName,automatic:true,admit:providerStartupAdmission});
      if(!['disabled','unsupported','changed'].includes(result.status))output.appendLine('Strata High: '+result.message);
      return result;
    }finally{strataChanging=false;publishStrata();}
  }
  async function stopStrata(){
    return manageStrata('unload');
  }
  let nativeController, usage, panel, panelReady=false, panelSection, busy = false, sequence = false, telemetry, telemetryDir, dashboardTimer, dashboardInFlight=false, stopAuto=false, chatCancellation;
  let strataManager, strataState, strataChanging=false, strataSetupRunning=false;
  let lastReview;
  const previewDocuments=new Map(), previewChanges=new vscode.EventEmitter();
  function updateReports(data){
    const st=data?.state||data;if(!st?.session)return;
    for(const [name,body] of [['Architecture.md',st.report],['Reviewed-API-map.md',st.api?.report]]){
      const uri=vscode.Uri.parse('karma-preview:/'+st.session+'/'+name);
      if(previewDocuments.has(uri.toString())){previewDocuments.set(uri.toString(),body||'This report is withheld or stale. Continue the review to regenerate it.');previewChanges.fire(uri);}
    }
  }
  const processes = new Set();
  let dashboardWorker, requestWorker, requestWorkerKey, ramCompartments;
  function runtimeEnvironment(cwd) {
    const config = vscode.workspace.getConfiguration('karma');
    const strataPort = config.inspect?.('strata.port')?.globalValue;
    const strataModel = config.inspect?.('strata.model')?.globalValue;
    // Only user settings can authorize automatic grants. A repository setting
    // or a model tool payload cannot set or widen these limits.
    const userAllocation = config.inspect?.('workspaceAllocation')?.globalValue;
    const configured = userAllocation === undefined ? {} : userAllocation;
    const isObject = value => value !== null && typeof value === 'object' && !Array.isArray(value);
    let workspaceAllocation = configured;
    if (isObject(configured)) {
      const {byFolder = {}, ...limits} = configured;
      const overrides = isObject(byFolder) && Object.hasOwn(byFolder, cwd) ? byFolder[cwd] : {};
      workspaceAllocation = isObject(byFolder) && isObject(overrides) ? {...limits, ...overrides} : null;
    }
    return {
      ...(strataPort === undefined ? {} : {SUTRA_STRATA_PORT: strataPort === 0 ? undefined : String(strataPort)}),
      ...(strataModel === undefined ? {} : {SUTRA_STRATA_MODEL: typeof strataModel === 'string' ? strataModel.trim() : ''}),
      SUTRA_CAPABILITIES_MODE: config.get('tools.mode', 'ask'),
      SUTRA_RUNTIME_DIR: path.join(context.globalStorageUri.fsPath, 'runtime-memory'),
      SUTRA_MEMORY_OWNERSHIP: config.get('memoryOwnership', 'shared'),
      SUTRA_WORKSPACE_ALLOCATION: JSON.stringify(workspaceAllocation)
    };
  }
  const workspace = () => {
    if (!vscode.workspace.isTrusted) throw new Error('Sutra requires a trusted workspace.');
    const folder = vscode.workspace.workspaceFolders?.[0];
    if (!folder || folder.uri.scheme !== 'file') throw new Error('Open a local folder in VS Code first.');
    return folder.uri.fsPath;
  };
  const storage = () => path.join(context.globalStorageUri.fsPath,
    crypto.createHash('sha256').update(workspace()).digest('hex').slice(0,24));
  function startTelemetry() {
    const directory=storage();
    if(telemetry && telemetryDir===directory)return;
    telemetry?.kill();telemetryDir=directory;fs.mkdirSync(directory,{recursive:true});
    const native=process.platform==='darwin'&&process.arch==='arm64';
    telemetry=spawn(native?path.join(context.extensionPath,'bin','telemetry'):process.execPath,native?[]:[path.join(context.extensionPath,'harness','telemetry_portable.js')],{stdio:['ignore','pipe','pipe'],env:{...process.env,ELECTRON_RUN_AS_NODE:'1'}});
    let buffer='',history=[];
    telemetry.stdout.on('data',chunk=>{
      buffer+=chunk;let end;
      while((end=buffer.indexOf('\n'))>=0){
        const line=buffer.slice(0,end);buffer=buffer.slice(end+1);
        try{const sample=JSON.parse(line);history.push(sample);history=history.slice(-60);
          for(const [name,value] of [['telemetry.json',sample],['telemetry-history.json',history]]){
            fs.writeFileSync(path.join(directory,name+'.tmp'),JSON.stringify(value));fs.renameSync(path.join(directory,name+'.tmp'),path.join(directory,name));}
        }catch(e){output.appendLine('Telemetry: '+e.message);}
      }
    });
    telemetry.on('error',e=>output.appendLine('Telemetry unavailable: '+e.message));
    telemetry.on('close',()=>{telemetry=undefined;});
  }
  async function waitForTelemetry(directory,token){
    const started=Date.now();
    while(Date.now()-started<8000){
      if(token?.isCancellationRequested)throw new Error('Sutra request cancelled while waiting for hardware readings.');
      try{const sample=JSON.parse(fs.readFileSync(path.join(directory,'telemetry.json'),'utf8'));const age=Date.now()/1000-sample.wall_time;if(age>=0&&age<8)return;}catch{}
      await new Promise(resolve=>setTimeout(resolve,100));
    }
    throw new Error('Hardware readings are not available yet. Check the Sutra dashboard and retry; no model call was made.');
  }
  function hasDirtyFiles() {
    const base=workspace();return vscode.workspace.textDocuments.some(d=>d.isDirty && d.uri.scheme==='file' &&
      !path.relative(base,d.uri.fsPath).startsWith('..') && !path.isAbsolute(path.relative(base,d.uri.fsPath)));
  }
  function requestDashboard(){
    const cwd=workspace(),directory=storage();startTelemetry();
    if(dashboardWorker&&dashboardWorker.directory!==directory){dashboardWorker.child.kill();dashboardWorker=undefined;usage?.reset();}
    if(!dashboardWorker){
      const config=vscode.workspace.getConfiguration('karma'),python=config.get('pythonPath','')||(process.platform==='win32'?'python':'python3');
      const child=spawn(python,['-B',path.join(context.extensionPath,'harness','vscode_bridge.py'),'--workspace',cwd,'--storage',directory,'--dashboard-stream'],
        {cwd,stdio:['pipe','pipe','pipe'],env:{...process.env,...runtimeEnvironment(cwd),KARMA_STORAGE_LIMIT_MIB:String(config.get('storageLimitMiB',512))}});
      const worker={child,directory,buffer:'',pending:null};dashboardWorker=worker;processes.add(child);
      const fail=error=>{if(worker.pending){clearTimeout(worker.pending.timer);worker.pending.reject(error);worker.pending=null;}if(dashboardWorker===worker)dashboardWorker=undefined;};
      child.on('error',fail);child.on('close',()=>{processes.delete(child);fail(new Error('Dashboard worker stopped; the next refresh will restart it.'));});
      child.stdin.on('error',error=>{fail(error);child.kill();});child.stderr.on('data',()=>{});
      child.stdout.on('data',chunk=>{
        worker.buffer+=chunk;if(worker.buffer.length>4_000_000){fail(new Error('Dashboard response too large'));child.kill();return;}
        let end;while((end=worker.buffer.indexOf('\n'))>=0){
          const line=worker.buffer.slice(0,end);worker.buffer=worker.buffer.slice(end+1);const pending=worker.pending;if(!pending)continue;
          clearTimeout(pending.timer);worker.pending=null;
          try{const value=JSON.parse(line);if(!value.ok)throw new Error(value.error);pending.resolve(value.data);}catch(error){pending.reject(error);}
        }
      });
    }
    const worker=dashboardWorker;if(worker.pending)return worker.pending.promise;
    let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});
    const timer=setTimeout(()=>{worker.pending=null;reject(new Error('Dashboard read timed out'));worker.child.kill();if(dashboardWorker===worker)dashboardWorker=undefined;},10000);
    worker.pending={promise,resolve,reject,timer};worker.child.stdin.write('{}\n');return promise;
  }
  async function refreshDashboard(){
    if((!panel?.visible&&!usage?.visible)||dashboardInFlight)return;
    dashboardInFlight=true;
    try{
      const data=await request({op:'dashboard'});
      if(ramCompartments)data.ram_compartments=ramCompartments;
      panel?.webview.postMessage({type:'dashboard',data});
      usage?.setData(data,path.basename(workspace()));
    }catch(e){output.appendLine('Dashboard: '+e.message);panel?.webview.postMessage({type:'dashboard-error',message:e.message});usage?.setError(e.message);}
    finally{dashboardInFlight=false;}
  }
  async function request(message, token, internal=false, onActivity) {
    if(token?.isCancellationRequested)throw new Error("Sutra request cancelled before dispatch.");
    const {think_for_turn:thinkForTurn,conversation_step:conversationStep,...payload}=message||{};
    if(thinkForTurn!==undefined&&(thinkForTurn!==true||!internal||message.op!=='orchestrate'||!['start','step'].includes(message.event)))throw new Error('Invalid Sutra reasoning override.');
    if(conversationStep!==undefined&&(conversationStep!==true||!internal||message.op!=='orchestrate'||message.event!=='step'))throw new Error('Invalid Sutra conversation step.');
    const documentEvidence=message?.op==='orchestrate'&&['locate_document','approve_document'].includes(message.event);
    if(documentEvidence&&!internal)throw new Error('Document access must be requested in Sutra chat.');
    const diagnostic=message?.op==='dashboard';
    if(diagnostic)return requestDashboard();
    const managementRead=['state','models','conversation_read','capability_list','calibration_list'].includes(message?.op);
    if ((((strataChanging||strataSetupRunning)&&!managementRead) || busy || ((sequence || nativeController?.status().working) && !internal)) && !diagnostic) throw new Error('A Sutra request or native model operation is running. Switch models after it finishes.');
    if (!['capability_list','capability_preview','capability_execute','orchestrate','conversation_read','conversation','native_permissions','native_work','native_preview','native_execute','chat_answer','calibration_register','calibration_list','calibration_bind','calibration_revoke','calibration_evaluate','models','state','new','save','switch','review','work','preview','execute','dashboard','quarantine','review_new','architecture','repository_tool','api_build','api_verify','static_build','static_explain','auto_new','auto_step','auto_report'].includes(message?.op)) throw new Error('Unknown Sutra request.');
    if((['native_work','chat_answer','static_build','static_explain','auto_step','auto_report'].includes(message.op)||(message.op==='orchestrate'&&!conversationStep&&!documentEvidence&&!['start','inspect','pending','cancel','task_list','task_focus','task_close','supply_evidence'].includes(message.event)))&&hasDirtyFiles())throw new Error('Save or discard unsaved workspace buffers before mapping current source.');
    if(['execute','native_execute'].includes(message.op) && hasDirtyFiles())throw new Error('Save or discard unsaved workspace buffers before applying an edit or running tests.');
    const cwd = workspace(), stateDir = storage();
    fs.mkdirSync(stateDir,{recursive:true});startTelemetry();
    if(!diagnostic){busy = true;panel?.webview.postMessage({type:'busy',value:true});}
    try {
      if(message.op==='conversation')await waitForTelemetry(stateDir,token);
      const config=vscode.workspace.getConfiguration('karma');
      const python=config.get('pythonPath','')||(process.platform==='win32'?'python':'python3');
      const fixedTier=internal&&TIERED_REQUESTS.includes(payload.op)?payload.tier:undefined;
      const thinking=thinkForTurn?'on':reasoningPreference(config,fixedTier);
      const key=JSON.stringify([cwd,stateDir,python]);
      if(!requestWorker||requestWorker.closed||requestWorkerKey!==key){
        requestWorker?.dispose();requestWorkerKey=key;ramCompartments=undefined;
        requestWorker=new BridgeWorker({spawn,command:python,
          args:['-B',path.join(context.extensionPath,'harness','vscode_bridge.py'),
            '--workspace',cwd,'--storage',stateDir,'--request-stream'],
          options:{cwd,stdio:['pipe','pipe','pipe'],env:{...process.env}},
          activity:require('./model_activity').decoder,
          onSpawn:child=>processes.add(child),onClose:child=>{processes.delete(child);if(requestWorker?.child===child)ramCompartments=undefined;}});
      }
      const ramSetting=config.inspect?.('ramMemoryMiB')?.globalValue;
      const ramMiB=ramSetting===undefined?64:ramSetting;
      if(!Number.isInteger(ramMiB)||ramMiB<0||ramMiB>1024)throw new Error('Sutra RAM allocation must be 0..1024 MiB in User Settings.');
      const environment={...runtimeEnvironment(cwd),
        KARMA_STORAGE_LIMIT_MIB:String(config.get('storageLimitMiB',512)),
        SUTRA_LOGIT_SENSOR:({'off':'','numbers':'1','tokens':'tokens'})[config.get('logitSensor','off')]||'',
        SUTRA_ACTIVITY:'1',SUTRA_THINKING:thinking,KARMA_NODE:process.execPath,
        KARMA_TYPESCRIPT:path.join(context.extensionPath,'vendor','typescript','typescript.js'),
        SUTRA_RAM_CACHE_MIB:String(ramMiB)};
      const result=await requestWorker.send(payload,environment,{token,onActivity:event=>{usage?.activity(event);onActivity?.(event);}});
      if(result.host_memory)ramCompartments=result.host_memory;
      if(!result.ok)throw new Error(result.error||'Bridge request failed');
      output.appendLine(new Date().toISOString()+' '+message.op+' completed');
      updateReports(result.data);
      if(result.data?.result?.token_usage){usage?.recordResponse(result.data.result.token_usage);void refreshDashboard();}
      return result.data;
    } finally {if(!diagnostic){busy=false;panel?.webview.postMessage({type:'busy',value:sequence});}}
  }
  function publishReview(data) {
    if(data.result)panel?.webview.postMessage({type:'result',data:data.result});
    panel?.webview.postMessage({type:'state',data:data.state});
    if(data.routing)output.appendLine('KARMA_ROUTED '+JSON.stringify(data.routing));
    if(!data.result?.admission?.admitted||!data.report_markdown)return;
    const p=data.result.accepted_output,id=data.result.invocation_id;
    if(!/^[a-f0-9]{32}$/.test(id))return;
    const reportUri=vscode.Uri.parse('karma-preview:/review/'+id+'/Review.md');
    const sourceUri=vscode.Uri.parse('karma-preview:/review/'+id+'/Chat-source-map.md');
    const sources=(p.citations||[]).map(c=>'### '+c.path+'\n\n```\n'+c.quote+'\n```').join('\n\n');
    previewDocuments.set(reportUri.toString(),data.report_markdown+'\n\n## Review evidence\n\nModel conclusions are unverified. Source anchors were checked against fresh reads.\n\n'+sources+'\n\n## Outstanding checks\n\n'+(p.remaining||[]).map(x=>'- '+x).join('\n')+(data.report_review?'\n\n## Claim review details\n\n```json\n'+JSON.stringify(data.report_review,null,2)+'\n```':'')+'\n\nProtocol invocation: '+id+'\n\n[Full static source map]('+sourceUri.toString()+')');
    if(data.diagram)previewDocuments.set(sourceUri.toString(),data.diagram);
    lastReview=data;
    return {reportUri:reportUri.toString(),sourceUri:sourceUri.toString()};
  }
  function modelSettings(){return vscode.workspace.getConfiguration('karma').get('models',{low:'',mid:'',high:''});}
  let modelCatalog;
  function publishSettings(){
    nativeController?.publish();
    panel?.webview.postMessage({type:'tier-settings',models:modelSettings(),catalog:modelCatalog});
    const config=vscode.workspace.getConfiguration('karma');panel?.webview.postMessage({type:'runtime-settings',settings:{storageLimitMiB:config.get('storageLimitMiB',512),pythonPath:config.get('pythonPath',''),thinking:config.get('thinking','adaptive'),tierReasoning:tierReasoning(config),logitSensor:config.get('logitSensor','off'),memoryOwnership:config.get('memoryOwnership','shared')}});
    if(strataState)publishStrata();
  }
  function publishStrata(){
    const config=vscode.workspace.getConfiguration('karma');
    panel?.webview.postMessage({type:'strata-state',data:{...strataState,
      busy:strataChanging||strataSetupRunning,
      start_on_startup:config.inspect?.('strata.startOnStartup')?.globalValue===true}});
  }
  function nativeManager(){
    return strataManager??=require('./strata_management').create({
      admit:providerStartupAdmission,remote:!!vscode.env?.remoteName,
      busy:()=>busy||sequence||!!nativeController?.status().working||strataSetupRunning});
  }
  async function refreshStrata(){
    try{strataState=await nativeManager().discover();}
    catch(error){strataState={available:false,message:error.message,profiles:[],models:[]};}
    publishStrata();return strataState;
  }
  async function writeNativeRecord(record){
    const target=path.join(require('node:os').homedir(),strataStartup.startupPath);
    await fs.promises.mkdir(path.dirname(target),{recursive:true,mode:0o700});
    const temporary=target+'.'+crypto.randomBytes(8).toString('hex')+'.tmp';
    try{
      await fs.promises.writeFile(temporary,JSON.stringify(record,null,2)+'\n',{flag:'wx',mode:0o600});
      await fs.promises.rename(temporary,target);
    }finally{await fs.promises.unlink(temporary).catch(()=>{});}
  }
  async function locateStrata(){
    const setup=require('./strata_setup');
    let native=await setup.findNative({remote:!!vscode.env?.remoteName});
    if(native.status!=='found'){
      if(native.status==='unsupported')throw new Error(native.message);
      const picked=await vscode.window.showOpenDialog({title:'Locate the installed native Strata executable',
        canSelectFiles:true,canSelectFolders:false,canSelectMany:false,openLabel:'Use Strata'});
      if(!picked?.length)return;
      native=await setup.inspectNative(picked[0].fsPath,{});
    }
    if(native?.verified!==true)throw new Error('A native Strata installation could not be verified.');
    let previous={};try{previous=await strataStartup.readStartupProfile();}catch{}
    const same=previous.executable===native.executable;
    await writeNativeRecord({... (same?previous:{port:19483,model:'',profile:'',cold_memory_bytes:null}),
      executable:native.executable,environment:native.environment||{}});
    strataManager=undefined;
    return refreshStrata();
  }
  async function setupStrata(){
    const setup=require('./strata_setup');
    let native=await setup.findNative({remote:!!vscode.env?.remoteName});
    if(native.status!=='found'){await locateStrata();native=await setup.findNative({remote:!!vscode.env?.remoteName});}
    if(native.status!=='found')return;
    const prepared=await setup.prepareSetup(native,{port:vscode.workspace.getConfiguration('karma').inspect?.('strata.port')?.globalValue||19483});
    if(prepared.already_installed)return {
      status:'installed',message:prepared.model+' is already installed. Choose Add installed model to register its configuration, then Load selected. No download was started.'
    };
    const approved=await vscode.window.showInformationMessage('Install '+prepared.model+' with native Strata?',
      {modal:true,detail:prepared.message+' The native installer writes a serving configuration without starting a model. Return here and choose Add installed model, then Load.'},'Install model');
    if(approved!=='Install model')return;
    const admission=await providerStartupAdmission(null);
    if(admission.admitted!==true)throw new Error('Native setup paused. '+admission.reason);
    strataSetupRunning=true;publishStrata();
    let execution;
    const ended=new Set();
    const subscription=vscode.tasks.onDidEndTask(event=>{
      ended.add(event.execution);
      if(event.execution!==execution)return;
      subscription.dispose();strataSetupRunning=false;void refreshStrata();
    });
    try{
      const started=await setup.startSetupTask(vscode,native,{explicitSetup:true,plan:prepared.plan});
      execution=started.execution||started;
      if(ended.has(execution)){subscription.dispose();strataSetupRunning=false;}
      context.subscriptions.push(subscription);
      return {status:'setup',message:'The native model installer is open in the Terminal panel. It installs without starting a runtime; use Add installed model and Load when it finishes.'};
    }catch(error){subscription.dispose();strataSetupRunning=false;throw error;}
  }
  async function manageStrata(action,id){
    if(process.platform!=='darwin'||vscode.env?.remoteName){
      throw new Error('Native Strata model management currently requires a local macOS VS Code window.');
    }
    if(strataChanging||strataSetupRunning||busy||sequence||nativeController?.status().working){
      throw new Error('Wait for the current Sutra request or native model operation to finish.');
    }
    strataChanging=true;publishStrata();
    try{
      let result;
      if(action==='locate')result=await locateStrata();
      else if(action==='setup')result=await setupStrata();
      else if(action==='add'){
        const catalog=await nativeManager().discover();
        const choices=(catalog.models||[]).map(model=>({label:model.name,description:'Installed native model',id:model.id}));
        choices.push({label:'Choose a native run configuration…',id:'pick-config'});
        const choice=await vscode.window.showQuickPick(choices,{title:'Add a native Strata serving profile'});
        if(!choice)return;
        if(choice.id==='pick-config'){
          const picked=await vscode.window.showOpenDialog({title:'Choose a native Strata run configuration',
            canSelectFiles:true,canSelectMany:false,filters:{'Strata run configuration':['json']}});
          if(!picked?.length)return;
          result=await nativeManager().importConfig(picked[0].fsPath);
        }else result=await nativeManager().importInstalled(choice.id);
      }else if(action==='unload')result=await nativeManager().unload();
      else{
        if(action==='load-selected'){
          const catalog=await nativeManager().discover();id=catalog.profiles?.find(profile=>profile.selected)?.id;
          if(!id){await showPanelSection('models');return;}
        }
        if(typeof id!=='string'||id.length>200)throw new Error('Choose an installed native profile.');
        result=await nativeManager().load(id);
        if(result.status==='ready'&&result.selected){
          const config=vscode.workspace.getConfiguration('karma');
          await config.update('strata.port',result.selected.port,vscode.ConfigurationTarget.Global);
          await config.update('strata.model',result.selected.model,vscode.ConfigurationTarget.Global);
          await config.update('models',{...modelSettings(),high:'strata:high'},vscode.ConfigurationTarget.Global);
        }
      }
      if(result?.message)output.appendLine('Strata management: '+result.message);
      const observed=await refreshStrata();
      if(result?.message){strataState={...observed,message:result.message};publishStrata();}
      return result;
    }catch(error){strataState={...strataState,message:error.message,error:error.message};publishStrata();throw error;}
    finally{
      strataChanging=false;publishStrata();
      if(['load','load-selected','unload'].includes(action)){
        try{await discoverModels();}catch(error){output.appendLine('Model discovery: '+error.message);}
      }
    }
  }
  async function discoverModels(token,internal=false){
    let catalog;try{catalog=await request({op:'models'},token,internal);}catch(error){modelCatalog={models:[],suggested_tiers:{},error:'Cannot discover local models. Start Ollama, then choose Retry model discovery. '+error.message};publishSettings();throw new Error(modelCatalog.error);}
    const current=modelSettings(),next=modelSelection.prefill(current,catalog);
    if(JSON.stringify(current)!==JSON.stringify(next))await vscode.workspace.getConfiguration('karma').update('models',next,vscode.ConfigurationTarget.Global);
    modelCatalog=catalog;publishSettings();return catalog;
  }
  function focusPanelSection(){if(panelReady&&panelSection){panel?.webview.postMessage({type:'show-section',section:panelSection});panelSection=undefined;}}
  async function showPanelSection(section){panelSection=section;await open();focusPanelSection();}
  async function configureModels(force=true,token,internal=false,{tier='auto',onFallback}={}){
    if(force){await showPanelSection('models');return modelSettings();}
    const catalog=await discoverModels(token,internal);
    if(tier==='auto'&&!catalog.models.some(m=>m.eligible))throw new Error('No local chat models found. Start Ollama, then refresh the model list.');
    const {models,fallbacks}=modelSelection.forRequest(modelSettings(),catalog,tier);
    if(fallbacks.length)onFallback?.(fallbacks);
    return models;
  }
  const helpMessage='Sutra is ready. Ask about the open workspace, or use /new followed by a review request to start a separate read-only task. Open Model settings for installed-model dropdowns; open the dashboard for CPU/GPU temperatures. Existing task constraints stay in force.';
  function notice(kind,message){const data={notice:{kind,message}};panel?.webview.postMessage({type:'chat-notice',data:data.notice});return data;}
  function entryButtons(stream,workflow=false){
    if(workflow)stream.button({command:'karma.openAdvanced',title:'Open current task controls'});
    stream.button({command:'karma.newChatReview',title:'Start separate review'});
    stream.button({command:'karma.configureModels',title:'Model settings'});
    stream.button({command:'karma.open',title:'Open Sutra dashboard'});
    stream.button({command:'karma.openUsage',title:'Tokens, system & kernel'});
  }
  async function newChatReview(){
    // Commands ignore arguments from other extensions. Only the user supplies the new goal.
    const prompt=await vscode.window.showInputBox({title:'Start a separate read-only review',prompt:'Scope: this workspace. The existing task is retained in history; the new review becomes active.',placeHolder:'Review this codebase and explain its architecture.'});
    if(!prompt?.trim())return;
    await showPanelSection('chat');return simpleChat(prompt,{fresh:true,progress:message=>panel?.webview.postMessage({type:'chat-progress',message})});
  }
  async function simpleChat(prompt,{fresh=false,tier='auto',token,conversationId,progress=()=>{}}={}){
    if(token?.isCancellationRequested)throw new Error('Sutra request cancelled before dispatch.');
    prompt=String(prompt||'');if(!prompt.trim()||prompt.length>1800)throw new Error('Enter a request of 1–1800 characters.');
    if(!fresh&&/^(?:hi|hello|hey|help|hi karma|hello karma|hey karma)[.!?\s]*$/i.test(prompt))return notice('help',helpMessage);
    if(busy||sequence)throw new Error('A Sutra task is running. Stop it or wait for it to finish.');
    if(hasDirtyFiles())throw new Error('Save or discard unsaved files so Sutra can read current source.');
    sequence=true;panel?.webview.postMessage({type:'busy',value:true});
    try{
      let st=await request({op:'state',review_prompt:prompt},token,true);
      if(st.request_notice)return notice('input',st.request_notice);
      if(!fresh&&st.session&&!st.auto){
        panel?.webview.postMessage({type:'state',data:st});
        return notice('workflow','An existing task is active in Advanced controls. Open current task controls to continue with its scope and permissions, or start a separate read-only review. No model call was made and the active task was not replaced.');
      }
      const models=await configureModels(false,token,true,{tier,onFallback:rows=>{
        for(const row of rows)progress(modelSelection.fallbackMessage(row));
      }});
      if(fresh||!st.session){
        st=await request({op:'auto_new',goal:prompt,roots:['.']},token,true);
      }
      if(!st.auto)throw new Error('The review task could not be initialized. Open the Sutra dashboard to inspect its state.');
      const binding={session:st.session,contract_hash:st.contract.contract_hash,calibration_hash:st.calibration.binding.hash};
      let childUsage={calls:[]};
      let flow=await request({op:'orchestrate',event:'start_review',prompt,models,tier,binding,armed:true,conversation_id:conversationId},token,true);
      if(flow.request_notice)return notice('input',flow.request_notice);
      while(flow.state!=='DONE'){
        if(flow.recovery)throw new Error(flow.recovery.message+' Open Sutra chat to choose recovery.');
        if(token?.isCancellationRequested)throw new Error('Sutra request cancelled. Task state and budgets were preserved.');
        if(flow.state==='PAUSED')throw new Error('The review stopped at its allocation or source boundary. Inspect Sutra Monitor.');
        flow=await request({op:'orchestrate',event:'step',flow_id:flow.flow_id,cursor:flow.cursor,binding:flow.binding||binding,armed:true,models,tier},token,true);
        if(flow.progress)progress(flow.progress);
        if(flow.child)childUsage=tokens.merge(childUsage,flow.child.result?.token_usage);
      }
      const data=flow.data;if(!data)throw new Error('No admitted review result is available.');
      data.result.token_usage=tokens.merge(childUsage,data.result.token_usage);
      data.state=await request({op:'state'},token,true);
      panel?.webview.postMessage({type:'state',data:data.state});panel?.webview.postMessage({type:'chat-result',data});
      output.appendLine('KARMA_CHAT_ROUTE '+JSON.stringify(data.route));
      if(data.diagram){const uri=vscode.Uri.parse('karma-preview:/'+data.state.session+'/'+data.result.invocation_id+'/Chat-source-map.md');previewDocuments.set(uri.toString(),data.diagram);data.diagramUri=uri;}
      publishReview(data);return data;
    }finally{sequence=false;panel?.webview.postMessage({type:'busy',value:false});}
  }
  if(vscode.chat?.createChatParticipant){
    const participant=vscode.chat.createChatParticipant('karma.local',async(req,chatContext,stream,token)=>{
      try{
        if(token?.isCancellationRequested)return;
        if(req.command==='help'){stream.markdown(helpMessage);entryButtons(stream);return;}
        if(req.command==='models'){await configureModels();stream.markdown('Opened the installed-model dropdowns. Saved choices are preserved. To replace them, choose Use discovered defaults, then Save choices.');return;}
        if(req.command==='dashboard'){await showPanelSection('dashboard');stream.markdown('Opened the live Sutra dashboard.');return;}
        if(req.command==='usage'){await vscode.commands.executeCommand('karma.openUsage');stream.markdown('Opened Sutra Monitor beside Terminal and Output. You can also reopen it from the status bar.');return;}
        if(req.command==='new'&&!req.prompt.trim()){await newChatReview();return;}
        if(req.command==='status'){
          const d=await request({op:'dashboard'}),t=d.hardware.temperatures;
          const temperature=group=>t?.[group]?.available&&Number.isFinite(t[group].maximum_celsius)?t[group].maximum_celsius.toFixed(1)+' °C':'unavailable';
          stream.markdown(`CPU sensor max: **${temperature('cpu')}** · GPU sensor max: **${temperature('gpu')}**. Thermal state: **${d.hardware.thermal_label||'unavailable'}**. Celsius readings are advisory.`);stream.button({command:'karma.open',title:'Open Sutra dashboard'});return;
        }
        const data=await simpleChat(req.prompt,{fresh:req.command==='new',tier:['low','mid','high'].includes(req.command)?req.command:'auto',token,progress:m=>stream.progress(m)});
        if(!data)return;
        if(data.notice){stream.markdown(data.notice.message);entryButtons(stream,data.notice.kind==='workflow');return;}
        stream.markdown(`**${data.route.tier} · ${data.route.model}** — ${data.route.reason}\n\n`);
        if(data.result.admission.admitted){const p=data.result.accepted_output;const md=new vscode.MarkdownString(data.report_markdown||p.answer);md.isTrusted=false;stream.markdown(md);
          stream.markdown(`\n\nMapped ${data.coverage.mapped_files}/${data.coverage.source_files} files. This answer inspected excerpts from ${data.source_files.join(', ')}.\n\nRemaining: ${p.remaining.join('; ')||'No further step proposed.'}`);
        }else stream.markdown('The response was withheld by Sutra checks. '+data.result.admission.errors.join('; '));
        stream.markdown('\n\n---\nSutra · '+tokens.compact(data.result.token_usage));
        stream.button({command:'karma.openUsage',title:'Open Sutra Monitor'});
        if(data.review_diagram)await diagrams.openReview(data);
        if(data.diagramUri)stream.reference(data.diagramUri);
        stream.button({command:'karma.open',title:'Open Sutra dashboard'});
        return {metadata:{model:data.route.model,tier:data.route.tier,invocationId:data.result.invocation_id}};
      }catch(error){entryButtons(stream);return {errorDetails:{message:error.message}};}
    });
    participant.iconPath=vscode.Uri.file(path.join(context.extensionPath,'media','karma.svg'));context.subscriptions.push(participant);
  }
  async function runReview(prompt, steps=1, token, mode='explore', runId) {
    if(!Number.isInteger(steps)||steps<1||steps>4)throw new Error('Choose 1..4 review steps.');
    if(sequence||busy)throw new Error('A Sutra request is running.');
    const runStep=async mode=>{
      const data=await request({op:'architecture',mode,prompt,run_id:runId},token,true);
      publishReview(data);
      return data;
    };
    sequence=true;let data;
    try {
      for(let i=0;i<steps;i++) {
        data=await runStep(mode);
        const kind=data.result.accepted_output?.tool.kind;
        if(!data.result.admission.admitted||kind==='report'||kind==='clarify')return data;
        if(data.tool_receipt?.result.status!=='ok')break;
      }
      // The final exploration step can supply the last excerpt. Give the
      // host a report step without granting another exploration operation.
      return mode==='guided'?await runStep('report'):data;
    }finally{sequence=false;panel?.webview.postMessage({type:'busy',value:false});}
  }
  async function runAuto(options={}){
    if(busy||sequence)throw new Error('A Sutra request is running.');
    sequence=true;stopAuto=false;panel?.webview.postMessage({type:'auto-running',value:true});let data;
    try{while(!stopAuto){data=await request({op:'auto_step',explain:options.explain!==false},undefined,true);publishReview(data);if(!['ready','page_rejected'].includes(data.auto.status))break;}return data;}
    finally{sequence=false;panel?.webview.postMessage({type:'auto-running',value:false});panel?.webview.postMessage({type:'busy',value:false});}
  }
  async function openAutoReport(){
    const data=await request({op:'auto_report'}),uri=vscode.Uri.parse('karma-preview:/'+data.state.session+'/'+Date.now()+'/Codebase-review.md');
    previewDocuments.set(uri.toString(),data.report);await vscode.window.showTextDocument(await vscode.workspace.openTextDocument(uri));return data.report;
  }
  async function automaticReview(){
    const goal=await vscode.window.showInputBox({title:'Sutra: whole-codebase review',value:'Review the whole codebase and explain its architecture with a diagram.'});if(!goal)return;
    const roots=(await vscode.window.showInputBox({title:'Read-only scope: relative directories or files, comma separated',value:'.'}))?.split(',').map(x=>x.trim());if(!roots)return;
    const data=await request({op:'auto_new',goal,roots});await open();panel?.webview.postMessage({type:'state',data});return data;
  }
  async function reviewCodebase(options) {
    workspace();
    const goal=await vscode.window.showInputBox({title:'Sutra: architecture review goal',value:'Review this codebase and explain its architecture with a diagram.'});
    if(!goal)return;
    const roots=(await vscode.window.showInputBox({title:'Read-only scope: relative directories or files, comma separated',value:'.'}))?.split(',').map(x=>x.trim());
    if(!roots)return;
    const data=await request({op:'review_new',goal,roots,max_tools:12});
    await open();panel?.webview.postMessage({type:'state',data});
    if(options?.run===false)return data;
    const result=await runReview(goal,4,undefined,'guided');
    if(result?.result?.accepted_output?.tool.kind==='report'&&result.state.report)await showArchitectureReport(result.state);
    return result;
  }
  async function recoverArchitectureEvidence(runId){
    const expected=runId??(await request({op:'state'})).architecture_stop?.run_id;
    if(typeof expected!=='string'||!/^[a-f0-9]{32}$/.test(expected))throw new Error('This review is ready to report or has no pending source read.');
    const result=await runReview(undefined,1,undefined,'recover_evidence',expected);
    if(result?.result?.accepted_output?.tool.kind==='report'&&result.state.report)await showArchitectureReport(result.state);
    return result;
  }
  async function showArchitectureReport(data) {
    if(!data.report)throw new Error('No current admitted architecture report. Continue the review first.');
    const uri=vscode.Uri.parse('karma-preview:/'+data.session+'/Architecture.md');
    previewDocuments.set(uri.toString(),data.report);
    const document=await vscode.workspace.openTextDocument(uri);await vscode.window.showTextDocument(document);
    return data.report;
  }
  async function openReport(){return showArchitectureReport(await request({op:'state'}));}
  async function refresh() {
    const state=await request({op:'state'});
    panel?.webview.postMessage({type:'state',data:state});
    try {const models=await discoverModels();panel?.webview.postMessage({type:'models',data:models.models.filter(model=>!model.tier)});}
    catch(e){panel?.webview.postMessage({type:'error',message:e.message});}
    return state;
  }
  async function open() {
    workspace();
    if(panel){panel.reveal();if(panelReady&&!busy&&!sequence)await refresh();return;}
    panelReady=false;
    panel=vscode.window.createWebviewPanel('karmaLocalHarness','Sutra',vscode.ViewColumn.One,
      {enableScripts:true,retainContextWhenHidden:true,localResourceRoots:[vscode.Uri.file(path.join(context.extensionPath,'media'))]});
    const nonce=crypto.randomBytes(18).toString('hex');
    const script=panel.webview.asWebviewUri(vscode.Uri.file(path.join(context.extensionPath,'media','panel.js')));
    panel.webview.html=fs.readFileSync(path.join(context.extensionPath,'media','panel.html'),'utf8')
      .replaceAll('NONCE_VALUE',nonce).replace('SCRIPT_URI',script.toString()).replace('USAGE_URI',panel.webview.asWebviewUri(vscode.Uri.file(path.join(context.extensionPath,'media','token_usage.js'))).toString());
    panel.onDidDispose(()=>{panel=undefined;panelReady=false;},null,context.subscriptions);
    panel.webview.onDidReceiveMessage(async message=>{
      try {
        if(message.type==='ready'){panelReady=true;publishSettings();void refreshStrata();if(!busy&&!sequence)await refresh();focusPanelSection();return;}
        if(message.op==='strata_refresh'){await refreshStrata();return;}
        if(message.op==='strata_load'){await manageStrata('load',message.id);return;}
        if(message.op==='strata_unload'){await manageStrata('unload');return;}
        if(message.op==='strata_add_config'){await manageStrata('add');return;}
        if(message.op==='strata_setup'){await manageStrata('setup');return;}
        if(message.op==='strata_locate'){await manageStrata('locate');return;}
        if(message.op==='strata_autostart'){
          if(typeof message.enabled!=='boolean'||strataChanging||strataSetupRunning||busy||sequence||nativeController?.status().working){
            throw new Error('Wait for current work, then choose whether to start the selected native profile.');
          }
          await vscode.workspace.getConfiguration('karma').update('strata.startOnStartup',message.enabled,vscode.ConfigurationTarget.Global);
          publishStrata();return;
        }
        if(message.op==='resume_workflow'){await vscode.commands.executeCommand('karma.resumeWorkflow');return;}
        if(message.op==='discard_workflow'){await vscode.commands.executeCommand('karma.discardWorkflow');return;}
        if(message.op==='open_usage'){await vscode.commands.executeCommand('karma.openUsage');return;}
        if(message.op==='native_use'){await vscode.commands.executeCommand('karma.useNativeChat');return;}
        if(message.op==='native_arm'){await vscode.commands.executeCommand('karma.armNative');return;}
        if(message.op==='native_disarm'){await vscode.commands.executeCommand('karma.disarmNative');return;}
        if(message.op==='native_toggle'){await vscode.commands.executeCommand('karma.toggleNative');return;}
        if(message.op==='chat_stop'){chatCancellation?.cancel();return;}
        if(message.op==='configure_models'){await configureModels();return;}
        if(message.op==='refresh_models'){await discoverModels();return;}
        if(message.op==='save_tier_reasoning'){
          if(busy||sequence||nativeController?.status().working)throw new Error('Wait for the current turn before changing reasoning preferences.');
          const settings=message.settings;
          if(!settings||typeof settings!=='object'||Object.keys(settings).length!==FIXED_TIERS.length||
              !FIXED_TIERS.every(tier=>Object.hasOwn(settings,tier)&&
                (settings[tier]==='inherit'||REASONING_PREFERENCES.includes(settings[tier])))){
            throw new Error('Choose a reasoning preference for each fixed Sutra tier.');
          }
          await vscode.workspace.getConfiguration('karma').update('tierReasoning',settings,vscode.ConfigurationTarget.Global);
          publishSettings();panel?.webview.postMessage({type:'tier-reasoning-saved'});return;
        }
        if(message.op==='save_runtime_settings'){
          if(busy||sequence)throw new Error('Wait for the current turn before changing runtime settings.');
          const s=message.settings;
          if(!s||Object.keys(s).some(k=>!['pythonPath','storageLimitMiB','thinking','logitSensor','memoryOwnership'].includes(k))||('memoryOwnership' in s&&!['shared','exclusive'].includes(s.memoryOwnership))||('logitSensor' in s&&!['off','numbers','tokens'].includes(s.logitSensor))||!Number.isInteger(s.storageLimitMiB)||s.storageLimitMiB<64||s.storageLimitMiB>8192||!['adaptive','off','on','model-default'].includes(s.thinking)||typeof s.pythonPath!=='string'||s.pythonPath.length>1024||/[\r\n\0]/.test(s.pythonPath))throw new Error('Use a history allocation of 64–8192 MiB and a single Python executable path.');
          const config=vscode.workspace.getConfiguration('karma');
          if(s.memoryOwnership==='exclusive'&&config.get('memoryOwnership','shared')!=='exclusive'){
            const approved=await vscode.window.showWarningMessage('Is local Ollama dedicated to Sutra?',{modal:true,detail:'This permits one explicit unload of a matching idle model after a completed Sutra call during resource recovery. Other clients using the same Ollama server could be interrupted. Keep Shared when using Ollama from other extensions or apps.'},'Use exclusively for Sutra');
            if(approved!=='Use exclusively for Sutra')return;
          }
          if('memoryOwnership' in s)await config.update('memoryOwnership',s.memoryOwnership,vscode.ConfigurationTarget.Global);
          await config.update('storageLimitMiB',s.storageLimitMiB,vscode.ConfigurationTarget.Global);
          await config.update('pythonPath',s.pythonPath.trim(),vscode.ConfigurationTarget.Global);
          await config.update('thinking',s.thinking,vscode.ConfigurationTarget.Global);
          if('logitSensor' in s)await config.update('logitSensor',s.logitSensor,vscode.ConfigurationTarget.Global);
          dashboardWorker?.child.kill();dashboardWorker=undefined;
          publishSettings();panel?.webview.postMessage({type:'runtime-saved'});return;
        }
        if(message.op==='save_model_tiers'){
          if(busy||sequence)throw new Error('Wait for the current turn before changing model settings.');
          const catalog=await discoverModels();const selected=modelSelection.validate(message.models,catalog);
          await vscode.workspace.getConfiguration('karma').update('models',selected,vscode.ConfigurationTarget.Global);publishSettings();panel?.webview.postMessage({type:'models-saved'});return;
        }
        if(message.op==='chat_send'){
          if(chatCancellation)throw new Error('A Sutra chat turn is running.');
          chatCancellation=new vscode.CancellationTokenSource();
          try{await simpleChat(message.prompt,{fresh:message.fresh===true,tier:message.tier,token:chatCancellation.token,progress:m=>panel?.webview.postMessage({type:'chat-progress',message:m})});}
          finally{chatCancellation.dispose();chatCancellation=undefined;}return;
        }
        if(message.op==='open_chat_diagram'){
          if(lastReview?.review_diagram){await diagrams.openReview(lastReview);return;}
          const match=[...previewDocuments.keys()].reverse().find(k=>k.endsWith('/Chat-source-map.md'));
          if(match)await vscode.window.showTextDocument(await vscode.workspace.openTextDocument(vscode.Uri.parse(match)));return;
        }
        if(message.op==='auto_pause'){stopAuto=true;return;}
        if(message.op==='auto_run'){await runAuto(message);return;}
        if(message.op==='open_auto_report'){await openAutoReport();return;}
        if(message.op==='architecture_run'){await runReview(message.prompt,message.steps,undefined,message.mode||'explore');return;}
        if(message.op==='architecture_read_and_retry'){await recoverArchitectureEvidence(message.run_id);return;}
        if(message.op==='open_api_report'){await openAPIReport();return;}
        if(message.op==='open_static_report'){await openStaticReport();return;}
        if(message.op==='open_report'){await openReport();return;}
        if(message.op==='diff'){
          const change=await request({op:'preview',invocation_id:message.invocation_id});
          if(!change.diff)throw new Error('This proposal is a test run; there is no edit diff.');
          const before=vscode.Uri.parse('karma-preview:/'+message.invocation_id+'/before.py');
          const after=vscode.Uri.parse('karma-preview:/'+message.invocation_id+'/after.py');
          previewDocuments.set(before.toString(),change.before);previewDocuments.set(after.toString(),change.after);
          await vscode.commands.executeCommand('vscode.diff',before,after,'Sutra proposed edit: '+change.target);return;
        }
        const data=await request(message);
        if(['save','new','auto_new','review_new'].includes(message.op))panel?.webview.postMessage({type:'draft-saved'});
        if(message.op==='review'||message.op==='work'||message.op==='architecture'){
          panel?.webview.postMessage({type:'result',data:data.result});
          panel?.webview.postMessage({type:'state',data:data.state});
        }else if(['repository_tool','api_build','api_verify','static_build','static_explain','auto_step'].includes(message.op)){
          if(data.result)panel?.webview.postMessage({type:'result',data:data.result});
          panel?.webview.postMessage({type:'state',data:data.state});
        }else if(message.op==='execute'){
          panel?.webview.postMessage({type:'action',data:data.action_receipt});
          panel?.webview.postMessage({type:'state',data:data.state});
        }else panel?.webview.postMessage({type:'state',data});
      }catch(e){
        if(typeof message.op==='string'&&message.op.startsWith('strata_')){
          strataState={...strataState,message:e.message,error:e.message};publishStrata();
        }
        panel?.webview.postMessage({type:'error',message:e.message});output.appendLine(e.message);
      }
    },null,context.subscriptions);
    void refreshDashboard();
  }
  async function openAPIReport(){
    const data=await request({op:'state'});
    if(!data.api?.report)throw new Error('No current reviewed API map. Build a draft, then run the smaller-model review.');
    const uri=vscode.Uri.parse('karma-preview:/'+data.session+'/Reviewed-API-map.md');
    previewDocuments.set(uri.toString(),data.api.report);await vscode.window.showTextDocument(await vscode.workspace.openTextDocument(uri));return data.api.report;
  }
  async function openStaticReport(){
    if(hasDirtyFiles())throw new Error('Save or discard unsaved buffers before viewing a current source map.');
    const data=await request({op:'state'});
    if(!data.static?.report)throw new Error(data.static?.error||'Build a fresh source map first.');
    const uri=vscode.Uri.parse('karma-preview:/'+data.session+'/'+data.static.receipt+'/Source-map.md');
    previewDocuments.set(uri.toString(),data.static.report);await vscode.window.showTextDocument(await vscode.workspace.openTextDocument(uri));return data.static.report;
  }
  async function staticMap(options={},explain=false){
    let st=await request({op:'state'});
    if(!st.architecture){await reviewCodebase({run:false});st=await request({op:'state'});}
    if(!st.architecture)return;
    const files=options.files??await vscode.window.showQuickPick(st.architecture.catalog.filter(n=>/\.(py|js|jsx|ts|tsx)$/.test(n)||n.endsWith('tsconfig.json')),{canPickMany:true,title:'Sutra: select up to 8 source/config files for a fresh map'});
    if(!files?.length)return;
    let model=options.model;
    if(explain&&!model){
      const {models}=await request({op:'models'}),preferred=st.static?.model||vscode.workspace.getConfiguration('karma').get('explanationModel','qwen3.6:35b');
      model=await vscode.window.showQuickPick(models.filter(m=>!m.tier).map(m=>m.name).sort((a,b)=>(b===preferred)-(a===preferred)),{title:'Sutra: local model to explain source facts'});
      if(!model)return;
    }
    const data=await request({op:explain?'static_explain':'static_build',files,...(options.offset!==undefined?{offset:options.offset}:{}),...(model?{model}:{})});
    await open();publishReview(data);return data;
  }
  async function apiMap(options){
    let st=await request({op:'state'});
    if(!st.architecture){await reviewCodebase({run:false});st=await request({op:'state'});}
    if(!st.architecture)return;
    const files=options?.files??await vscode.window.showQuickPick(st.architecture.catalog.filter(n=>/\.(py|js|jsx|ts|tsx)$/.test(n)),{canPickMany:true,title:'Sutra: choose up to 8 API source files'});
    if(!files?.length)return;await open();
    const draft=await request({op:'api_build',files,offset:options?.offset??0});publishReview(draft);
    if(!draft.result.admission.admitted||options?.review===false)return draft;
    const checked=await request({op:'api_verify',reviewer_model:options?.reviewer_model??'auto'});publishReview(checked);return checked;
  }
  async function readCalibrationJSON(title){
    workspace();const files=await vscode.window.showOpenDialog({title,canSelectMany:false,filters:{JSON:['json']}});if(!files?.length)return;
    const raw=await vscode.workspace.fs.readFile(files[0]);if(raw.length>64000)throw new Error('Calibration JSON must be at most 64 KB.');return JSON.parse(Buffer.from(raw).toString('utf8'));
  }
  async function importCalibration(){
    const profile=await readCalibrationJSON('Import a versioned calibration profile (evidence is declared, not independently verified)');if(!profile)return;
    await request({op:'calibration_register',profile});await refresh();
  }
  async function selectCalibrations(){
    const data=await request({op:'calibration_list'}),st=await request({op:'state'});if(!st.session)throw new Error('Create a task before selecting profiles.');
    const selected=new Set(data.binding.profiles.map(p=>p.hash));
    const items=data.profiles.filter(p=>p.status==='available').map(p=>({label:p.profile_id+' v'+p.version,description:p.adapter+' / '+p.domain+' / '+p.evidence_kind,picked:selected.has(p.hash),profile:p}));
    const chosen=await vscode.window.showQuickPick(items,{canPickMany:true,title:'Select calibration versions for this task; clear all to remove selections'});if(!chosen)return;
    await request({op:'calibration_bind',expected_hash:data.binding.hash,profiles:chosen.map(({profile:p})=>({profile_id:p.profile_id,version:p.version,hash:p.hash}))});await refresh();
  }
  async function revokeCalibration(){
    const data=await request({op:'calibration_list'});const chosen=await vscode.window.showQuickPick(data.profiles.map(p=>({label:p.profile_id+' v'+p.version,description:p.status,profile:p})),{title:'Revoke a calibration version; dependent results will be withheld'});if(!chosen)return;
    const reason=await vscode.window.showInputBox({title:'Reason for revocation'});if(!reason?.trim())return;
    const p=chosen.profile;await request({op:'calibration_revoke',profile_ref:{profile_id:p.profile_id,version:p.version,hash:p.hash},reason});await refresh();
  }
  async function evaluateCalibration(){
    const data=await request({op:'calibration_list'});const chosen=await vscode.window.showQuickPick(data.binding.profiles.map(p=>({label:p.profile_id+' v'+p.version,profile:p})),{title:'Choose a selected profile for an uncertain measurement'});if(!chosen)return;
    const sample=await readCalibrationJSON('Open a measurement sample (diagnostic evaluation; grants no tool permission)');if(!sample)return;
    const result=await request({op:'calibration_evaluate',profile_ref:chosen.profile,sample});
    const uri=vscode.Uri.parse('karma-preview:/calibration/'+result.evaluation.receipt_id+'.json');previewDocuments.set(uri.toString(),JSON.stringify(result.evaluation,null,2));await vscode.window.showTextDocument(await vscode.workspace.openTextDocument(uri));
  }
  async function switchModel() {
    const {models}=await request({op:'models'});
    const chosen=await vscode.window.showQuickPick(models.filter(m=>!m.tier).map(m=>m.name),{title:'Sutra: choose the model for the next turn'});
    if(chosen){const state=await request({op:'switch',model:chosen});panel?.webview.postMessage({type:'state',data:state});}
  }
  const status=vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right,20);
  status.text='$(layers) Sutra';status.command='karma.open';status.tooltip='Open the local harness';status.show();
  function conversationDetails(data, invocationId) {
    const answer = data.result.accepted_output;
    const citations = data.document_citations || [];
    const lines = [nativeProvider.conversationMarkdown(answer), '---', 'Model: '+data.route.model,
      tokens.markdown(data.result.token_usage), 'Conversation receipt: '+invocationId];
    if (citations.length) {
      lines.push('## Document quotations\n\n'+citations.map(citation => {
        const quote = citation.quote.replace(/[\r\n]+/g, ' ').replace(/[\\`*_{}\[\]()<>#|!]/g, '\\$&');
        return '- **'+citation.id+'** (extracted lines '+citation.start_line+'–'+citation.end_line+') '+quote;
      }).join('\n'));
    }
    if (answer.diagram) {
      lines.push('Open **Sutra: Open Last Diagram** to view the latest diagram.',
        '```mermaid\n'+diagramView.mermaidSource(answer.diagram)+'\n```');
    }
    return lines.join('\n\n')+'\n';
  }
  context.subscriptions.push(status,output,previewChanges,vscode.commands.registerCommand('karma.open',open),
    vscode.commands.registerCommand('karma.configureModels',()=>configureModels()),
    vscode.commands.registerCommand('karma.manageStrata',async()=>{await showPanelSection('models');return refreshStrata();}),
    vscode.commands.registerCommand('karma.startStrata',()=>startStrata(true)),
    vscode.commands.registerCommand('karma.stopStrata',()=>stopStrata()),
    vscode.commands.registerCommand('karma.configureReasoning',()=>showPanelSection('reasoning')),
    vscode.commands.registerCommand('karma.openAdvanced',()=>showPanelSection('advanced')),
    vscode.commands.registerCommand('karma.newChatReview',()=>newChatReview()),
    vscode.workspace.registerTextDocumentContentProvider('karma-preview', {
      onDidChange: previewChanges.event,
      provideTextDocumentContent(uri) {
        const value = uri.toString();
        if (value.startsWith('karma-preview:/activity/')) {
          return usage?.activityDocument(value) || 'Reasoning history is not ready.';
        }
        const match = value.match(/^karma-preview:\/conversation\/([a-f0-9]{32})\/Response\.md(?:\?checkpoint=[a-f0-9]{64})?$/);
        if (match) {
          return request({op:'conversation_read', invocation_id:match[1]})
            .then(data => conversationDetails(data, match[1]));
        }
        return previewDocuments.get(value) || '';
      },
    }),
    vscode.commands.registerCommand('karma.exportDebug',async()=>{const data=await request({op:'dashboard'});
      const uri=await vscode.window.showSaveDialog({defaultUri:vscode.Uri.file(path.join(workspace(),'karma-debug.json')),filters:{JSON:['json']}});
      if(uri)await vscode.workspace.fs.writeFile(uri,Buffer.from(JSON.stringify(data,null,2)+'\n'));}),
    vscode.commands.registerCommand('karma.reviewCodebase',()=>reviewCodebase()),
    vscode.commands.registerCommand('karma.continueReview',()=>runReview('Continue the architecture review with current evidence and constraints.',1,undefined,'guided')),
    vscode.commands.registerCommand('karma.architectureReadAndRetry',async runId=>{
      try{await recoverArchitectureEvidence(runId);}catch(error){await vscode.window.showErrorMessage(error.message);}
    }),
    vscode.commands.registerCommand('karma.openReport',openReport),
    vscode.commands.registerCommand('karma.apiMap',apiMap),
    vscode.commands.registerCommand('karma.openAPIReport',openAPIReport),
    vscode.commands.registerCommand('karma.staticMap',options=>staticMap(options)),
    vscode.commands.registerCommand('karma.explainMap',options=>staticMap(options,true)),
    vscode.commands.registerCommand('karma.openStaticReport',openStaticReport),
    vscode.commands.registerCommand('karma.automaticReview',()=>automaticReview()),
    vscode.commands.registerCommand('karma.continueAutomaticReview',()=>runAuto()),
    vscode.commands.registerCommand('karma.openAutomaticReport',openAutoReport),
    vscode.commands.registerCommand('karma.importCalibration',()=>importCalibration()),
    vscode.commands.registerCommand('karma.selectCalibrations',()=>selectCalibrations()),
    vscode.commands.registerCommand('karma.revokeCalibration',()=>revokeCalibration()),
    vscode.commands.registerCommand('karma.evaluateCalibration',()=>evaluateCalibration()),
    vscode.commands.registerCommand('karma.switchModel',switchModel),{dispose(){clearInterval(dashboardTimer);requestWorker?.dispose();telemetry?.kill();for(const p of processes)p.kill('SIGTERM');}});
  const diagrams=diagramView.install(vscode,context,id=>request({op:'conversation_read',invocation_id:id}));
  usage=usageView.install(vscode,context,{refresh:refreshDashboard,storage,activityChanged:uri=>previewChanges.fire(uri)});
  dashboardTimer=setInterval(refreshDashboard,2000);
  function withWorkspaceAccess(token,invoke){
    if(!nativeController)throw new Error('Sutra native workspace permissions are unavailable in this VS Code version.');
    return nativeController.withWorkspaceAccess(token,invoke);
  }
  const toolController=capabilityUI.controller(vscode,context,{workspace,armed:()=>!!nativeController?.status().armed,
    request:(m,t)=>request(m,t,true),async showResult(r){const uri=vscode.Uri.parse('karma-preview:/tools/'+r.operation_id+'/Diagnostics.md');previewDocuments.set(uri.toString(),capabilityUI.report(r));await vscode.window.showTextDocument(await vscode.workspace.openTextDocument(uri));}});
  context.subscriptions.push(vscode.commands.registerCommand('karma.manageTools',()=>withWorkspaceAccess(undefined,t=>toolController.manage(t)).catch(e=>vscode.window.showErrorMessage(e.message))),vscode.commands.registerCommand('karma.configureTools',()=>toolController.configure().catch(e=>vscode.window.showErrorMessage(e.message))));
  const externalTools={
    karma_capabilities:async()=>request({op:'capability_list'}),
    karma_auto_review:async(input,token)=>request({op:'auto_step',explain:input.explain!==false},token),
    karma_review:async(input,token)=>runReview(input.prompt,input.steps??1,token,input.mode??'explore'),
    karma_source_map:async(input,token)=>request({op:'static_build',files:input.files,offset:input.offset??0},token),
    karma_explain_map:async(input,token)=>request({...input,op:'static_explain'},token),
    karma_read_file:async(input,token)=>request({op:'repository_tool',kind:'read',target:input.path,line:input.cursor?.line??input.line??1,cursor:input.cursor},token),
    karma_api_map:async(input,token)=>request({op:'api_build',files:input.files,offset:input.offset??0},token),
    karma_api_verify:async(input,token)=>request({op:'api_verify',reviewer_model:input.model??'auto'},token),
    karma_search:async(input,token)=>request({op:'repository_tool',kind:'search',query:input.query},token)
  };
  for(const [name,invoke] of Object.entries(externalTools))context.subscriptions.push(vscode.lm.registerTool(name,{
    async invoke(options,token){const data=await (name==='karma_capabilities'?invoke(options.input,token):
      withWorkspaceAccess(token,t=>invoke(options.input,t)));if(data.state)panel?.webview.postMessage({type:'state',data:data.state});
      if(data.tool_receipt)output.appendLine('KARMA_TOOL_RECEIPT '+data.tool_receipt.id);return new vscode.LanguageModelToolResult([new vscode.LanguageModelTextPart(JSON.stringify(data))]);}
  }));
  if(vscode.lm.registerLanguageModelChatProvider){
    nativeController=nativeProvider.install(vscode,context,{authorization:require('./workspace_authorization').createStore(vscode,context),request:(m,t,a)=>request(m,t,true,a),workspace,isBusy:()=>busy||sequence||strataChanging||strataSetupRunning,
      approveCapability:(p,t)=>toolController.approve(p,t),capabilityReport:capabilityUI.report,
      activityLink:event=>usage?.activityLink(event),modelSettings,configureModels:(force,t,options)=>configureModels(force,t,true,options),simpleChat,publishReview,
      async presentReview(data){await diagrams.openReview(data);},
      async presentConversation(data){if(data.result.accepted_output.diagram)await diagrams.open(data);},
      async showDiff(change,iid){const before=vscode.Uri.parse('karma-preview:/'+iid+'/before.py'),after=vscode.Uri.parse('karma-preview:/'+iid+'/after.py');previewDocuments.set(before.toString(),change.before);previewDocuments.set(after.toString(),change.after);await vscode.commands.executeCommand('vscode.diff',before,after,'Sutra proposed edit: '+change.target);},
      onRoute(value){output.appendLine('KARMA_NATIVE_ROUTE '+JSON.stringify(value));},
      onStatus(value){status.text=(value.routing?.active?'$(sync~spin) ':'$(shield) ')+value.label;status.tooltip=value.explanation+' '+value.reason+(value.routing?' Last Sutra request: '+value.routing.id+' · '+value.routing.stage:'');status.command='karma.openMonitor';panel?.webview.postMessage({type:'native-status',data:value});usage?.setStatus(value);}
    });context.subscriptions.push(nativeController);
  }
  const strataConfiguration=vscode.workspace.onDidChangeConfiguration?.(event=>{
    if(!strataChanging&&strataStartup.settingKeys.some(key=>event.affectsConfiguration('karma.'+key)))void startStrata();
  });
  if(strataConfiguration)context.subscriptions.push(strataConfiguration);
  void startStrata();
  // Public extension API cannot create contracts, widen scope, edit, or execute.
  return Object.freeze({open,getRoutingStatus:()=>structuredClone(nativeController?.status()||null),getState:async()=>({...await request({op:'state'}),native:nativeController?.status()}),version:context.extension.packageJSON.version});
}
exports.activate=activate;

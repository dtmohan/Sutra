'use strict';

const assert = require('node:assert/strict');
const {constants, mkdtempSync, writeFileSync, chmodSync, rmSync, mkdirSync,realpathSync} = require('node:fs');
const {tmpdir} = require('node:os');
const path = require('node:path');
const setup = require('../strata_setup');

const executable = '/trusted native/strata';
const saved = {
  executable, environment:{STRATA_INSTALL_DIR:'/private install', STRATA_NO_ACQUIRE:'1'},
  profile:'sutra-high', model:'flash', port:19483, cold_memory_bytes:20*1024**3
};
const profiles = {schema_version:1, ok:true, profiles:[{
  id:'sutra-high', revision:'a'.repeat(16), source_config:'/private install/sutra-high.json'
}]};
const installer = {verified:true,native_executable:executable,
  executable:'/trusted native/python/bin/python3',script:'/trusted native/setup.py'};
const plan = {settings:{family:'qwen',model:'Q2_0',context:32768,vision:'off',drafting:'off',mtp:'off',access:'local',port:19483},
  download:{gb:0,text:'Nothing to download.'},model:{title:'Native Qwen Q2_0'}};
const inspectedModel={ok:true,model:{id:'q2_0',source_config:'/private install/strata-q2_0.json',
  source_sha256:'b'.repeat(64),component_status:'present'}};
const fail = () => assert.fail('Unexpected side effect');

async function verify() {
  const calls = [];
  const inspect = async (file, args, options) => {
    calls.push({file, args, options});
    return JSON.stringify(profiles);
  };
  const native = await setup.findNative({
    home:'/user', platform:'darwin', readRecord:async () => saved,
    exists:async file => file === executable, run:inspect
  });
  assert.equal(native.status, 'found');
  assert.equal(native.source, 'startup');
  assert.deepEqual(native.profiles, profiles.profiles);
  assert.deepEqual(calls.map(call => call.args), [['profile', 'list', '--output', 'json']]);
  assert.equal(calls[0].options.environment.STRATA_INSTALL_DIR, '/private install');
  assert.equal(calls[0].options.environment.STRATA_NO_ACQUIRE, '1');
  const empty = await setup.findNative({
    home:'/user', platform:'darwin', readRecord:async () => ({executable, environment:{}}),
    exists:async file => file === executable,
    run:async () => ({schema_version:1, ok:true, profiles:[]})
  });
  assert.equal(empty.status, 'found');
  assert.deepEqual(empty.profiles, [], 'A new native runtime can enter setup before any model profile exists');
  assert.equal(empty.model, undefined, 'Discovery cannot invent a model binding');
  assert.equal(empty.port, undefined, 'Discovery cannot invent an endpoint binding');

  const managed = setup.nativeCandidates({home:'/user', platform:'darwin'})[0];
  const fallback = await setup.findNative({
    home:'/user', platform:'darwin', readRecord:async () => {throw new Error('No startup record');},
    exists:async file => file === managed.executable, run:inspect
  });
  assert.equal(fallback.source, 'managed');
  assert.equal(fallback.environment.STRATA_INSTALL_DIR, '/user/Library/Application Support/Sutra/Strata/runtime');
  assert.equal(fallback.environment.STRATA_COMPONENTS_DIR, '/user/Library/Application Support/Sutra/Strata/components');
  assert.equal(fallback.environment.STRATA_RUNTIME_DIR, '/user/.local/state/sutra-strata');

  const missing = await setup.findNative({
    home:'/user', platform:'linux', readRecord:async () => null, exists:async () => false, run:fail
  });
  assert.equal(missing.status, 'missing');
  assert.equal((await setup.findNative({remote:true, exists:fail, readRecord:fail})).status, 'unsupported');
  assert.equal((await setup.findNative({platform:'unknown', exists:fail, readRecord:fail})).status, 'unsupported');
  assert(setup.nativeCandidates({home:'/user', platform:'linux'}).some(item => item.executable === '/user/.local/bin/strata'));
  assert(setup.nativeCandidates({home:'/user', platform:'win32'}).every(item => item.executable.endsWith('.exe')));

  for (const response of [
    {ok:true, profiles:[]}, {schema_version:1, ok:false, profiles:[]},
    {...profiles, profiles:[{...profiles.profiles[0], id:'../escape'}]},
    {...profiles, profiles:[{...profiles.profiles[0], revision:'invalid'}]},
    {...profiles, profiles:[{...profiles.profiles[0], source_config:'relative.json'}]},
    'x'.repeat(262145)
  ]) {
    await assert.rejects(setup.inspectNative(executable, {}, {
      exists:async () => true, run:async () => response
    }));
  }
  await assert.rejects(setup.inspectNative('relative/strata', {}, {exists:fail, run:fail}));
  await assert.rejects(setup.inspectNative(executable, {LD_PRELOAD:'/workspace/library'}, {
    exists:async () => true, run:fail
  }));
  assert.throws(() => setup.trustedEnvironment({STRATA_INSTALL_DIR:'bad\npath'}));
  assert.throws(() => setup.setupCommand(native), /explicitly requested/);
  assert.throws(() => setup.setupCommand({...native, verified:false}, {explicitSetup:true}));
  assert.throws(() => setup.setupCommand(native, {explicitSetup:true,plan}), /bundled native/);
  const prepared=await setup.prepareSetup(native,{run:async (_file,args,options)=>{
    if(args[0]==='models'){
      assert.deepEqual(args,['models','inspect','q2_0','--install-dir','/private install','--output','json']);
      return inspectedModel;
    }
    assert(args.includes('--plan'));assert(args.includes('--output'));
    assert.equal(options.environment.STRATA_NO_ACQUIRE,'1');
    return {ok:true,plan};
  }});
  assert.equal(prepared.model,'Native Qwen Q2_0');assert.equal(prepared.download_gb,0);
  assert.equal(prepared.already_installed,true,'Present native files require import, not another installation');
  await assert.rejects(setup.prepareSetup(native,{run:async(_file,args)=>args[0]==='models'?{ok:false}:({ok:true,plan})}),/could not be verified/);
  await assert.rejects(setup.prepareSetup(native,{run:async(_file,args)=>args[0]==='models'?
    {...inspectedModel,ok:false,model:{...inspectedModel.model,component_status:'missing'}}:({ok:true,plan})}),/need repair/);
  const newPlan={...plan,download:{gb:66.4,text:'Download the recommended model.'}};
  const notInstalled=await setup.prepareSetup(native,{run:async(_file,args)=>args[0]==='models'?{ok:false}:({ok:true,plan:newPlan})});
  assert.equal(notInstalled.already_installed,false);assert.equal(notInstalled.download_gb,66.4);
  for(const broken of [
    {...plan,settings:{...plan.settings,port:0}},
    {...plan,settings:{...plan.settings,vision:'gpu'}},
    {...plan,settings:{...plan.settings,context:65536}},
    {...plan,download:{gb:-1,text:'Bad'}},
    {...plan,settings:{...plan.settings,model:'--bad'}}
  ])await assert.rejects(setup.prepareSetup(native,{run:async()=>({ok:true,plan:broken})}));
  assert.throws(()=>setup.setupCommand(native,{explicitSetup:true,installer,plan:{}}),/download plan/);
  const command = setup.setupCommand(native, {
    explicitSetup:true, cwd:'/user', installer,plan,
    environment:{PATH:'/system',STRATA_NO_ACQUIRE:'1',PYTHONHOME:'/wrong',PYTHONPATH:'/wrong',VIRTUAL_ENV:'/wrong'}
  });
  assert.deepEqual(command.args,[installer.script,'--setup','--no-start','--no-browser','--host','127.0.0.1',
    '--port','19483','--family','qwen','--model','Q2_0','--context','32768','--vision','no','--mtp','off']);
  assert.equal(command.executable,installer.executable);
  assert.equal(command.options.env.STRATA_NO_ACQUIRE, undefined);
  assert.equal(command.options.env.STRATA_INSTALL_DIR, '/private install');
  assert.equal(command.options.env.PATH, '/system');
  assert.equal(command.options.env.STRATA_CONTROL_INSTALL,'1');
  assert.equal(command.options.env.PYTHONNOUSERSITE,'1');
  for(const key of ['PYTHONHOME','PYTHONPATH','VIRTUAL_ENV'])assert.equal(command.options.env[key],undefined);
  assert.equal(native.environment.STRATA_NO_ACQUIRE, '1', 'Setup must not change the trusted discovery record');
  assert.equal(command.options.shell, undefined);

  const processes = [], tasks = [], executions = [];
  const vscode = {
    ProcessExecution:class {
      constructor(file, args, options) {Object.assign(this, {file,args,options}); processes.push(this);}
    },
    Task:class {
      constructor(definition,scope,name,source,execution,matchers) {
        Object.assign(this, {definition,scope,name,source,execution,matchers}); tasks.push(this);
      }
    },
    TaskScope:{Global:1}, TaskRevealKind:{Always:1}, TaskPanelKind:{Dedicated:2},
    tasks:{executeTask:async task => {const execution = {task}; executions.push(execution); return execution;}}
  };
  const launched = await setup.startSetupTask(vscode, native, {explicitSetup:true, cwd:'/user', environment:{},plan,
    resolveInstaller:async()=>installer});
  assert.equal(launched.status, 'started');
  assert.equal(launched.execution, executions[0]);
  assert.equal(launched.task, tasks[0]);
  assert.deepEqual(processes[0].args,command.args);
  assert.equal(tasks[0].presentationOptions.focus, true);
  assert.equal(tasks[0].presentationOptions.panel, vscode.TaskPanelKind.Dedicated);
  assert.equal(processes[0].options.env.STRATA_NO_ACQUIRE, undefined);

  // Exercise the bounded real subprocess path without launching the installed runtime.
  if (process.platform !== 'win32') {
    const temporary = mkdtempSync(path.join(tmpdir(), 'sutra-native-setup-'));
    try {
      const bin = path.join(temporary, 'native with spaces');
      const quoted = "'"+JSON.stringify(profiles).replaceAll("'", "'\\''")+"'";
      writeFileSync(bin, '#!/bin/sh\n'+
        '[ "$STRATA_NO_ACQUIRE" = "1" ] || exit 8\n'+
        '[ "$*" = "profile list --output json" ] || exit 9\n'+
        'printf "%s" '+quoted+'\n');
      chmodSync(bin, 0o700);
      assert.equal((await setup.inspectNative(bin)).verified, true);
      const control=path.join(temporary,'native bundle');
      mkdirSync(path.join(control,'bin'),{recursive:true});
      mkdirSync(path.join(control,'python','bin'),{recursive:true});
      const nativePath=path.join(control,'bin','strata');
      writeFileSync(nativePath,'#!/bin/sh\nexit 0\n');chmodSync(nativePath,0o700);
      writeFileSync(path.join(control,'setup.py'),'# Native installer fixture\n');
      const privatePython=path.join(control,'python','bin','python3');
      writeFileSync(privatePython,'#!/bin/sh\nexit 0\n');chmodSync(privatePython,0o700);
      const verifiedInstaller=await setup.resolveInstaller({...native,executable:nativePath});
      assert.equal(verifiedInstaller.executable,realpathSync(privatePython));assert.equal(verifiedInstaller.script,realpathSync(path.join(control,'setup.py')));
      rmSync(path.join(control,'setup.py'));
      await assert.rejects(setup.resolveInstaller({...native,executable:nativePath}),/no supported bundled installer/);
      await assert.rejects(setup.resolveInstaller({...native,executable:bin}),/no supported bundled installer/);
      const recordDir = path.join(temporary, 'Library', 'Application Support', 'Sutra', 'Strata');
      mkdirSync(recordDir, {recursive:true});
      writeFileSync(path.join(recordDir, 'startup.json'), JSON.stringify(saved));
      assert.deepEqual(await setup.readStartupRecord(temporary), saved);
      writeFileSync(path.join(recordDir, 'startup.json'), ' '.repeat(8193));
      await assert.rejects(setup.readStartupRecord(temporary));
      assert.equal(constants.X_OK, 1);
    } finally {
      rmSync(temporary, {recursive:true, force:true});
    }
  }
  console.log('Native Strata discovery and explicit setup task checks passed.');
}

verify().catch(error => {console.error(error); process.exitCode = 1;});

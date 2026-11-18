'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const net = require('node:net');
const path = require('node:path');
const vm = require('node:vm');
const startup = require('../strata_startup');

const user = {
  models:{high:'strata:high'}, 'strata.startOnStartup':true,
  'strata.port':19483, 'strata.model':'flash'
};
const getConfig = (values = user, workspace = {}) => () => ({
  inspect:key => ({globalValue:values[key], workspaceValue:workspace[key], workspaceFolderValue:workspace[key]})
});
const profile = {
  port:user['strata.port'], model:user['strata.model'],
  executable:'/installed Strata/bin/strata', profile:'sutra-high',
  environment:{STRATA_RUNTIME_DIR:'/private state', STRATA_NO_ACQUIRE:'1'},
  cold_memory_bytes:20*1024**3
};
const plan = {
  ok:true, plan:{profile:profile.profile, source_sha256:'a'.repeat(64), launch:{
    profile:profile.profile, profile_revision:'b'.repeat(16), model_name:profile.model,
    effective:{host:'127.0.0.1', port:profile.port}
  }}
};
const owner = 'c'.repeat(32), instance = 'd'.repeat(32);
const stopPlan = {ok:true, plan:{profile:profile.profile, owner_id:owner, instance_id:instance}};
const serverStatus = {service:'strata', model:profile.model, instance_id:instance, activity:{in_flight:0}};
const fail = () => assert.fail('Unexpected side effect');

function options(changes = {}) {
  return {
    platform:'darwin', remote:false, probe:async () => false,
    readProfile:async () => structuredClone(profile),
    admit:async bytes => {
      assert.equal(bytes, profile.cold_memory_bytes);
      return {admitted:true, decision:'ALLOW'};
    },
    run:fail,
    ...changes
  };
}

async function verify() {
  const inactive = options({probe:fail, readProfile:fail, admit:fail});
  assert.equal((await startup.start(getConfig({}, user), inactive)).status, 'disabled');
  assert.equal((await startup.start(getConfig({...user, models:{high:'ollama'}}, user), inactive)).status, 'disabled');
  assert.equal((await startup.start(getConfig({...user, 'strata.port':0}, user), inactive)).status, 'invalid');
  assert.equal((await startup.start(getConfig(), {...inactive, remote:true})).status, 'unsupported');
  assert.equal((await startup.start(getConfig(), {...inactive, platform:'linux'})).status, 'unsupported');

  const server = net.createServer();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const settings = {...user, 'strata.port':server.address().port};
    assert.equal((await startup.start(getConfig(settings), options({
      probe:undefined, readProfile:fail, admit:fail
    }))).status, 'occupied');
  } finally {
    await new Promise(resolve => server.close(resolve));
  }
  assert.equal((await startup.start(getConfig(), options({
    probe:async () => {throw new Error('Unknown port');}, readProfile:fail
  }))).status, 'unavailable');

  for (const bad of [
    null, {port:profile.port, model:profile.model},
    {...profile, executable:'relative/bin/strata'},
    {...profile, profile:'../../other'},
    {...profile, profile:'Uppercase'},
    {...profile, cold_memory_bytes:-1},
    {...profile, environment:{LD_PRELOAD:'/workspace/library'}},
    {...profile, port:19484},
    {...profile, model:'other'}
  ]) {
    assert.equal((await startup.start(getConfig(), options({readProfile:async () => bad}))).status, 'setup');
  }
  const changed = {...user};
  assert.equal((await startup.start(getConfig(changed), options({
    readProfile:async () => {
      changed['strata.startOnStartup'] = false;
      return profile;
    }
  }))).status, 'changed');

  for (const state of ['ready', 'starting']) {
    const result = await startup.start(getConfig(), options({
      run:async (_record, action) => {
        assert.equal(action, 'status');
        return {ok:true, profile:profile.profile, owner_id:owner, state};
      },
      admit:fail
    }));
    assert.equal(result.status, state === 'ready' ? 'unavailable' : 'starting');
  }
  assert.equal((await startup.start(getConfig(), options({
    run:async () => ({ok:false, state:'starting'}), admit:fail
  }))).status, 'unavailable');
  for (const invalidStatus of [
    {ok:true, state:'ready'},
    {ok:true, profile:'other-profile', state:'stopped'},
    {ok:true, profile:profile.profile, state:'ready', api_url:'http://127.0.0.1:19484'},
    {ok:true, profile:profile.profile, state:'starting'}
  ]) {
    const result = await startup.start(getConfig(), options({
      run:async (_record, action) => {
        assert.equal(action, 'status', 'An unbound runtime status must not launch a model');
        return invalidStatus;
      },
      admit:fail
    }));
    assert.equal(result.status, 'unavailable');
    assert.doesNotMatch(result.message, /runtime is ready/i);
  }

  let calls = [];
  const observe = async (_record, action, args = []) => {
    calls.push({action,args});
    if (action === 'status') return {ok:true, profile:profile.profile, state:'stopped'};
    if (args.includes('--plan')) return structuredClone(plan);
    return {ok:true, profile:profile.profile, state:'starting'};
  };
  const bound = {...profile, memory_binding:startup.memoryBinding(profile,plan.plan)};
  assert.equal((await startup.start(getConfig(), options({readProfile:async()=>bound,run:observe}))).status,'starting');
  assert.equal((await startup.start(getConfig(), options({
    readProfile:async()=>({...bound,memory_binding:{...bound.memory_binding,source_sha256:'f'.repeat(64)}}),
    run:observe,admit:fail
  }))).status,'changed');
  const firstLoad = {...bound,cold_memory_bytes:null,native_fit_authorized:true};
  assert.equal((await startup.start(getConfig(),options({
    readProfile:async()=>firstLoad,run:observe,
    admit:async bytes=>{assert.equal(bytes,null);return {admitted:true};}
  }))).status,'starting');
  assert.equal((await startup.start(getConfig({...user, 'strata.startOnStartup':false}), options({
    automatic:false, run:observe
  }))).status, 'starting');
  calls = [];
  const denied = await startup.start(getConfig(), options({
    run:observe, admit:async () => ({admitted:false, decision:'BRIDGE', reason:'Memory pressure'})
  }));
  assert.equal(denied.status, 'paused');
  assert.equal(denied.decision, 'BRIDGE');
  assert.deepEqual(calls.map(call => [call.action,...call.args]), [['status'],['start','--plan']]);

  for (const altered of [
    {...plan, plan:{...plan.plan, source_sha256:'invalid'}},
    {...plan, plan:{...plan.plan, launch:{...plan.plan.launch, model_name:'other'}}},
    {...plan, plan:{...plan.plan, launch:{...plan.plan.launch, effective:{host:'0.0.0.0', port:profile.port}}}},
    {...plan, plan:{...plan.plan, launch:{...plan.plan.launch, effective:{host:'127.0.0.1', port:19484}}}}
  ]) {
    const outcome = await startup.start(getConfig(), options({
      run:async (_record, action) => action === 'status' ? {ok:true, profile:profile.profile, state:'stopped'} : altered,
      admit:fail
    }));
    assert.equal(outcome.status, 'mismatch');
  }

  let planReads = 0;
  const changedPlan = await startup.start(getConfig(), options({
    run:async (_record, action, args) => {
      if (action === 'status') return {ok:true, profile:profile.profile, state:'stopped'};
      assert(args.includes('--plan'), 'A changed source must not launch');
      const response = structuredClone(plan);
      if (++planReads === 2) response.plan.source_sha256 = 'f'.repeat(64);
      return response;
    }
  }));
  assert.equal(changedPlan.status, 'changed');

  const revoked = {...user};
  const revokedStart = await startup.start(getConfig(revoked), options({
    run:async (_record, action, args) => {
      if (action === 'status') return {ok:true, profile:profile.profile, state:'stopped'};
      assert(args.includes('--plan'), 'Revoked settings must not launch');
      return structuredClone(plan);
    },
    admit:async () => {
      revoked['strata.startOnStartup'] = false;
      return {admitted:true, decision:'ALLOW'};
    }
  }));
  assert.equal(revokedStart.status, 'changed');

  for (const recoveredState of ['starting','ready']) {
    let starts = 0, statusCalls = 0;
    const outcome = await startup.start(getConfig(), options({
      run:async (_record, action, args) => {
        if (action === 'status') {
          return {ok:true, profile:profile.profile, owner_id:owner, state:statusCalls++ ? recoveredState : 'stopped'};
        }
        if (args.includes('--plan')) return structuredClone(plan);
        starts++;
        throw new Error('CLI disconnected after creating its owner');
      }
    }));
    assert.equal(starts, 1);
    assert.equal(outcome.status, recoveredState === 'ready' ? 'unavailable' : 'starting');
  }

  const commands = [];
  const subject = {exports:{}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../strata_startup.js'), 'utf8'), {
    module:subject, process, Buffer,
    require:name => name === 'node:child_process' ? {
      execFile(executable, argv, opts, callback) {
        commands.push({executable, argv:Array.from(argv), shell:opts.shell, environment:opts.env});
        const result = argv[1] === 'status' ? {ok:true, profile:profile.profile, state:'stopped'} :
          argv.includes('--plan') ? plan : {ok:true, profile:profile.profile, state:'starting'};
        callback(null, JSON.stringify(result));
      }
    } : require(name)
  });
  const results = await Promise.all([
    subject.exports.start(getConfig(), options({run:undefined})),
    subject.exports.start(getConfig(), options({run:undefined}))
  ]);
  assert(results.every(result => result.status === 'starting'));
  assert.equal(commands.filter(call => call.argv.includes('--wait')).length, 2);
  assert(commands.every(call => call.executable === profile.executable && call.shell === undefined));
  assert(commands.every(call => call.argv.includes(profile.profile) && !call.argv.includes('restart')));
  assert(commands.every(call => call.environment.STRATA_RUNTIME_DIR === '/private state'));
  assert(commands.every(call => !call.argv.includes('launchctl')));

  assert.equal((await startup.stop(getConfig(), options({busy:true, readProfile:fail}))).status, 'busy');
  for (const observation of [
    {...serverStatus, model:'other'}, {...serverStatus, instance_id:'e'.repeat(32)},
    {...serverStatus, activity:{in_flight:1}}, {...serverStatus, activity:{}}
  ]) {
    const outcome = await startup.stop(getConfig(), options({
      run:async (_record, action, args) => {
        assert.equal(action, 'stop');
        assert.deepEqual(args, ['--plan']);
        return structuredClone(stopPlan);
      },
      inspectServer:async () => observation
    }));
    assert(['busy','mismatch'].includes(outcome.status));
  }
  let stopCalls = [];
  const stopped = await startup.stop(getConfig(), options({
    inspectServer:async () => structuredClone(serverStatus),
    run:async (_record, action, args) => {
      stopCalls.push({action,args});
      return args.includes('--plan') ? structuredClone(stopPlan) : {ok:true, profile:profile.profile, state:'stopped'};
    }
  }));
  assert.equal(stopped.status, 'stopped');
  assert.deepEqual(stopCalls.at(-1).args,
    ['--wait','30','--if-owner-id',owner,'--if-instance-id',instance]);
  console.log('Native Strata lifecycle checks passed for trusted profiles, resource admission, pending recovery, changed plans and exact idle instance stops.');
}

verify().catch(error => {console.error(error); process.exitCode = 1;});

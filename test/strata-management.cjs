'use strict';
const assert = require('node:assert/strict');
const {create, nativeJSON, planBinding, contextTokens} = require('../strata_management');

const owner = name => (name === 'alpha' ? 'a' : 'b').repeat(32);
const instance = name => (name === 'alpha' ? 'c' : 'd').repeat(32);
const record = () => ({executable:'/native/bin/strata', environment:{STRATA_INSTALL_DIR:'/native/runtime',
  STRATA_NO_ACQUIRE:'1'}, profile:'alpha', model:'first', port:19483, cold_memory_bytes:20*1024**3});

function fixture(options = {}) {
  let saved = record(), time = 0, writes = 0;
  const calls = [], admissions = [];
  const configs = {alpha:{model:'first', port:19483, revision:'1'.repeat(16), hash:'1'.repeat(64)},
    beta:{model:'second', port:options.otherPort || 19483, revision:'2'.repeat(16), hash:'2'.repeat(64)}};
  const states = {alpha:options.running ? 'ready' : 'stopped', beta:'stopped'};
  const plan = name => ({profile:name, source_sha256:configs[name].hash, launch:{profile:name,
    model_name:configs[name].model, profile_revision:configs[name].revision,
    effective:{host:'127.0.0.1', port:configs[name].port}, engine_argv:['--max-context','8192']}});
  const state = name => ({ok:true, profile:name, state:states[name], owner_id:owner(name), instance_id:instance(name),
    api_identity_verified:true, generation_ready:true});
  const command = async (profile, args) => {
    calls.push({profile:profile.profile, args});
    if (options.command) {
      const overridden = await options.command(profile, args, {configs, states, plan, state});
      if (overridden !== undefined) return overridden;
    }
    if (args[0] === 'models') return {ok:true, models:Object.entries(configs).map(([name,c]) => ({
      name:c.model, source_config:'/native/'+name+'.json', source_sha256:c.hash, context_tokens:8192, component_status:'present'}))};
    if (args[0] === 'profile' && args[1] === 'list') return {ok:true, profiles:Object.keys(configs).map(id => ({id}))};
    if (args[0] === 'profile' && args[1] === 'show') return {ok:true,
      profile:{id:args[2], source_config:'/native/'+args[2]+'.json'}, plan:{model_name:configs[args[2]].model,
        effective:{host:'127.0.0.1', port:configs[args[2]].port}}};
    if (args[0] === 'profile' && args[1] === 'create') {
      configs[args[2]] = {...configs.beta}; states[args[2]] = 'stopped'; return {ok:true};
    }
    const name = profile.profile;
    if (args[1] === 'status') return state(name);
    if (args[1] === 'start' && args.includes('--plan')) return {ok:true, plan:plan(name)};
    if (args[1] === 'stop' && args.includes('--plan')) return {ok:true,
      plan:{profile:name, owner_id:owner(name), instance_id:instance(name)}};
    if (args[1] === 'start') {
      states[name] = options.failBeta && name === 'beta' ? (options.failBeta === 'stopped' ? 'stopped' : 'failed') : options.slow ? 'starting' : 'ready';
      return state(name);
    }
    if (args[1] === 'stop') {
      assert.equal(args[args.indexOf('--if-owner-id')+1], owner(name));
      assert.equal(args[args.indexOf('--if-instance-id')+1], instance(name));
      states[name] = 'stopped'; return state(name);
    }
    assert.fail('Unexpected native command '+args.join(' '));
  };
  const manager = create({platform:'darwin', readProfile:async () => structuredClone(saved),
    writeProfile:async (next, expected) => { assert.deepEqual(saved, expected); saved = structuredClone(next); writes++; },
    command, inspectServer:async port => {
      if (options.inspectServer) return options.inspectServer(port, {configs, states});
      if (options.foreign) return {service:'foreign'};
      const name = Object.keys(configs).find(key => configs[key].port === port &&
        (states[key] === 'ready' || (options.startingServer && states[key] === 'starting')));
      return name ? {service:'strata', model:configs[name].model, instance_id:instance(name),
        activity:{in_flight:options.inflight || 0}} : null;
    },
    busy:options.busy || (() => false), now:() => time, wait:async ms => {time += ms;}, startupSeconds:1,
    admit:async bytes => {admissions.push(bytes); return options.admit ? options.admit(bytes, {states, admissions}) : {admitted:true};}
  });
  return {manager, calls, admissions, configs, states, plan, saved:() => saved, writes:() => writes,
    mutateRecord:next => {saved = next;}, sideEffects:() => calls.filter(call => call.args[0] === 'runtime' &&
      ['start','stop'].includes(call.args[1]) && !call.args.includes('--plan'))};
}

async function verify() {
  let f = fixture({busy:() => true});
  assert.equal((await f.manager.load('forged')).status, 'busy');
  assert.equal(f.calls.length, 0);
  f = fixture();
  assert.equal((await f.manager.load('forged')).status, 'invalid');
  assert.equal(f.calls.length, 0);
  let catalog = await f.manager.discover();
  assert.equal(catalog.available, true);
  assert.equal(catalog.profiles.length, 2);
  assert.equal(catalog.selected_profile, 'alpha');
  assert.equal(catalog.profiles[0].context_tokens, 8192);
  assert.ok(catalog.profiles.every(row => /^[a-f0-9]{32}$/.test(row.id) && !JSON.stringify(row).includes('/native')));
  let alpha = catalog.profiles.find(row => row.name === 'alpha');
  let beta = catalog.profiles.find(row => row.name === 'beta');
  f.configs.beta.hash = 'f'.repeat(64);
  assert.equal((await f.manager.load(beta.id)).status, 'changed');
  assert.equal(f.sideEffects().length, 0);

  f = fixture({admit:async (_bytes, {states}) => {
    // Configuration changes after the resource check cannot stop the old model.
    if (states.alpha === 'ready') f.configs.beta.revision = 'e'.repeat(16);
    return {admitted:true};
  }, running:true});
  catalog = await f.manager.discover();
  assert.equal((await f.manager.load(catalog.profiles[1].id)).status, 'changed');
  assert.equal(f.sideEffects().length, 0);

  f = fixture({admit:async () => ({admitted:false, reason:'Pressure warning', decision:'BRIDGE'})});
  catalog = await f.manager.discover();
  assert.equal((await f.manager.load(catalog.profiles[1].id)).status, 'paused');
  assert.equal(f.sideEffects().length, 0);
  assert.equal(f.writes(), 0);

  f = fixture({running:true, admit:async (_bytes,{states}) => states.alpha === 'ready' ?
    {admitted:false,reason:'Previous model occupies memory',decision:'BRIDGE'} : {admitted:true,decision:'ALLOW'}});
  catalog = await f.manager.discover();
  assert.equal((await f.manager.load(catalog.profiles[1].id)).status, 'ready');
  assert.deepEqual(f.sideEffects().map(call=>[call.profile,call.args[1]]), [['alpha','stop'],['beta','start']],
    'A denied cold target can release one verified idle predecessor and use a fresh admitted measurement');
  assert.equal(f.saved().profile, 'beta');
  assert.equal(f.admissions.length, 3, 'Denied admission, fresh post-release admission, and loading hardware check');

  f = fixture({running:true, admit:async () => ({admitted:false,reason:'Pressure persists',decision:'BRIDGE'})});
  catalog = await f.manager.discover();
  const stillPaused = await f.manager.load(catalog.profiles[1].id);
  assert.equal(stillPaused.status, 'paused');
  assert.deepEqual(stillPaused.resource_recovery,{status:'released_previous',profile:'alpha'});
  assert.deepEqual(f.sideEffects().map(call=>[call.profile,call.args[1]]), [['alpha','stop']]);
  assert.equal(f.admissions.length, 2, 'Resource recovery performs exactly one new admission check');
  assert.equal(f.saved().profile, 'alpha'); assert.equal(f.writes(), 0);

  f = fixture({running:true, admit:async () => {
    f.configs.beta.hash = 'e'.repeat(64);
    return {admitted:false,reason:'Pressure',decision:'BRIDGE'};
  }});
  catalog = await f.manager.discover();
  assert.equal((await f.manager.load(catalog.profiles[1].id)).status, 'changed');
  assert.equal(f.sideEffects().length, 0, 'A changed source cannot authorize recovery unloading');
  f = fixture({running:true, otherPort:19484, inflight:1,
    admit:async () => ({admitted:false,reason:'Pressure',decision:'BRIDGE'})});
  catalog = await f.manager.discover();
  assert.equal((await f.manager.load(catalog.profiles[1].id)).status, 'busy');
  assert.equal(f.sideEffects().length, 0, 'Recovery never unloads a busy old model on another port');

  f = fixture({running:true, admit:async () => ({admitted:false,reason:'Pressure',decision:'BRIDGE'}),
    command:async (profile,args,{state}) => {
      if (profile.profile === 'alpha' && args[0] === 'runtime' && args[1] === 'stop' && !args.includes('--plan')) {
        return {...state('alpha'),ok:false};
      }
    }});
  catalog = await f.manager.discover();
  assert.equal((await f.manager.load(catalog.profiles[1].id)).status, 'uncertain');
  assert.deepEqual(f.sideEffects().map(call=>[call.profile,call.args[1]]), [['alpha','stop']]);
  assert.equal(f.admissions.length, 1, 'Unconfirmed shutdown cannot authorize another allocation or start');
  assert.equal(f.writes(), 0);

  f = fixture({running:true, failBeta:'stopped', admit:async (_bytes,{admissions}) =>
    admissions.length === 1 ? {admitted:false,reason:'Pressure',decision:'BRIDGE'} : {admitted:true,decision:'ALLOW'}});
  catalog = await f.manager.discover();
  const recoveredAfterRelease = await f.manager.load(catalog.profiles[1].id);
  assert.equal(recoveredAfterRelease.status, 'failed');
  assert.equal(recoveredAfterRelease.rollback.status, 'ready');
  assert.equal(f.saved().profile, 'alpha'); assert.equal(f.writes(), 0);
  assert.equal(f.states.alpha, 'ready', 'Pressure recovery retains the old plan for confirmed rollback');

  f = fixture({running:true, admit:async () => ({admitted:false,reason:'Pressure',decision:'BRIDGE'}),
    command:async (profile,args,{configs}) => {
      if (f.admissions.length && profile.profile === 'alpha' && args[0] === 'runtime' &&
          args[1] === 'start' && args.includes('--plan')) configs.beta.hash = 'f'.repeat(64);
    }});
  catalog = await f.manager.discover();
  assert.equal((await f.manager.load(catalog.profiles[1].id)).status, 'changed');
  assert.equal(f.sideEffects().length, 0, 'The target plan is checked again immediately before the old owner stop');

  f = fixture({foreign:true});
  catalog = await f.manager.discover();
  assert.equal((await f.manager.load(catalog.profiles[1].id)).status, 'mismatch');
  assert.equal(f.sideEffects().length, 0);

  f = fixture(); catalog = await f.manager.discover();
  const loaded = await f.manager.load(catalog.profiles[0].id);
  assert.equal(loaded.status, 'ready');
  assert.deepEqual(loaded.selected, {profile:'alpha', model:'first', port:19483});
  assert.equal(f.writes(), 1);
  assert.equal(f.admissions[0], record().cold_memory_bytes);
  assert.equal(f.admissions[1], null, 'Cold memory is not charged again after starting');
  assert.deepEqual(f.saved().memory_binding, planBinding(f.saved(), f.plan('alpha')));
  assert.equal(loaded.peak_memory_verified, false);

  f = fixture({running:true}); catalog = await f.manager.discover();
  assert.equal((await f.manager.load(catalog.profiles[0].id)).status, 'ready');
  assert.ok(f.admissions.every(value => value === null), 'Ready resident instance uses hardware admission only');
  assert.equal(f.sideEffects().length, 0);

  f = fixture({running:true, otherPort:19484}); catalog = await f.manager.discover();
  assert.equal((await f.manager.load(catalog.profiles[1].id)).status, 'ready');
  assert.deepEqual(f.sideEffects().map(call => [call.profile, call.args[1]]), [['alpha','stop'], ['beta','start']]);
  assert.equal(f.saved().profile, 'beta');
  assert.equal(f.saved().cold_memory_bytes, null, 'A new profile never inherits the previous footprint');
  assert.ok(f.admissions.every(value => value === null));

  // An interrupted helper may leave the target fully running before committing
  // startup.json. Resume that exact owner without replaying a native start.
  for (const oldState of ['stopped', 'stale', 'failed']) {
    f = fixture(); f.states.alpha = oldState; f.states.beta = 'ready';
    catalog = await f.manager.discover();
    const adopted = await f.manager.load(catalog.profiles.find(row => row.name === 'beta').id);
    assert.equal(adopted.status, 'ready');
    assert.equal(f.saved().profile, 'beta');
    assert.equal(f.sideEffects().length, 0, 'A running target must be adopted without start or stop');
    assert.ok(f.admissions.every(value => value === null), 'A verified resident target uses fresh hardware admission');
  }
  f = fixture({otherPort:19484}); f.states.beta = 'ready';
  catalog = await f.manager.discover();
  assert.equal((await f.manager.load(catalog.profiles[1].id)).status, 'ready');
  assert.equal(f.sideEffects().length, 0, 'A target on another port must not be restarted');

  f = fixture({running:true, otherPort:19484}); f.states.beta = 'ready';
  catalog = await f.manager.discover();
  assert.equal((await f.manager.load(catalog.profiles[1].id)).status, 'ready');
  assert.deepEqual(f.sideEffects().map(call => [call.profile,call.args[1]]), [['alpha','stop']],
    'Only the verified idle old instance is stopped when the target is already ready');

  let targetPolls = 0;
  f = fixture({startingServer:true, command:async (profile,args,{states}) => {
    if (args[0] === 'runtime' && args[1] === 'status' && profile.profile === 'beta' && ++targetPolls >= 4) states.beta = 'ready';
  }});
  f.states.beta = 'starting'; catalog = await f.manager.discover();
  assert.equal((await f.manager.load(catalog.profiles[1].id)).status, 'ready');
  assert.equal(f.sideEffects().length, 0, 'An already-starting target is polled, never started twice');

  const targetServer = (port,{configs,states}) => port === configs.beta.port && states.beta === 'ready' ? {
    service:'strata',model:configs.beta.model,instance_id:instance('beta'),activity:{in_flight:0}
  } : null;
  f = fixture({running:true, inspectServer:targetServer}); f.states.beta = 'ready';
  catalog = await f.manager.discover();
  assert.equal((await f.manager.load(catalog.profiles[1].id)).status, 'mismatch',
    'A target at the old port cannot hide an old profile whose status remains active');
  assert.equal(f.sideEffects().length, 0); assert.equal(f.writes(), 0);

  f = fixture({inflight:1}); f.states.beta = 'ready'; catalog = await f.manager.discover();
  assert.equal((await f.manager.load(catalog.profiles[1].id)).status, 'busy');
  assert.equal(f.sideEffects().length, 0); assert.equal(f.writes(), 0);
  f = fixture({inspectServer:(port,context) => {
    const server = targetServer(port,context);
    return server ? {...server,instance_id:'e'.repeat(32)} : null;
  }});
  f.states.beta = 'ready'; catalog = await f.manager.discover();
  assert.equal((await f.manager.load(catalog.profiles[1].id)).status, 'mismatch');
  assert.equal(f.sideEffects().length, 0); assert.equal(f.writes(), 0);

  f = fixture({running:true, inflight:1}); catalog = await f.manager.discover();
  assert.equal((await f.manager.load(catalog.profiles[1].id)).status, 'busy');
  assert.equal(f.sideEffects().length, 0);
  assert.equal((await f.manager.unload()).status, 'busy');

  f = fixture({running:true, failBeta:true}); catalog = await f.manager.discover();
  const failed = await f.manager.load(catalog.profiles[1].id);
  assert.equal(failed.status, 'failed');
  assert.equal(failed.rollback.status, 'uncertain');
  assert.equal(f.saved().profile, 'alpha');
  assert.equal(f.writes(), 0);
  assert.equal(f.states.alpha, 'stopped', 'An unverified candidate shutdown must not trigger a second start');

  f = fixture({running:true, failBeta:'stopped'}); catalog = await f.manager.discover();
  const recovered = await f.manager.load(catalog.profiles[1].id);
  assert.equal(recovered.status, 'failed');
  assert.equal(recovered.rollback.status, 'ready');
  assert.equal(f.saved().profile, 'alpha');
  assert.equal(f.writes(), 0);
  assert.equal(f.states.alpha, 'ready');

  f = fixture({slow:true}); catalog = await f.manager.discover();
  assert.equal((await f.manager.load(catalog.profiles[1].id)).status, 'timeout');
  assert.equal(f.writes(), 0);
  assert.equal(f.sideEffects().filter(call => call.args[1] === 'start').length, 1, 'Ambiguous loads are not replayed');

  f = fixture({running:true});
  assert.equal((await f.manager.unload()).status, 'stopped');
  assert.equal(f.sideEffects().length, 1);
  assert.equal((await f.manager.unload()).status, 'stopped');
  assert.equal(f.sideEffects().length, 1, 'Already stopped unload is idempotent');

  f = fixture({command:async (_profile,args) => args[0] === 'models' ? {ok:true, models:'corrupt'} : undefined});
  assert.equal((await f.manager.discover()).available, false);
  f = fixture({command:async () => 'not json'});
  assert.equal((await f.manager.discover()).available, false);

  f = fixture(); catalog = await f.manager.discover();
  assert.equal((await f.manager.importInstalled(catalog.models[1].id)).status, 'imported');
  assert.equal(f.writes(), 0, 'Import does not select or start a model');
  assert.equal(f.sideEffects().length, 0);
  assert.equal((await f.manager.importInstalled('../path')).status, 'invalid');

  f = fixture(); catalog = await f.manager.discover();
  const changed = {...record(), memory_binding:{profile_revision:'0'.repeat(16), source_sha256:'0'.repeat(64), environment_sha256:'0'.repeat(64)}};
  f.mutateRecord(changed);
  assert.equal((await f.manager.load(catalog.profiles[0].id)).status, 'ready');
  assert.equal(f.admissions[0], null, 'A source/profile/environment binding mismatch invalidates the old estimate');

  await assert.rejects(nativeJSON({executable:'relative', environment:{}}, ['profile','list']), /Invalid native/);
  assert.equal(contextTokens({launch:{engine_argv:['--max-context=16384']}}), 16384);
  assert.equal(contextTokens({launch:{engine_argv:['--max-context','-1']}}), null);
  console.log('Strata management: catalog, ownership, busy, resource, readiness, binding, rollback and import checks passed.');
}
verify().catch(error => { console.error(error); process.exitCode = 1; });

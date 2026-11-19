'use strict';
// Trusted extension-host operations only. No executable, profile path or native
// argument is accepted from a model or a webview action.
const {execFile} = require('node:child_process');
const {createHash, randomBytes} = require('node:crypto');
const {constants, promises:fs} = require('node:fs');
const {homedir} = require('node:os');
const {dirname, isAbsolute, join} = require('node:path');
const startup = require('./strata_startup');

const recordPath = () => join(homedir(), startup.startupPath);
const digest = value => createHash('sha256').update(JSON.stringify(value)).digest('hex');
const clean = (value, max = 200) => typeof value === 'string' && value.length > 0 &&
  value.length <= max && !/[\x00-\x1f\x7f]/.test(value);
const profileName = value => clean(value, 64) && /^[a-z][a-z0-9_-]{0,63}$/.test(value);
const nativeID = value => typeof value === 'string' && /^[0-9a-f]{32}$/.test(value);
const baseValid = profile => profile && startup.validProfile({...profile,
  port:19483, model:'discovery', profile:'sutra-discovery', cold_memory_bytes:1},
{port:19483, model:'discovery'});
const selectedValid = profile => baseValid(profile) && profileName(profile.profile) &&
  clean(profile.model) && Number.isInteger(profile.port) && profile.port > 0 && profile.port <= 65535;
const environmentDigest = profile => digest({executable:profile.executable,
  environment:Object.entries(profile.environment).sort(([a],[b]) => a.localeCompare(b))});
const planBinding = (profile, plan) => ({profile_revision:plan.launch.profile_revision,
  source_sha256:plan.source_sha256, environment_sha256:environmentDigest(profile)});
const same = (left, right) => JSON.stringify(left) === JSON.stringify(right);
const planIdentity = plan => digest({profile:plan.profile, source_sha256:plan.source_sha256, launch:plan.launch});
const failure = (status, message, extra = {}) => ({status, message, ...extra});

function contextTokens(plan) {
  const args = plan?.launch?.engine_argv || plan?.engine_argv;
  if (!Array.isArray(args)) return null;
  const flags = ['--max-context', '--context-length', '--ctx-size', '--context'];
  for (let index = 0; index < args.length; index++) {
    if (typeof args[index] !== 'string') continue;
    const [flag, inline] = args[index].split('=', 2);
    if (!flags.includes(flag)) continue;
    const value = Number(inline ?? args[index+1]);
    if (Number.isSafeInteger(value) && value > 0 && value <= 10000000) return value;
  }
  return null;
}

function nativeJSON(profile, argv) {
  if (!baseValid(profile) || !Array.isArray(argv) || argv.some(arg => !clean(arg, 4096))) {
    return Promise.reject(new Error('Invalid native Strata command'));
  }
  return new Promise((resolve, reject) => {
    execFile(profile.executable, argv, {env:{...process.env, ...profile.environment},
      timeout:argv.includes('stop') ? 40000 : 15000, maxBuffer:524288, windowsHide:true}, (error, stdout) => {
      try {
        const result = JSON.parse(stdout);
        if (!result || typeof result.ok !== 'boolean') throw new Error('Invalid native Strata JSON');
        resolve(result);
      } catch {
        reject(new Error(error ? 'Native Strata command failed without a valid JSON response.' :
          'Native Strata returned invalid JSON.'));
      }
    });
  });
}

async function atomicWriteProfile(profile, expected) {
  const target = recordPath();
  const serialized = JSON.stringify(profile, null, 2)+'\n';
  if (Buffer.byteLength(serialized) > 8192) throw new Error('The native Strata selection record is too large.');
  if (expected && !same(await startup.readStartupProfile(), expected)) {
    throw new Error('Strata selection changed before it could be saved.');
  }
  await fs.mkdir(dirname(target), {recursive:true, mode:0o700});
  const temporary = target+'.'+randomBytes(12).toString('hex')+'.tmp';
  let handle;
  try {
    handle = await fs.open(temporary, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL, 0o600);
    await handle.writeFile(serialized);
    await handle.sync();
    await handle.close();
    handle = null;
    if (expected && !same(await startup.readStartupProfile(), expected)) {
      throw new Error('Strata selection changed before it could be saved.');
    }
    await fs.rename(temporary, target);
  } finally {
    if (handle) await handle.close();
    await fs.unlink(temporary).catch(() => {});
  }
}

function create({readProfile = startup.readStartupProfile, writeProfile = atomicWriteProfile,
  command = nativeJSON, admit = async () => ({admitted:false, reason:'Resource admission is unavailable.'}),
  inspectServer = startup.readServer, busy = () => false, platform = process.platform, remote = false,
  wait = ms => new Promise(resolve => setTimeout(resolve, ms)), now = () => Date.now(),
  startupSeconds = 120} = {}) {
  const catalog = new Map();
  let operating = false;
  const isBusy = () => operating || (typeof busy === 'function' ? busy() : busy);
  const supported = () => platform === 'darwin' && !remote;
  const invoke = async (profile, argv) => {
    const result = await command(profile, [...argv, '--output', 'json']);
    if (!result || typeof result.ok !== 'boolean') throw new Error('Invalid native Strata response.');
    return result;
  };
  const runtime = (profile, action, args = []) => invoke(profile,
    ['runtime', action, '--profile', profile.profile, ...args]);
  const configuration = async () => {
    const record = await readProfile();
    if (!baseValid(record)) throw new Error('Locate the installed native Strata CLI before managing models.');
    return record;
  };
  const unchanged = async record => same(await readProfile(), record);
  const entryID = entry => digest({kind:entry.kind, name:entry.name,
    source:entry.source, identity:entry.identity, environment:entry.environment}).slice(0, 32);
  const put = entry => {
    const id = entryID(entry);
    catalog.set(id, entry);
    return id;
  };
  const getPlan = async profile => {
    const result = await runtime(profile, 'start', ['--plan']);
    if (!startup.matchingPlan(result, profile)) throw new Error('Native Strata plan has an invalid profile, model or loopback endpoint.');
    return result.plan;
  };
  const inspect = async port => {
    try { return await inspectServer(port); }
    catch (error) {
      if (error?.code === 'ECONNREFUSED') return null;
      throw new Error('The loopback port is occupied or its server identity could not be verified.');
    }
  };
  const serverMatches = (server, profile, state) => server?.service === 'strata' &&
    server.model === profile.model && server.instance_id === state.instance_id &&
    nativeID(state.owner_id) && nativeID(state.instance_id);
  const installedArgs = record => ['models', 'list', '--installed',
    ...(record.environment.STRATA_INSTALL_DIR ? ['--install-dir', record.environment.STRATA_INSTALL_DIR] : [])];
  const admission = async (profile, plan, legacy = false, hardwareOnly = false) => {
    const binding = planBinding(profile, plan);
    const bound = same(profile.memory_binding, binding) || legacy;
    const bytes = bound && Number.isSafeInteger(profile.cold_memory_bytes) && profile.cold_memory_bytes > 0 ?
      profile.cold_memory_bytes : null;
    const check = await admit(hardwareOnly ? null : bytes);
    return {check, bytes, binding};
  };

  async function discover() {
    if (!supported()) return {available:false, supported:false, busy:isBusy(), profiles:[], models:[],
      message:'Native Strata model management requires a local macOS window.'};
    try {
      const record = await configuration();
      const [listed, installed] = await Promise.all([
        invoke(record, ['profile', 'list']), invoke(record, installedArgs(record))
      ]);
      if (!listed.ok || !Array.isArray(listed.profiles) || listed.profiles.length > 128 ||
          !installed.ok || !Array.isArray(installed.models) || installed.models.length > 128) {
        throw new Error('Native Strata returned an invalid model catalog.');
      }
      catalog.clear();
      const models = [];
      const sources = new Map();
      for (const model of installed.models) {
        if (!clean(model.name) || !clean(model.source_config, 4096) || !isAbsolute(model.source_config) ||
            !/^[0-9a-f]{64}$/.test(model.source_sha256)) continue;
        const entry = {kind:'config', name:model.name, model:model.name,
          source:model.source_config, source_sha256:model.source_sha256,
          identity:model.source_sha256, environment:environmentDigest(record)};
        const id = put(entry);
        const context = Number.isSafeInteger(model.context_tokens) && model.context_tokens > 0 ? model.context_tokens : null;
        models.push({id, name:model.name, model:model.name, context_tokens:context,
          state:model.component_status === 'present' ? 'installed' : 'components_missing'});
        sources.set(model.source_config, {model, context});
      }
      const profiles = [];
      for (const row of listed.profiles) {
        if (!profileName(row.id)) continue;
        const shown = await invoke(record, ['profile', 'show', row.id, '--effective']);
        const effective = shown.plan?.effective;
        if (!shown.ok || shown.profile?.id !== row.id || !clean(shown.plan?.model_name) ||
            effective?.host !== '127.0.0.1' || !Number.isInteger(effective.port) || effective.port < 1 || effective.port > 65535) continue;
        const profile = {...record, profile:row.id, model:shown.plan.model_name, port:effective.port};
        const plan = await getPlan(profile);
        const state = await runtime(profile, 'status');
        if (state.profile !== row.id || !clean(state.state, 40)) throw new Error('Invalid native Strata profile status.');
        let ready=state.ok===true&&state.state==='ready'&&state.api_identity_verified===true&&
          state.generation_ready===true&&nativeID(state.owner_id)&&nativeID(state.instance_id);
        if(ready){
          try{const server=await inspectServer(profile.port);ready=serverMatches(server,profile,state)&&server.loaded===true;}
          catch{ready=false;}
        }
        const reportedState=state.state==='ready'&&!ready?'unverified':state.state;
        const source = shown.profile.source_config;
        const selected = selectedValid(record) && record.profile === profile.profile && record.model === profile.model && record.port === profile.port;
        const entry = {kind:'profile', name:row.id, profile, source,
          identity:planIdentity(plan), plan, environment:environmentDigest(record),
          legacy:selected && !record.memory_binding && Number.isSafeInteger(record.cold_memory_bytes) && record.cold_memory_bytes > 0,
          record:structuredClone(record)};
        const id = put(entry);
        profiles.push({id, name:row.id, model:profile.model,
          context_tokens:contextTokens(plan) || contextTokens(shown.plan) || sources.get(source)?.context || null,
          state:reportedState, ready, selected});
      }
      const selected = profiles.find(profile => profile.selected);
      return {available:true, supported:true, busy:isBusy(), profiles, models,
        selected_profile:selected?.name || null, runtime_state:selected?.state || 'unselected',
        message:profiles.length ? 'Installed native Strata profiles. Loading verifies the native model and available resources.' :
          'No local native Strata profile is configured. Import a native Strata configuration.'};
    } catch (error) {
      return {available:false, supported:true, busy:isBusy(), profiles:[], models:[], message:error.message};
    }
  }

  async function exclusive(operation) {
    if (!supported()) return failure('unsupported', 'Native Strata management requires a local macOS window.');
    if (isBusy()) return failure('busy', 'Wait for the active Sutra request or model operation.');
    operating = true;
    try { return await operation(); }
    catch (error) { return failure('failed', error.message || 'Native Strata management failed.'); }
    finally { operating = false; }
  }

  async function importConfig(trustedPath) {
    return exclusive(async () => {
      // The caller must obtain this path from a trusted native file picker, never
      // from model text or a webview payload.
      if (!clean(trustedPath, 4096) || !isAbsolute(trustedPath)) return failure('invalid', 'Select an absolute native Strata configuration path.');
      const record = await configuration();
      const name = 'sutra-'+randomBytes(8).toString('hex');
      const made = await invoke(record, ['profile', 'create', name, '--from-config', trustedPath]);
      if (!made.ok) return failure('invalid', 'Native Strata did not accept that configuration.');
      const shown = await invoke(record, ['profile', 'show', name, '--effective']);
      const effective = shown.plan?.effective;
      if (!shown.ok || shown.profile?.id !== name || !clean(shown.plan?.model_name) || effective?.host !== '127.0.0.1' ||
          !Number.isInteger(effective.port) || effective.port < 1 || effective.port > 65535) {
        return failure('invalid', 'The imported configuration must name a model on a valid loopback endpoint. It was not selected or loaded.');
      }
      const profile = {...record, profile:name, model:shown.plan.model_name, port:effective.port, cold_memory_bytes:null};
      delete profile.memory_binding;
      const plan = await getPlan(profile);
      const id = put({kind:'profile', name, profile, source:trustedPath, identity:planIdentity(plan),
        plan, environment:environmentDigest(record), legacy:false, record:structuredClone(record)});
      return failure('imported', 'Native Strata saved the configuration as a profile. Select Load to start and verify it.', {id, profile:name});
    });
  }

  async function stopExact(profile, expectedOwner = null) {
    const state = await runtime(profile, 'status');
    if (state.ok && state.profile === profile.profile && state.state === 'stopped') return failure('stopped', 'The native Strata profile is already stopped.');
    const preview = await runtime(profile, 'stop', ['--plan']), plan = preview.plan;
    if (!preview.ok || plan?.profile !== profile.profile || !nativeID(plan.owner_id) || !nativeID(plan.instance_id) ||
        (expectedOwner && plan.owner_id !== expectedOwner)) return failure('unavailable', 'The native owner could not be verified; no stop was sent.');
    const server = await inspect(profile.port);
    if (!serverMatches(server, profile, plan)) return failure('mismatch', 'The loopback server is not the reviewed native model instance; no stop was sent.');
    if (!Number.isInteger(server.activity?.in_flight) || server.activity.in_flight !== 0) return failure('busy', 'The native Strata model has active requests; no stop was sent.');
    let result;
    try { result = await runtime(profile, 'stop', ['--wait', '30', '--if-owner-id', plan.owner_id, '--if-instance-id', plan.instance_id]); }
    catch { /* Reconcile an interrupted native CLI response; never replay stop. */ }
    if (!result?.ok || result.profile !== profile.profile || result.state !== 'stopped') result = await runtime(profile, 'status');
    return result.ok && result.profile === profile.profile && result.state === 'stopped' ?
      failure('stopped', 'Strata stopped the verified idle native instance.') :
      failure('uncertain', 'The stop was sent once, but native Strata has not confirmed it.');
  }

  async function startReady(profile, plan, memory, record) {
    const state = await runtime(profile, 'status');
    if (state.profile !== profile.profile || !['ready', 'starting', 'stopped', 'stale', 'failed'].includes(state.state)) {
      return failure('unavailable', 'Native Strata runtime state could not be verified.');
    }
    let owner = nativeID(state.owner_id) ? state.owner_id : null;
    if (!['ready', 'starting'].includes(state.state)) {
      if (await inspect(profile.port)) return failure('mismatch', 'The target loopback port became occupied before native startup.');
      const latest = await getPlan(profile);
      if (planIdentity(latest) !== planIdentity(plan) || !await unchanged(record)) return failure('changed', 'The Strata profile or selection changed before loading.');
      try {
        const started = await runtime(profile, 'start', ['--wait', '0']);
        if (nativeID(started.owner_id)) owner = started.owner_id;
      } catch { /* Start may have happened. Poll its native owner; do not retry. */ }
    }
    const deadline = now()+Math.min(120, Math.max(1, startupSeconds))*1000;
    let nextAdmission = 0;
    do {
      if (now() >= nextAdmission) {
        // Once loading has begun the model's allocation is reflected in system
        // pressure. Recharging its cold footprint here double-counts memory.
        const check = await admit(null);
        if (check?.admitted !== true) {
          let recovery;
          if (owner) recovery = await stopExact(profile, owner).catch(() => failure('uncertain', 'Candidate stop could not be verified.'));
          return failure('paused', 'Native model loading paused: '+(check?.reason || 'Resource admission was denied.'), {recovery, owner});
        }
        nextAdmission = now()+2000;
      }
      const observed = await runtime(profile, 'status');
      if (observed.profile !== profile.profile) return failure('mismatch', 'Native Strata returned a different profile during loading.', {owner});
      if (nativeID(observed.owner_id)) {
        if (owner && observed.owner_id !== owner) return failure('changed', 'The native Strata owner changed during loading.', {owner});
        owner = observed.owner_id;
      }
      if (observed.ok && observed.state === 'ready' && observed.api_identity_verified === true && observed.generation_ready === true) {
        const server = await inspect(profile.port);
        if (!serverMatches(server, profile, observed)) return failure('mismatch', 'Native readiness did not match the loopback server identity.', {owner});
        if (planIdentity(await getPlan(profile)) !== planIdentity(plan)) return failure('changed', 'The native profile changed while loading.', {owner});
        return failure('ready', 'Native Strata verified model identity and a generation probe.', {owner, instance:observed.instance_id});
      }
      if (['failed', 'stopped'].includes(observed.state)) return failure('failed', 'The native Strata model did not remain running.', {owner});
      await wait(500);
    } while (now() < deadline);
    return failure('timeout', 'Native Strata has not confirmed generation readiness within this bounded load operation.', {owner});
  }

  async function load(id) {
    return exclusive(async () => {
      const entry = typeof id === 'string' && catalog.get(id);
      if (!entry || entry.kind !== 'profile') return failure('invalid', 'Refresh installed Strata profiles and select a valid profile.');
      const record = await configuration();
      if (entry.environment !== environmentDigest(record)) return failure('changed', 'The native Strata environment changed. Refresh the model list.');
      const profile = {...record, profile:entry.profile.profile, model:entry.profile.model, port:entry.profile.port};
      const plan = await getPlan(profile);
      if (planIdentity(plan) !== entry.identity) return failure('changed', 'The native profile changed since discovery. Refresh before loading.');
      const exactPrevious = selectedValid(record) && record.profile === profile.profile && record.model === profile.model && record.port === profile.port;
      const legacy = exactPrevious && entry.legacy && same(entry.record, record);
      if (!exactPrevious) { profile.cold_memory_bytes = null; delete profile.memory_binding; }
      const server = await inspect(profile.port);
      let existing, targetOwnsServer = false;
      if (server) {
        existing = await runtime(profile, 'status');
        targetOwnsServer = existing.ok === true && existing.profile === profile.profile &&
          ['ready', 'starting'].includes(existing.state) && serverMatches(server, profile, existing);
        if (!targetOwnsServer) {
          // A different selected profile may legitimately own this same port.
          const previous = selectedValid(record) && record.port === profile.port ? await runtime(record, 'status') : null;
          if (!previous || !serverMatches(server, record, previous)) return failure('mismatch', 'That loopback port has an unowned server. No process was stopped.');
        }
        if (!Number.isInteger(server.activity?.in_flight) || server.activity.in_flight !== 0) return failure('busy', 'The native model has active requests or unavailable activity data.');
      }
      const alreadyReady = targetOwnsServer && existing.state === 'ready';
      let stoppedPrevious = false;
      let previousPlan;
      let resourceRecovery;
      let memory = await admission(profile, plan, legacy, alreadyReady);
      if (memory.check?.admitted !== true && !exactPrevious && !targetOwnsServer && selectedValid(record)) {
        // One bounded recovery may release an owned idle predecessor. The
        // denied allocation never authorizes inference, and a fresh check must
        // allow the target after the old runtime has actually stopped.
        const latest = await getPlan(profile);
        if (planIdentity(latest) !== entry.identity || !await unchanged(record)) {
          return failure('changed', 'The native profile or saved selection changed before resource recovery.');
        }
        const oldServer = await inspect(record.port);
        const current = await runtime(record, 'status');
        if (oldServer) {
          if (current.profile !== record.profile || !serverMatches(oldServer, record, current)) {
            return failure('mismatch', 'Resource recovery could not verify the previous native owner. No process was stopped.');
          }
          if (!Number.isInteger(oldServer.activity?.in_flight) || oldServer.activity.in_flight !== 0) {
            return failure('busy', 'The previous native model has active requests or unavailable activity data. No process was stopped.');
          }
          previousPlan = await getPlan(record);
          const beforeStop = await getPlan(profile);
          if (planIdentity(beforeStop) !== entry.identity || !await unchanged(record)) {
            return failure('changed', 'The native profile or saved selection changed before releasing the previous model.');
          }
          const stopped = await stopExact(record, current.owner_id);
          if (stopped.status !== 'stopped') return stopped;
          stoppedPrevious = true;
          resourceRecovery = {status:'released_previous',profile:record.profile};
          memory = await admission(profile, plan, legacy, false);
        }
      }
      if (memory.check?.admitted !== true) return failure('paused',
        'Model loading paused. '+(memory.check?.reason || 'Resource admission was denied.')+
        (stoppedPrevious ? ' The previous idle native model was released; its saved selection and task spending were retained.' : ''),
        {decision:memory.check?.decision,...(resourceRecovery ? {resource_recovery:resourceRecovery} : {})});
      const latest = await getPlan(profile);
      if (planIdentity(latest) !== entry.identity || !await unchanged(record)) return failure('changed', 'The native profile or saved selection changed before loading.');
      if (!stoppedPrevious && !exactPrevious && selectedValid(record)) {
        const current = await runtime(record, 'status');
        if (targetOwnsServer && record.port === profile.port) {
          // A timed-out coordinator may leave the authorized target running
          // before it saves the new selection. Its exact native owner can be
          // resumed, but the old profile must already be inactive at this port.
          if (current.profile !== record.profile || !['stopped', 'stale', 'failed'].includes(current.state)) {
            return failure('mismatch', 'The target owns this port, but the previous native profile is not confirmed inactive. No process was stopped.');
          }
        } else {
          const oldServer = record.port === profile.port ? server : await inspect(record.port);
          if (oldServer) {
            if (!serverMatches(oldServer, record, current)) return failure('mismatch', 'The current server does not match the saved native Strata owner. No process was stopped.');
            previousPlan = await getPlan(record);
            const stopped = await stopExact(record, current.owner_id);
            if (stopped.status !== 'stopped') return stopped;
            stoppedPrevious = true;
          } else if (current.profile !== record.profile || !['stopped', 'stale', 'failed'].includes(current.state)) {
            return failure('unavailable', 'The previous native model is not stopped, and its server identity could not be verified.');
          }
        }
      }
      let loaded;
      try { loaded = await startReady(profile, plan, memory, record); }
      catch (error) { loaded = failure('failed', error.message); }
      if (loaded.status === 'ready') {
        if (!await unchanged(record)) return failure('changed', 'Native Strata is ready, but the saved selection changed. No selection was overwritten.');
        const committed = {...profile, cold_memory_bytes:memory.bytes, memory_binding:memory.binding,
          native_fit_authorized:memory.bytes === null};
        try { await writeProfile(committed, record); }
        catch (error) { return failure('save_failed', 'Native Strata is ready, but its selection could not be saved: '+error.message); }
        return {...loaded, selected:{model:profile.model, profile:profile.profile, port:profile.port},
          peak_memory_verified:false, cold_memory_bytes:memory.bytes,
          message:memory.bytes === null ? 'Native Strata is ready. Peak memory is unmeasured; each explicit load uses fresh hardware admission.' : loaded.message};
      }
      let rollback = {status:'not_needed', message:'The saved selection was not changed.'};
      if (stoppedPrevious && previousPlan && await unchanged(record)) {
        // Never replay start or touch an unknown process after an ambiguous result.
        let candidateStop;
        if (nativeID(loaded.owner)) {
          candidateStop = await stopExact(profile, loaded.owner).catch(() => failure('uncertain', 'Could not verify the candidate instance.'));
        } else {
          const candidateState = await runtime(profile, 'status');
          candidateStop = candidateState.ok && candidateState.profile === profile.profile && candidateState.state === 'stopped' ?
            failure('stopped', 'The candidate profile is stopped.') : failure('uncertain', 'The candidate start owner was not confirmed.');
        }
        if (candidateStop.status === 'stopped') {
          const previousMemory = await admission(record, previousPlan, !record.memory_binding && entry.record?.profile === record.profile);
          if (previousMemory.check?.admitted === true) rollback = await startReady(record, previousPlan, previousMemory, record);
          else rollback = failure('paused', 'Previous selection remains saved; resources did not admit restarting it.');
        } else rollback = failure('uncertain', 'Previous selection remains saved; candidate shutdown was not confirmed, so it was not restarted.');
      }
      return {...loaded, rollback, message:loaded.message+' The saved model selection was preserved. '+rollback.message};
    });
  }

  async function unload() {
    return exclusive(async () => {
      const record = await configuration();
      if (!selectedValid(record)) return failure('stopped', 'No native Strata profile is selected.');
      return stopExact(record);
    });
  }
  async function importInstalled(id) {
    const entry = typeof id === 'string' && catalog.get(id);
    if (!entry || entry.kind !== 'config') return failure('invalid', 'Refresh installed models and select a valid native configuration.');
    try {
      const record = await configuration();
      if (entry.environment !== environmentDigest(record)) return failure('changed', 'The native environment changed. Refresh installed models.');
      const installed = await invoke(record, installedArgs(record));
      if (!installed.ok || !Array.isArray(installed.models) || !installed.models.some(model =>
        model.name === entry.model && model.source_config === entry.source && model.source_sha256 === entry.source_sha256)) {
        return failure('changed', 'The installed native configuration changed. Refresh before importing it.');
      }
      return importConfig(entry.source);
    } catch (error) { return failure('failed', error.message); }
  }
  return {discover, importConfig, importInstalled, load, unload};
}

module.exports = {create, nativeJSON, atomicWriteProfile, baseValid, planBinding, contextTokens};

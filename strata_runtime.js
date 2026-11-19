'use strict';
// Process boundary for native Strata stages. The host supplies the allowlist;
// model text never supplies executable paths, environment overrides or arguments.
const {execFile} = require('node:child_process');
const path = require('node:path');
const startup = require('./strata_startup');
const management = require('./strata_management');

const MAX_OUTPUT = 524288;
const PROFILE = /^[a-z][a-z0-9_-]{0,63}$/;
const IDENTITY = /^[0-9a-f]{32}$/;
const STATES = new Set(['ready','starting','stopped','stale','failed']);
const clean = value => typeof value === 'string' && value.length > 0 && value.length <= 200 &&
  !/[\x00-\x1f\x7f]/.test(value);
const same = (left,right) => JSON.stringify(left) === JSON.stringify(right);

function nativeProfile(ref) {
  if (typeof ref !== 'string' || !ref.startsWith('strata:profile/')) return null;
  const name = ref.slice('strata:profile/'.length);
  return PROFILE.test(name) ? name : null;
}

function validRef(ref) { return ref === 'strata:high' || nativeProfile(ref) !== null; }

function failure(status, error, extra = {}) { return {ok:false,status,error,...extra}; }

function configuredLegacy(environment) {
  const raw = environment.SUTRA_STRATA_PORT;
  const port = typeof raw === 'string' && /^[0-9]{1,5}$/.test(raw) ? Number(raw) : null;
  const model = environment.SUTRA_STRATA_MODEL;
  return port > 0 && port <= 65535 && clean(model) ? {port,model} : null;
}

function allowedRefs(environment) {
  let values;
  try { values = JSON.parse(environment.SUTRA_STRATA_MODELS || 'null'); } catch {}
  if (!Array.isArray(values) || values.length > 16 || values.some(value =>
    typeof value !== 'string' || value.length > 200 || /[\x00-\x1f\x7f]/.test(value))) {
    throw new Error('Native model activation needs a valid host model allowlist.');
  }
  return new Set(values.filter(validRef));
}

function providerAdmission(bytes, {environment = process.env, execute = execFile} = {}) {
  const executable = environment.SUTRA_PROVIDER_PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
  if (typeof executable !== 'string' || !executable || /[\x00-\x1f\x7f]/.test(executable)) {
    return Promise.resolve({admitted:false,reason:'The trusted provider Python executable is invalid.'});
  }
  return new Promise(resolve => {
    execute(executable, ['-B',path.join(__dirname,'harness','provider_startup.py'),
      ...(bytes === null ? ['--native-fit'] : ['--cold-memory-bytes',String(bytes)])],
    {env:{...environment},timeout:8000,maxBuffer:65536,windowsHide:true}, (error,stdout) => {
      try {
        const value = JSON.parse(stdout);
        if (error || typeof value?.admitted !== 'boolean') throw new Error('Invalid hardware admission');
        resolve(value);
      } catch {
        resolve({admitted:false,reason:'Fresh native provider hardware admission is unavailable.'});
      }
    });
  });
}

function create({readProfile = startup.readStartupProfile, writeProfile = management.atomicWriteProfile,
  command = management.nativeJSON, managerFactory = management.create, inspectServer = startup.readServer,
  admit, environment = process.env, platform = process.platform, remote = false} = {}) {
  const admission = admit || (bytes => providerAdmission(bytes,{environment}));
  const supported = () => platform === 'darwin' && !remote;
  const invoke = async (record,argv) => {
    const value = await command(record,[...argv,'--output','json']);
    if (typeof value?.ok !== 'boolean') throw new Error('Invalid native Strata response.');
    return value;
  };

  async function record() {
    const current = await readProfile();
    if (!management.baseValid(current)) throw new Error('Locate the installed native Strata runtime in Sutra model settings.');
    return current;
  }

  async function profileMetadata(current,name) {
    if (!PROFILE.test(name)) throw new Error('Invalid native profile identity.');
    const shown = await invoke(current,['profile','show',name,'--effective']);
    const effective = shown.plan?.effective;
    if (!shown.ok || shown.profile?.id !== name || !clean(shown.plan?.model_name) ||
        effective?.host !== '127.0.0.1' || !Number.isInteger(effective.port) || effective.port < 1 || effective.port > 65535) {
      throw new Error('Native profile must name a model on a valid loopback endpoint.');
    }
    const selected = {...current,profile:name,model:shown.plan.model_name,port:effective.port};
    const preview = await invoke(selected,['runtime','start','--profile',name,'--plan']);
    if (!startup.matchingPlan(preview,selected)) throw new Error('Native profile plan does not match its model and endpoint.');
    const context = management.contextTokens(preview.plan);
    if (!Number.isSafeInteger(context) || context < 1) throw new Error('Native profile has no verified context allocation.');
    const state = await invoke(selected,['runtime','status','--profile',name]);
    if (state.profile !== name || !STATES.has(state.state)) throw new Error('Native profile status could not be verified.');
    let ready = state.ok === true && state.state === 'ready' && state.api_identity_verified === true &&
      state.generation_ready === true && IDENTITY.test(state.owner_id) && IDENTITY.test(state.instance_id);
    if (ready) {
      try {
        const server = await inspectServer(selected.port);
        ready = server?.service === 'strata' && server.model === selected.model &&
          server.instance_id === state.instance_id && server.loaded === true;
      } catch { ready = false; }
    }
    const ref = 'strata:profile/'+name;
    return {name:ref,ref,display_name:name+' · '+selected.model,profile:name,model:selected.model,
      port:selected.port,context_tokens:context,profile_revision:preview.plan.launch.profile_revision,
      source_sha256:preview.plan.source_sha256,active:['ready','starting'].includes(state.state),ready,
      eligible:true,provider:'strata',execution_compatibility:'unverified'};
  }

  function legacyName(current,profiles) {
    const wanted = configuredLegacy(environment);
    if (!wanted) throw new Error('Configure the legacy Strata port and model before selecting strata:high.');
    const candidates = profiles.filter(row => row.model === wanted.model && row.port === wanted.port);
    const pin = current.legacy_high;
    if (pin && PROFILE.test(pin.profile) && pin.model === wanted.model && pin.port === wanted.port &&
        candidates.some(row => row.profile === pin.profile)) return pin.profile;
    if (current.model === wanted.model && current.port === wanted.port &&
        candidates.some(row => row.profile === current.profile)) return current.profile;
    if (candidates.length === 1) return candidates[0].profile;
    throw new Error(candidates.length ? 'The legacy Strata selection matches several profiles. Select a named native profile.' :
      'The legacy Strata model is not registered as a local native profile.');
  }

  async function scan() {
    const current = await record();
    const listed = await invoke(current,['profile','list']);
    if (!listed.ok || !Array.isArray(listed.profiles) || listed.profiles.length > 128) {
      throw new Error('Native Strata returned an invalid bounded profile list.');
    }
    const profiles = [],excluded = [],seen = new Set();
    for (const row of listed.profiles) {
      if (!PROFILE.test(row?.id) || seen.has(row.id)) throw new Error('Native Strata returned an invalid profile identity.');
      seen.add(row.id);
      try { profiles.push(await profileMetadata(current,row.id)); }
      catch (error) { excluded.push({profile:row.id,reason:error.message}); }
    }
    return {current,profiles,excluded};
  }

  async function catalog() {
    if (!supported()) return failure('unsupported','Native Strata management requires a local macOS host.');
    try {
      const result = await scan();
      let legacy;
      try { legacy = 'strata:profile/'+legacyName(result.current,result.profiles); } catch {}
      return {ok:true,profiles:result.profiles,excluded:result.excluded,...(legacy ? {legacy_high_ref:legacy} : {})};
    } catch (error) { return failure('unavailable',error.message); }
  }

  async function inspect(ref) {
    if (!supported()) return failure('unsupported','Native Strata management requires a local macOS host.');
    if (!validRef(ref)) return failure('invalid','Choose a valid native Strata model reference.');
    try {
      let metadata;
      if (ref === 'strata:high') {
        const result = await scan();
        metadata = result.profiles.find(row => row.profile === legacyName(result.current,result.profiles));
      } else metadata = await profileMetadata(await record(),nativeProfile(ref));
      return {ok:true,profile:{...metadata,name:ref,ref}};
    } catch (error) { return failure('unavailable',error.message); }
  }

  async function activate(ref) {
    if (!supported()) return failure('unsupported','Native Strata management requires a local macOS host.');
    if (!validRef(ref)) return failure('invalid','Choose a valid native Strata model reference.');
    try {
      if (!allowedRefs(environment).has(ref)) return failure('denied','The native model is not assigned to a user-configured Sutra tier.');
      const initial = await scan();
      const name = ref === 'strata:high' ? legacyName(initial.current,initial.profiles) : nativeProfile(ref);
      const metadata = initial.profiles.find(row => row.profile === name);
      if (!metadata) return failure('unavailable','The native model is not present in the verified profile catalog.');
      let pin;
      try {
        const high = legacyName(initial.current,initial.profiles);
        const row = initial.profiles.find(profile => profile.profile === high);
        pin = {profile:high,model:row.model,port:row.port};
      } catch {}
      const manager = managerFactory({readProfile,command,admit:admission,inspectServer,platform,remote,
        writeProfile:async (next,expected) => {
          // Preserve the original legacy High endpoint when a named Low/Mid
          // profile becomes the machine's currently selected native runtime.
          const committed = pin ? {...next,legacy_high:pin} : next;
          await writeProfile(committed,expected);
        }});
      const discovered = await manager.discover();
      const selected = discovered.profiles?.find(row => row.name === name);
      if (!discovered.available || !selected) return failure('unavailable',discovered.message || 'The native profile is unavailable.');
      const loaded = await manager.load(selected.id);
      if (loaded.status !== 'ready') return failure(loaded.status || 'unavailable',loaded.message || 'Native model activation did not complete.',
        {...(loaded.decision ? {decision:loaded.decision} : {}),...(loaded.rollback ? {rollback:loaded.rollback} : {})});
      if (loaded.selected?.profile !== metadata.profile || loaded.selected.model !== metadata.model || loaded.selected.port !== metadata.port) {
        return failure('changed','Native activation returned a different selected model.');
      }
      const fresh = await profileMetadata(await record(),name);
      if (!fresh.ready || !same([fresh.profile_revision,fresh.source_sha256,fresh.model,fresh.port,fresh.context_tokens],
        [metadata.profile_revision,metadata.source_sha256,metadata.model,metadata.port,metadata.context_tokens])) {
        return failure('changed','The native profile changed or lost readiness during activation.');
      }
      const server = await inspectServer(fresh.port);
      const current = await record();
      const ready = await invoke({...current,profile:name,model:fresh.model,port:fresh.port},
        ['runtime','status','--profile',name]);
      if (server?.service !== 'strata' || server.loaded !== true || server.model !== fresh.model ||
          ready.ok !== true || ready.profile !== name || ready.state !== 'ready' ||
          ready.api_identity_verified !== true || ready.generation_ready !== true ||
          !IDENTITY.test(ready.owner_id) || !IDENTITY.test(ready.instance_id) || server.instance_id !== ready.instance_id) {
        return failure('changed','The activated native model no longer matches its server identity.');
      }
      return {ok:true,status:'ready',profile:{...fresh,name:ref,ref},server};
    } catch (error) { return failure('unavailable',error.message); }
  }
  return {catalog,inspect,activate};
}

async function main(argv = process.argv.slice(2), {runtime = create(), output = value => process.stdout.write(value)} = {}) {
  let result;
  if (argv.length === 1 && argv[0] === 'catalog') result = await runtime.catalog();
  else if (argv.length === 2 && ['inspect','activate'].includes(argv[0])) result = await runtime[argv[0]](argv[1]);
  else result = failure('invalid','Use catalog, inspect MODEL_REF, or activate MODEL_REF.');
  let serialized = JSON.stringify(result)+'\n';
  if (Buffer.byteLength(serialized) > MAX_OUTPUT) {
    result = failure('invalid','The bounded native model response exceeded its allocation.');
    serialized = JSON.stringify(result)+'\n';
  }
  output(serialized);
  return result.ok === true ? 0 : 1;
}

module.exports = {create,nativeProfile,validRef,allowedRefs,configuredLegacy,providerAdmission,main};
if (require.main === module) main().then(code => {process.exitCode=code;}).catch(() => {
  process.stdout.write(JSON.stringify(failure('unavailable','Native Strata coordination failed.'))+'\n');
  process.exitCode=1;
});

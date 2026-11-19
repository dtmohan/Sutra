'use strict';
const {execFile} = require('node:child_process');
const {createHash} = require('node:crypto');
const {constants, promises:fs} = require('node:fs');
const http = require('node:http');
const {connect} = require('node:net');
const {homedir} = require('node:os');
const {isAbsolute, join} = require('node:path');

const settingKeys = ['models', 'strata.startOnStartup', 'strata.port', 'strata.model'];
const startupPath = 'Library/Application Support/Sutra/Strata/startup.json';
const environmentKeys = new Set(['STRATA_COMPONENTS_DIR', 'STRATA_INSTALL_DIR', 'STRATA_NO_ACQUIRE',
  'STRATA_METAL_ARENA_GB', 'STRATA_METAL_STREAM', 'STRATA_PROFILE_DIR', 'STRATA_RUNTIME_DIR', 'XDG_CONFIG_HOME']);
const identity = value => typeof value === 'string' && /^[0-9a-f]{32}$/.test(value);

function selection(config, automatic = true) {
  const user = key => config.inspect?.(key)?.globalValue;
  const models=user('models')||{};
  if ((automatic && user('strata.startOnStartup') !== true) ||
      !Object.values(models).some(name=>name==='strata:high'||
        typeof name==='string'&&/^strata:profile\/[a-z][a-z0-9_-]{0,63}$/.test(name))) return {};
  const port = user('strata.port'), model = user('strata.model');
  if (!Number.isInteger(port) || port < 1 || port > 65535 ||
      typeof model !== 'string' || !model.trim() || model.length > 200 || /[\x00-\x1f\x7f]/.test(model)) {
    return {invalid:true};
  }
  return {port, model:model.trim()};
}

function probePort(port) {
  return new Promise((resolve, reject) => {
    const socket = connect({host:'127.0.0.1', port});
    const finish = (error, occupied) => {
      socket.destroy();
      if (error) reject(error);
      else resolve(occupied);
    };
    socket.once('connect', () => finish(null, true));
    socket.once('error', error => finish(error.code === 'ECONNREFUSED' ? null : error, false));
    socket.setTimeout(1500, () => finish(new Error('Loopback connection timed out')));
  });
}

async function readStartupProfile() {
  const file = await fs.open(join(homedir(), startupPath),
    constants.O_RDONLY | constants.O_NONBLOCK | constants.O_NOFOLLOW);
  try {
    const buffer = Buffer.alloc(8193), stat = await file.stat();
    if (!stat.isFile() || stat.size >= buffer.length) throw new Error('Invalid Strata startup profile');
    const {bytesRead} = await file.read(buffer);
    if (bytesRead >= buffer.length) throw new Error('Invalid Strata startup profile');
    return JSON.parse(buffer.toString('utf8', 0, bytesRead));
  } finally {
    await file.close();
  }
}

function validProfile(profile, selected) {
  return profile && profile.port === selected.port && profile.model === selected.model &&
    typeof profile.executable === 'string' && isAbsolute(profile.executable) &&
    !/[\x00-\x1f\x7f]/.test(profile.executable) &&
    typeof profile.profile === 'string' && /^[a-z][a-z0-9_-]{0,63}$/.test(profile.profile) &&
    ((Number.isSafeInteger(profile.cold_memory_bytes) && profile.cold_memory_bytes > 0) ||
      (profile.cold_memory_bytes === null && profile.native_fit_authorized === true && profile.memory_binding)) &&
    profile.environment && typeof profile.environment === 'object' && !Array.isArray(profile.environment) &&
    Object.entries(profile.environment).every(([key,value]) => environmentKeys.has(key) &&
      typeof value === 'string' && value.length <= 4096 && !/[\x00-\x1f\x7f]/.test(value));
}

function nativeCommand(profile, action, args = []) {
  return new Promise((resolve, reject) => {
    execFile(profile.executable, ['runtime', action, '--profile', profile.profile, ...args, '--output', 'json'],
      {env:{...process.env, ...profile.environment}, timeout:action === 'stop' ? 40000 : 15000,
       maxBuffer:262144, windowsHide:true}, (error, stdout) => {
        try {
          const result = JSON.parse(stdout);
          if (typeof result?.ok !== 'boolean') throw new Error('Malformed Strata runtime response');
          // Status includes useful stopped/stale/failed observations on nonzero exit.
          resolve(result);
        } catch {
          reject(error || new Error('Strata returned an invalid runtime response'));
        }
      });
  });
}

function serverJSON(port, pathname) {
  return new Promise((resolve, reject) => {
    const headers = process.env.SUTRA_STRATA_API_KEY ? {Authorization:'Bearer '+process.env.SUTRA_STRATA_API_KEY} : {};
    const request = http.get({host:'127.0.0.1', port, path:pathname, headers}, response => {
      let body = '';
      response.setEncoding('utf8');
      response.on('data', chunk => {
        body += chunk;
        if (Buffer.byteLength(body) > 65536) request.destroy(new Error('Strata status is too large'));
      });
      response.on('end', () => {
        try {
          if (response.statusCode !== 200) throw new Error('Strata status is unavailable');
          resolve(JSON.parse(body));
        } catch (error) {
          reject(error);
        }
      });
      response.on('error', reject);
    });
    request.on('error', reject);
    request.setTimeout(2000, () => request.destroy(new Error('Strata status timed out')));
  });
}

async function readServer(port) {
  const [server, telemetry] = await Promise.all([
    serverJSON(port, '/v1/status'), serverJSON(port, '/v1/telemetry')
  ]);
  if (telemetry.instance?.server !== 'strata' || telemetry.model?.id !== server.model) {
    throw new Error('Strata telemetry does not match the status response');
  }
  return {...server, instance_id:telemetry.instance.id};
}

function runtimeState(result, profile, freshStart = false) {
  if (result?.ok !== true || result.profile !== profile.profile) return;
  if (result.state === 'ready') return {
    status:'unavailable',
    message:'Strata reports a running native instance, but the configured endpoint was unavailable. Check its provider status before using High.'
  };
  if (result.state === 'starting' && (freshStart || identity(result.owner_id))) return {
    status:'starting',
    message:'Strata is loading through its native runtime. High becomes available after the server finishes loading.'
  };
}

function matchingPlan(result, profile) {
  const plan = result?.ok === true && result.plan, launch = plan?.launch;
  return plan?.profile === profile.profile && launch?.profile === profile.profile &&
    launch.model_name === profile.model && launch.effective?.host === '127.0.0.1' &&
    launch.effective.port === profile.port && /^[0-9a-f]{16}$/.test(launch.profile_revision) &&
    /^[0-9a-f]{64}$/.test(plan.source_sha256);
}

function memoryBinding(profile, plan) {
  return {profile_revision:plan.launch.profile_revision, source_sha256:plan.source_sha256,
    environment_sha256:createHash('sha256').update(JSON.stringify({executable:profile.executable,
      environment:Object.entries(profile.environment).sort(([a],[b])=>a.localeCompare(b))})).digest('hex')};
}

async function configured(getConfig, readProfile, automatic) {
  const selected = selection(getConfig(), automatic);
  if (!selected.port) return {result:selected.invalid ? {
    status:'invalid', message:'Set a valid Strata port and model in VS Code User Settings.'
  } : {
    status:'disabled', message:'Select Strata High and enable Karma › Strata: Start On Startup in VS Code User Settings.'
  }};
  let profile;
  try { profile = await readProfile(); } catch {}
  const current = selection(getConfig(), automatic);
  if (current.port !== selected.port || current.model !== selected.model) return {
    result:{status:'changed', message:'Strata settings changed before the runtime operation.'}
  };
  if (!validProfile(profile, selected)) return {result:{
    status:'setup', message:'Open Sutra → Settings → Models to locate Strata and load an installed native profile before enabling automatic startup.'
  }};
  return {selected, profile};
}

async function start(getConfig, {
  platform = process.platform, remote = false, automatic = true, probe = probePort, readProfile = readStartupProfile,
  run = nativeCommand, admit = async () => ({admitted:false, reason:'Startup resource admission is unavailable.'})
} = {}) {
  if (platform !== 'darwin' || remote) return {
    status:'unsupported', message:'Sutra can manage the native Strata runtime from a local macOS VS Code window.'
  };
  const selected = selection(getConfig(), automatic);
  if (selected.port) {
    try {
      if (await probe(selected.port)) return {
        status:'occupied', message:`Port ${selected.port} is already in use. Sutra checks the configured Strata model before routing High requests.`
      };
    } catch {
      return {status:'unavailable', message:'Could not check the configured Strata port.'};
    }
  }
  const config = await configured(getConfig, readProfile, automatic);
  if (config.result) return config.result;
  const {profile} = config;
  const unchanged = () => {
    const current = selection(getConfig(), automatic);
    return current.port === profile.port && current.model === profile.model;
  };
  try {
    const current = await run(profile, 'status');
    const existing = runtimeState(current, profile);
    if (existing) return existing;
    if (current.profile !== profile.profile || !['stopped', 'stale', 'failed'].includes(current.state)) return {
      status:'unavailable', message:'Strata native runtime state is unavailable. Inspect its status before starting again.'
    };
    const plan = await run(profile, 'start', ['--plan']);
    if (!matchingPlan(plan, profile)) return {
      status:'mismatch', message:'The native Strata start plan differs from the configured loopback port or model.'
    };
    if (profile.memory_binding && JSON.stringify(profile.memory_binding)!==JSON.stringify(memoryBinding(profile,plan.plan))) return {
      status:'changed', message:'The native profile differs from its memory calibration. Load it from Sutra model settings with fresh hardware checks.'
    };
    const admission = await admit(profile.cold_memory_bytes);
    if (admission?.admitted !== true) return {
      status:'paused', message:'Strata startup paused. '+(admission?.reason || 'Resource admission is unavailable.'),
      decision:admission?.decision
    };
    const latest = await run(profile, 'start', ['--plan']);
    if (!matchingPlan(latest, profile) || JSON.stringify(latest.plan) !== JSON.stringify(plan.plan) || !unchanged()) return {
      status:'changed', message:'The Strata profile or settings changed during startup. Review the configuration and try again.'
    };
    let started;
    try {
      started = await run(profile, 'start', ['--wait', '0']);
    } catch {
      // The CLI may have started its owner before its own response was interrupted.
    }
    const observed = runtimeState(started, profile, true);
    if (observed) return observed;
    const recovered = runtimeState(await run(profile, 'status'), profile);
    return recovered || {
      status:'uncertain', message:'Strata did not confirm startup. Inspect its native runtime status before retrying.'
    };
  } catch {
    return {status:'failed', message:'Could not inspect the native Strata runtime. Check its executable and saved profile.'};
  }
}

async function stop(getConfig, {
  platform = process.platform, remote = false, readProfile = readStartupProfile, run = nativeCommand,
  inspectServer = readServer, busy = false
} = {}) {
  if (platform !== 'darwin' || remote) return {
    status:'unsupported', message:'Sutra can manage the native Strata runtime from a local macOS VS Code window.'
  };
  if (busy) return {status:'busy', message:'Wait for the active Sutra request before stopping Strata.'};
  const config = await configured(getConfig, readProfile, false);
  if (config.result) return config.result;
  const {profile} = config;
  try {
    const preview = await run(profile, 'stop', ['--plan']), plan = preview?.plan;
    if (preview?.ok !== true || plan?.profile !== profile.profile ||
        !identity(plan.owner_id) || !identity(plan.instance_id)) return {
      status:'unavailable', message:'A running Strata owner and model instance could not be verified. Inspect native runtime status.'
    };
    const server = await inspectServer(profile.port);
    if (server?.service !== 'strata' || server.model !== profile.model || server.instance_id !== plan.instance_id) return {
      status:'mismatch', message:'The Strata server differs from the reviewed native runtime instance.'
    };
    if (!Number.isInteger(server.activity?.in_flight) || server.activity.in_flight !== 0) return {
      status:'busy', message:'Strata has active requests or unavailable activity data. Wait before stopping it.'
    };
    const current = selection(getConfig(), false);
    if (current.port !== profile.port || current.model !== profile.model) return {
      status:'changed', message:'Strata settings changed before stopping the runtime.'
    };
    let stopped;
    try {
      stopped = await run(profile, 'stop', ['--wait','30','--if-owner-id',plan.owner_id,'--if-instance-id',plan.instance_id]);
    } catch {}
    if (stopped?.ok === true && stopped.profile === profile.profile && stopped.state === 'stopped') return {
      status:'stopped', message:'Strata stopped its reviewed native runtime.'
    };
    const observed = await run(profile, 'status');
    return observed.ok === true && observed.profile === profile.profile && observed.state === 'stopped' ? {
      status:'stopped', message:'Strata confirms its native runtime is stopped.'
    } : {
      status:'uncertain', message:'Strata did not confirm the stop. Inspect native runtime status before retrying.'
    };
  } catch {
    return {status:'unavailable', message:'Could not verify the native Strata runtime before stopping it.'};
  }
}

// Machine-management UI uses the same identity and lifecycle checks.
module.exports = {settingKeys, start, stop, validProfile, readStartupProfile,
  nativeCommand, readServer, matchingPlan, memoryBinding, startupPath};

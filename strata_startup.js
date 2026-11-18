'use strict';
const {execFile} = require('node:child_process');
const {constants, promises:fs} = require('node:fs');
const {connect} = require('node:net');
const {homedir} = require('node:os');
const {join} = require('node:path');

const label = 'io.sutra.strata-high';
const settingKeys = ['models', 'strata.startOnStartup', 'strata.port', 'strata.model'];
const startupPath = 'Library/Application Support/Sutra/Strata/startup.json';

function selection(config) {
  const user = key => config.inspect?.(key)?.globalValue;
  if (user('strata.startOnStartup') !== true || user('models')?.high !== 'strata:high') return {};
  const port = user('strata.port');
  const model = user('strata.model');
  if (!Number.isInteger(port) || port < 1 || port > 65535 ||
      typeof model !== 'string' || !model.trim() || model.length > 200 || /[\x00-\x1f\x7f]/.test(model)) {
    return {invalid: true};
  }
  return {port, model: model.trim()};
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
    socket.once('error', error => {
      if (error.code === 'ECONNREFUSED') finish(null, false);
      else finish(error);
    });
    socket.setTimeout(1500, () => finish(new Error('Loopback connection timed out')));
  });
}

async function readStartupProfile() {
  const flags = constants.O_RDONLY | constants.O_NONBLOCK | constants.O_NOFOLLOW;
  const file = await fs.open(join(homedir(), startupPath), flags);
  try {
    const buffer = Buffer.alloc(1025);
    const stat = await file.stat();
    if (!stat.isFile() || stat.size >= buffer.length) throw new Error('Invalid Strata startup profile');
    const {bytesRead} = await file.read(buffer);
    if (bytesRead >= buffer.length) throw new Error('Invalid Strata startup profile');
    return JSON.parse(buffer.toString('utf8', 0, bytesRead));
  } finally {
    await file.close();
  }
}

function kickstart(uid) {
  return new Promise((resolve, reject) => {
    // launchd owns the single service instance across windows and profiles.
    // Omit -k so another window cannot restart a running service.
    execFile('/bin/launchctl', ['kickstart', `gui/${uid}/${label}`],
      {timeout:5000, maxBuffer:8192}, error => error ? reject(error) : resolve());
  });
}

async function start(getConfig, {
  platform = process.platform,
  uid = process.getuid?.(),
  remote = false,
  probe = probePort,
  readProfile = readStartupProfile,
  launch = kickstart
} = {}) {
  if (platform !== 'darwin' || remote || !Number.isInteger(uid)) return {
    status:'unsupported', message:'Sutra can start the Strata LaunchAgent from a local macOS VS Code window.'
  };
  const {port, model, invalid} = selection(getConfig());
  if (!port) return invalid ? {
    status:'invalid', message:'Set a valid Strata port and model in VS Code User Settings.'
  } : {
    status:'disabled', message:'Select Strata High and enable Karma › Strata: Start On Startup in VS Code User Settings.'
  };
  try {
    if (await probe(port)) return {
      status:'occupied', message:`Port ${port} is already in use. Sutra will check for the configured Strata model before routing High requests.`
    };
  } catch {
    return {status:'unavailable', message:`Could not check Strata's local port ${port}.`};
  }
  let profile;
  try {
    profile = await readProfile();
  } catch {
    // Missing and unreadable profiles both require local setup before launch.
  }
  const current = selection(getConfig());
  if (current.port !== port || current.model !== model) return {
    status:'changed', message:'Strata startup settings changed before launch.'
  };
  if (!profile || typeof profile.port !== 'number' || typeof profile.model !== 'string') return {
    status:'setup', message:`Set up the Strata LaunchAgent and its model profile at ~/${startupPath} before starting it from Sutra.`
  };
  if (port !== profile.port || model !== profile.model) return {
    status:'mismatch', message:`The Strata port or model in VS Code User Settings differs from ~/${startupPath}. Make the settings and installed profile match before starting it from Sutra.`
  };
  try {
    await launch(uid);
    return {
      status:'requested', message:`Strata launch requested for ${model}. The High tier will become available when the local server finishes loading.`
    };
  } catch {
    return {
      status:'failed', message:`Could not start the macOS LaunchAgent ${label}. Check that it is installed and enabled for this macOS user.`
    };
  }
}

module.exports = {settingKeys, start};

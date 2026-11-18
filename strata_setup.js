'use strict';

const {execFile} = require('node:child_process');
const {constants, promises:fs} = require('node:fs');
const {homedir} = require('node:os');
const {basename, dirname, isAbsolute, join} = require('node:path');

const environmentKeys = new Set([
  'STRATA_COMPONENTS_DIR', 'STRATA_INSTALL_DIR', 'STRATA_NO_ACQUIRE',
  'STRATA_METAL_ARENA_GB', 'STRATA_METAL_STREAM', 'STRATA_PROFILE_DIR',
  'STRATA_RUNTIME_DIR', 'XDG_CONFIG_HOME'
]);
const profileId = value => typeof value === 'string' && /^[a-z][a-z0-9_-]{0,63}$/.test(value);
const safePath = value => typeof value === 'string' && value.length <= 4096 &&
  isAbsolute(value) && !/[\x00-\x1f\x7f]/.test(value);

function trustedEnvironment(value = {}) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('Invalid native Strata environment.');
  }
  const result = {};
  for (const [key, item] of Object.entries(value)) {
    if (!environmentKeys.has(key) || typeof item !== 'string' || item.length > 4096 ||
        /[\x00-\x1f\x7f]/.test(item)) throw new Error('Invalid native Strata environment.');
    result[key] = item;
  }
  return result;
}

function managedEnvironment(home = homedir()) {
  const base = join(home, 'Library', 'Application Support', 'Sutra', 'Strata');
  return {
    STRATA_COMPONENTS_DIR:join(base, 'components'),
    STRATA_INSTALL_DIR:join(base, 'runtime'),
    STRATA_RUNTIME_DIR:join(home, '.local', 'state', 'sutra-strata'),
    XDG_CONFIG_HOME:join(base, 'config'),
    STRATA_NO_ACQUIRE:'1'
  };
}

function nativeCandidates({home = homedir(), platform = process.platform} = {}) {
  const local = [join(home, '.local', 'bin', 'strata')];
  if (platform === 'darwin') return [
    {executable:join(home, 'Library', 'Application Support', 'Sutra', 'Strata',
      'mount', 'Strata.app', 'Contents', 'Resources', 'control', 'bin', 'strata'),
    environment:managedEnvironment(home), source:'managed'},
    ...[join('/Applications', 'Strata.app'), join(home, 'Applications', 'Strata.app')]
      .map(app => ({executable:join(app, 'Contents', 'Resources', 'control', 'bin', 'strata'), source:'application'})),
    ...local.map(executable => ({executable, source:'local'}))
  ];
  if (platform === 'linux') return [...local, '/usr/local/bin/strata', '/usr/bin/strata']
    .map(executable => ({executable, source:'local'}));
  // ProcessExecution executes a real executable directly. Windows .cmd wrappers need a
  // shell and are deliberately left to the native installer's own integration.
  if (platform === 'win32') return [
    join(home, 'AppData', 'Local', 'Strata', 'strata.exe'),
    join(home, '.local', 'bin', 'strata.exe')
  ].map(executable => ({executable, source:'local'}));
  return [];
}

async function readStartupRecord(home = homedir()) {
  const filename = join(home, 'Library', 'Application Support', 'Sutra', 'Strata', 'startup.json');
  const file = await fs.open(filename, constants.O_RDONLY | constants.O_NONBLOCK | (constants.O_NOFOLLOW || 0));
  try {
    const stat = await file.stat();
    if (!stat.isFile() || stat.size > 8192) throw new Error('Invalid Strata startup record.');
    const buffer = Buffer.alloc(8193);
    const {bytesRead} = await file.read(buffer);
    if (bytesRead > 8192) throw new Error('Invalid Strata startup record.');
    return JSON.parse(buffer.toString('utf8', 0, bytesRead));
  } finally {
    await file.close();
  }
}

async function executableExists(executable) {
  if (!safePath(executable)) return false;
  try {
    const stat = await fs.stat(executable);
    if (!stat.isFile()) return false;
    await fs.access(executable, constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

function runReadOnly(executable, args, options) {
  return new Promise((resolve, reject) => {
    execFile(executable, args, {
      env:{...process.env, ...options.environment, STRATA_NO_ACQUIRE:'1'},
      timeout:10000, maxBuffer:262144, windowsHide:true
    }, (error, stdout) => error ? reject(error) : resolve(stdout));
  });
}

async function inspectNative(executable, environment = {}, {
  exists = executableExists, run = runReadOnly, source = 'selected'
} = {}) {
  if (!safePath(executable) || !(await exists(executable))) {
    throw new Error('Select an installed native Strata executable.');
  }
  const clean = trustedEnvironment(environment);
  const raw = await run(executable, ['profile', 'list', '--output', 'json'], {environment:clean});
  if (typeof raw === 'string' && Buffer.byteLength(raw) > 262144) {
    throw new Error('The native Strata profile response exceeded the inspection limit.');
  }
  const result = typeof raw === 'string' ? JSON.parse(raw) : raw;
  if (result?.ok !== true || result.schema_version !== 1 || !Array.isArray(result.profiles) ||
      result.profiles.length > 128 || result.profiles.some(profile => !profileId(profile?.id) ||
        !/^[0-9a-f]{16}$/.test(profile.revision) || !safePath(profile.source_config))) {
    throw new Error('The selected executable did not return a valid native Strata profile list.');
  }
  return {
    verified:true, executable, environment:clean, source,
    profiles:result.profiles.map(profile => ({
      id:profile.id, revision:profile.revision, source_config:profile.source_config
    }))
  };
}

async function findNative({
  home = homedir(), platform = process.platform, remote = false,
  readRecord = readStartupRecord, exists = executableExists, run = runReadOnly
} = {}) {
  if (remote || !['darwin', 'linux', 'win32'].includes(platform)) {
    return {status:'unsupported', message:'Native Strata setup needs a supported local VS Code host.'};
  }
  const candidates = [], attempts = [], seen = new Set();
  try {
    const saved = await readRecord(home);
    if (safePath(saved?.executable)) candidates.push({
      executable:saved.executable, environment:trustedEnvironment(saved.environment || {}), source:'startup'
    });
  } catch {}
  candidates.push(...nativeCandidates({home, platform}));
  for (const candidate of candidates) {
    if (seen.has(candidate.executable)) continue;
    seen.add(candidate.executable);
    if (!(await exists(candidate.executable))) continue;
    try {
      const native = await inspectNative(candidate.executable, candidate.environment, {
        exists, run, source:candidate.source
      });
      return {status:'found', ...native};
    } catch {
      attempts.push({executable:candidate.executable, status:'inspection_failed'});
    }
  }
  return {
    status:'missing', attempts,
    message:'Install the native Strata runtime, or select its installed executable, then open model setup again.'
  };
}

async function resolveInstaller(native, {realpath = fs.realpath, stat = fs.stat, access = fs.access} = {}) {
  if (native?.verified !== true || !safePath(native.executable)) {
    throw new Error('Inspect the installed Strata executable before opening native model setup.');
  }
  const executable = await realpath(native.executable);
  if (!safePath(executable) || basename(dirname(executable)) !== 'bin') {
    throw new Error('This native Strata installation has no supported bundled installer. Import an existing native configuration to load a model.');
  }
  const root = dirname(dirname(executable));
  const script = join(root, 'setup.py');
  const python = join(root, 'python', 'bin', 'python3');
  try {
    const [scriptStat, pythonStat] = await Promise.all([stat(script), stat(python)]);
    if (!scriptStat.isFile() || !pythonStat.isFile()) throw new Error('Missing native installer files');
    await access(python, constants.X_OK);
  } catch {
    throw new Error('This native Strata installation has no supported bundled installer. Import an existing native configuration to load a model.');
  }
  return Object.freeze({verified:true, native_executable:native.executable, executable:python, script});
}

function validSetupPlan(plan) {
  const settings = plan?.settings;
  return settings && typeof settings.family === 'string' && /^[a-z][a-z0-9_-]{0,63}$/.test(settings.family) &&
    typeof settings.model === 'string' && /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(settings.model) &&
    settings.context === 32768 && settings.vision === 'off' && settings.drafting === 'off' &&
    settings.mtp === 'off' && settings.access === 'local' &&
    Number.isInteger(settings.port) && settings.port >= 1 && settings.port <= 65535 &&
    Number.isFinite(plan.download?.gb) && plan.download.gb >= 0 &&
    typeof plan.download.text === 'string' && plan.download.text.length <= 4096 &&
    typeof plan.model?.title === 'string' && plan.model.title.length > 0 && plan.model.title.length <= 200;
}

async function prepareSetup(native, {port = 19483, run = runReadOnly} = {}) {
  if (native?.verified !== true || !safePath(native.executable)) {
    throw new Error('Inspect the installed Strata executable before preparing model setup.');
  }
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Choose a valid native model port.');
  const argv = ['launch', '--context', '32k', '--vision', 'off', '--drafting', 'off', '--mtp', 'off',
    '--port', String(port), '--plan', '--output', 'json'];
  const raw = await run(native.executable, argv, {environment:trustedEnvironment(native.environment)});
  if (typeof raw === 'string' && Buffer.byteLength(raw) > 262144) throw new Error('Native Strata setup plan is too large.');
  const result = typeof raw === 'string' ? JSON.parse(raw) : raw;
  if (result?.ok !== true || !validSetupPlan(result.plan) || result.plan.settings.port !== port) {
    throw new Error('Native Strata did not provide a valid install-only model plan.');
  }
  let inspection;
  try {
    const args = ['models', 'inspect', result.plan.settings.model.toLowerCase()];
    const install = native.environment?.STRATA_INSTALL_DIR;
    if (install && safePath(install)) args.push('--install-dir', install);
    args.push('--output', 'json');
    const rawInspection = await run(native.executable, args, {environment:trustedEnvironment(native.environment)});
    if (typeof rawInspection === 'string' && Buffer.byteLength(rawInspection) > 262144) {
      throw new Error('Native model inspection is too large.');
    }
    inspection = typeof rawInspection === 'string' ? JSON.parse(rawInspection) : rawInspection;
  } catch { /* A new model has no installed run configuration to inspect. */ }
  const registered = inspection?.model?.id === result.plan.settings.model.toLowerCase() &&
    safePath(inspection.model.source_config) && /^[0-9a-f]{64}$/.test(inspection.model.source_sha256);
  if (registered && (inspection.ok !== true || inspection.model.component_status !== 'present')) {
    throw new Error('This model is already registered but required components need repair. Use Strata’s native update or repair tools, then add its existing configuration. No setup was started.');
  }
  const alreadyInstalled = !!(registered && inspection.ok === true && inspection.model.component_status === 'present');
  if (!alreadyInstalled && result.plan.download.gb === 0) {
    throw new Error('Strata’s plan expects existing model files, but their installed configuration could not be verified. Add the native configuration instead of starting another download.');
  }
  return {plan:result.plan, model:result.plan.model.title, download_gb:result.plan.download.gb,
    message:result.plan.download.text, already_installed:alreadyInstalled};
}

function setupCommand(native, {
  explicitSetup = false, environment = process.env, cwd = homedir(), installer, plan
} = {}) {
  if (explicitSetup !== true) throw new Error('Native Strata setup must be explicitly requested by the user.');
  if (native?.verified !== true || !safePath(native.executable) || !safePath(cwd)) {
    throw new Error('Inspect the installed Strata executable before opening native model setup.');
  }
  if (installer?.verified !== true || installer.native_executable !== native.executable ||
      !safePath(installer.executable) || !safePath(installer.script)) {
    throw new Error('Verify the bundled native Strata installer before setup.');
  }
  if (!validSetupPlan(plan)) throw new Error('Review a native Strata model and download plan before setup.');
  const settings = plan.settings;
  const env = {...environment, ...trustedEnvironment(native.environment)};
  // Only this explicit setup entry can acquire components. Ordinary startup and
  // read-only discovery retain STRATA_NO_ACQUIRE=1.
  delete env.STRATA_NO_ACQUIRE;
  for (const key of ['PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV']) delete env[key];
  env.STRATA_CONTROL_INSTALL = '1';
  env.PYTHONNOUSERSITE = '1';
  env.STRATA_INSTALL_DIR ||= managedEnvironment().STRATA_INSTALL_DIR;
  if (!safePath(env.STRATA_INSTALL_DIR)) throw new Error('Locate a writable native Strata installation folder before installing models.');
  return {executable:installer.executable,
    args:[installer.script, '--setup', '--no-start', '--no-browser', '--host', '127.0.0.1',
      '--port', String(settings.port), '--family', settings.family, '--model', settings.model,
      '--context', String(settings.context), '--vision', 'no', '--mtp', 'off'],
    options:{cwd, env}};
}

function setupTask(vscode, native, options = {}) {
  const command = setupCommand(native, options);
  const execution = new vscode.ProcessExecution(command.executable, command.args, command.options);
  const task = new vscode.Task(
    {type:'sutra-strata-setup', version:1}, vscode.TaskScope.Global,
    'Set up local models with Strata', 'Sutra', execution, []
  );
  task.detail = 'Install model files using Strata’s bundled installer. Loading stays in Sutra’s resource-checked model manager.';
  task.presentationOptions = {
    reveal:vscode.TaskRevealKind.Always, panel:vscode.TaskPanelKind.Dedicated,
    focus:true, clear:false, showReuseMessage:true
  };
  return {task, command};
}

async function startSetupTask(vscode, native, options = {}) {
  const installer = await (options.resolveInstaller || resolveInstaller)(native);
  const {task, command} = setupTask(vscode, native, {...options, installer});
  const execution = await vscode.tasks.executeTask(task);
  return {
    status:'started', task, execution, command,
    message:'Strata’s install-only task is open in the VS Code terminal. Add its configuration and load it from Sutra after setup finishes.'
  };
}

module.exports = {
  trustedEnvironment, managedEnvironment, nativeCandidates, readStartupRecord,
  inspectNative, findNative, resolveInstaller, prepareSetup, setupCommand, setupTask, startSetupTask
};

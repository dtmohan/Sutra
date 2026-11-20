'use strict';

const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

const digest = value => crypto.createHash('sha256').update(JSON.stringify(value)).digest('hex');
const profiles = Object.freeze({read_only: 'Read only', workspace_agent: 'Workspace agent'});

function createStore(vscode, context, dependencies = {}) {
  const watch = dependencies.watch || fs.watch;
  const schedule = dependencies.setInterval || setInterval;
  const cancelSchedule = dependencies.clearInterval || clearInterval;
  const directory = path.join(context.globalStorageUri.fsPath, 'workspace-permissions');
  function location() {
    const uri = vscode.workspace.workspaceFile || vscode.workspace.workspaceFolders?.[0]?.uri;
    if (!uri) throw new Error('Open a local folder to configure Sutra workspace permissions.');
    return path.join(directory, digest(uri.toString()) + '.json');
  }
  function scope() {
    if (!vscode.workspace.isTrusted) throw new Error('Trust this workspace in VS Code before granting Sutra access.');
    const folders = vscode.workspace.workspaceFolders || [];
    if (!folders.length || folders.some(folder => folder.uri.scheme !== 'file')) {
      throw new Error('Sutra workspace permissions require local folders.');
    }
    const roots = folders.map(({uri}) => {
      const realPath = fs.realpathSync.native(uri.fsPath);
      const stat = fs.statSync(realPath, {bigint: true});
      return [uri.fsPath, realPath, String(stat.dev), String(stat.ino)];
    });
    return digest([location(), roots]);
  }
  function read() {
    const file = location();
    try {
      return JSON.parse(fs.readFileSync(file, 'utf8'));
    } catch (error) {
      if (error.code !== 'ENOENT' && !(error instanceof SyntaxError)) throw error;
    }
  }
  function write(record) {
    const file = location();
    fs.mkdirSync(directory, {recursive: true, mode: 0o700});
    const temp = file + '.' + crypto.randomUUID() + '.tmp';
    try {
      fs.writeFileSync(temp, JSON.stringify(record), {flag: 'wx', mode: 0o600});
      fs.renameSync(temp, file);
    } finally {
      fs.rmSync(temp, {force: true});
    }
  }
  function evidence(expected) {
    const file=location();
    const descriptor=fs.openSync(file,fs.constants.O_RDONLY|(fs.constants.O_NOFOLLOW||0)|(fs.constants.O_NONBLOCK||0));
    try {
      const info=fs.fstatSync(descriptor);
      if(!info.isFile()||info.nlink!==1||info.size>8192||
          (process.getuid&&info.uid!==process.getuid())||
          (process.platform!=='win32'&&(info.mode&0o077)))throw new Error('Workspace authorization record is not private.');
      const bytes=Buffer.alloc(8193),count=fs.readSync(descriptor,bytes,0,bytes.length,0);
      if(count>8192)throw new Error('Workspace authorization record exceeds its bound.');
      const grant=JSON.parse(bytes.subarray(0,count).toString('utf8'));
      if(!expected||grant.id!==expected.generation||grant.scope!==expected.scope||grant.profile!==expected.profile)
        throw new Error('Workspace permission changed before submission.');
      const canonicalFile=path.join(fs.realpathSync.native(path.dirname(file)),path.basename(file));
      return {path:canonicalFile,digest:crypto.createHash('sha256').update(bytes.subarray(0,count)).digest('hex')};
    } finally {fs.closeSync(descriptor);}
  }
  function onDidChange(listener) {
    fs.mkdirSync(directory, {recursive: true, mode: 0o700});
    let watcher;
    // File notifications may be coalesced or silently missed. Reconcile active
    // grants periodically as well, so a missed event cannot retain authority.
    const poll = schedule(listener, 500);
    poll.unref?.();
    function closeWatcher() {
      watcher?.close();
    }
    try {
      watcher = watch(directory, {persistent: false}, (_event, name) => {
        if (!name || String(name).endsWith('.json')) listener();
      });
      watcher.once('error', closeWatcher);
    } catch {
      // Polling remains active when this host cannot create native watchers.
    }
    return {dispose() {
      watcher?.close();
      cancelSchedule(poll);
    }};
  }
  return {scope, read, write, evidence, onDidChange};
}

module.exports = {createStore, profiles};

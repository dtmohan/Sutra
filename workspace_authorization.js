'use strict';

const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

const digest = value => crypto.createHash('sha256').update(JSON.stringify(value)).digest('hex');
const profiles = Object.freeze({read_only: 'Read only', workspace_agent: 'Workspace agent'});

function createStore(vscode, context) {
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
  function onDidChange(listener) {
    fs.mkdirSync(directory, {recursive: true, mode: 0o700});
    let watcher;
    let poll;
    function fallback() {
      watcher?.close();
      // Some extension hosts cannot create native file watchers.
      poll = setInterval(listener, 500);
      poll.unref();
    }
    try {
      watcher = fs.watch(directory, {persistent: false}, (_event, name) => {
        if (!name || name.endsWith('.json')) listener();
      });
      watcher.once('error', fallback);
    } catch {
      fallback();
    }
    return {dispose() {
      watcher?.close();
      clearInterval(poll);
    }};
  }
  return {scope, read, write, onDidChange};
}

module.exports = {createStore, profiles};

'use strict';

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const {EventEmitter} = require('node:events');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const {install} = require('../native_provider');
const {createStore} = require('../workspace_authorization');

const disposable = () => ({dispose() {}});
const uri = fsPath => ({fsPath, scheme: 'file', toString: () => pathToFileURL(fsPath).href});
const folder = fsPath => ({uri: uri(fsPath)});

class CancellationTokenSource {
  constructor() {
    const listeners = new EventEmitter();
    this.token = {
      isCancellationRequested: false,
      onCancellationRequested(listener) {
        listeners.on('cancel', listener);
        return {dispose: () => listeners.off('cancel', listener)};
      },
    };
    this.cancel = () => {
      this.token.isCancellationRequested = true;
      listeners.emit('cancel');
    };
    this.dispose = () => listeners.removeAllListeners();
  }
}

function windowFor(root, storage, dependencies) {
  const commands = new Map();
  const status = new EventEmitter();
  const vscode = {
    EventEmitter, CancellationTokenSource,
    window: {},
    workspace: {
      isTrusted: true,
      workspaceFolders: [folder(root)],
      getConfiguration: () => ({get: (_key, fallback) => fallback}),
    },
    commands: {
      executeCommand: async () => {},
      registerCommand(name, callback) {
        commands.set(name, callback);
        return disposable();
      },
    },
    lm: {registerLanguageModelChatProvider: disposable, registerTool: disposable},
  };
  const context = {globalStorageUri: {fsPath: storage}, workspaceState: new Map(), subscriptions: []};
  const authorization = createStore(vscode, context, dependencies);
  const controller = install(vscode, context, {
    authorization, workspace: () => root, modelSettings: () => ({}),
    onStatus: value => status.emit('access', value),
  });
  return {vscode, authorization, controller, commands, status};
}

async function check() {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'sutra-permission-store-'));
  const root = path.join(directory, 'project');
  const extra = path.join(directory, 'extra');
  const storage = path.join(directory, 'extension-storage');
  for (const name of [root, extra, storage]) fs.mkdirSync(name);
  const first = windowFor(root, storage);
  const second = windowFor(root, storage);
  const {authorization} = first;
  const savedGrant = {id: crypto.randomUUID(), profile: 'workspace_agent', scope: authorization.scope()};
  try {
    assert.equal(authorization.read(), undefined);
    authorization.write(savedGrant);
    assert.equal(first.controller.status().accessProfile, 'workspace_agent');
    assert.equal(second.controller.status().accessProfile, 'workspace_agent');
    const expected={workspace:root,generation:savedGrant.id,scope:savedGrant.scope,profile:savedGrant.profile};
    const proof=authorization.evidence(expected);
    assert.match(proof.digest,/^[a-f0-9]{64}$/);
    assert.equal(path.dirname(proof.path),fs.realpathSync.native(path.dirname(proof.path)));
    assert.deepEqual(first.controller.permissionEvidence(expected),proof);
    authorization.write({...savedGrant,id:crypto.randomUUID()});
    assert.throws(()=>authorization.evidence(expected),/permission changed/);
    authorization.write({...savedGrant,profile:'read_only'});
    assert.throws(()=>authorization.evidence(expected),/permission changed/);
    authorization.write(savedGrant);

    let interrupted = false;
    const active = second.controller.withWorkspaceAccess(undefined, token => new Promise(resolve => {
      token.onCancellationRequested(() => {
        interrupted = true;
        resolve();
      });
    }));
    const rejected = assert.rejects(active, /permission changed|cancelled/i);
    let timer;
    try {
      const observed = new Promise((resolve, reject) => {
        second.status.on('access', state => {
          if (!state.armed) resolve(state);
        });
        timer = setTimeout(() => reject(new Error('Second window did not observe revocation')), 5000);
      });
      first.commands.get('karma.disarmNative')();
      await observed;
      await rejected;
      assert(interrupted, 'Revocation must cancel workspace work in the other window');
      assert(!second.controller.status().armed);
    } finally {
      clearTimeout(timer);
    }

    authorization.write(savedGrant);
    const {workspace} = second.vscode;
    workspace.isTrusted = false;
    assert.throws(() => second.authorization.scope(), /trust/i);
    assert(!second.controller.status().armed);
    workspace.isTrusted = true;
    assert(second.controller.status().armed);

    workspace.workspaceFolders.push(folder(extra));
    assert(!second.controller.status().armed, 'An added folder requires a new workspace grant');
    workspace.workspaceFolders.pop();
    workspace.workspaceFile = uri(path.join(directory, 'project.code-workspace'));
    assert(!second.controller.status().armed, 'A different workspace identity cannot reuse this grant');
    delete workspace.workspaceFile;
    assert(second.controller.status().armed);

    authorization.write({...savedGrant, profile: 'unknown'});
    assert(!second.controller.status().armed);
    authorization.write({scope: savedGrant.scope, profile: 'read_only'});
    assert(!second.controller.status().armed, 'A grant without a generation is invalid');
    const permissionDirectory = path.join(storage, 'workspace-permissions');
    const [record] = fs.readdirSync(permissionDirectory);
    fs.writeFileSync(path.join(permissionDirectory, record), '{malformed');
    assert.equal(authorization.read(), undefined, 'A malformed file carries no grant');
    assert(!second.controller.status().armed);

    authorization.write(savedGrant);
    fs.renameSync(root, path.join(directory, 'original-project'));
    fs.mkdirSync(root);
    assert.notEqual(authorization.scope(), savedGrant.scope);
    assert(!second.controller.status().armed, 'Replacing a directory at the same path requires a new grant');
    // A watcher can remain open but emit nothing. Exercise revocation through
    // the independent reconciliation clock, with no machine-speed deadline.
    const ticks = new Set();
    const silentWatch = () => Object.assign(new EventEmitter(), {close() {}});
    const options = {watch: silentWatch,
      setInterval(callback, period) {assert.equal(period, 500); ticks.add(callback); return callback;},
      clearInterval(callback) {ticks.delete(callback);}};
    const third = windowFor(root, storage, options);
    const fresh = {id: crypto.randomUUID(), profile: 'workspace_agent', scope: third.authorization.scope()};
    third.authorization.write(fresh);
    assert(third.controller.status().armed);
    let cancelled = false;
    const pending = third.controller.withWorkspaceAccess(undefined, token => new Promise(resolve => {
      token.onCancellationRequested(() => {cancelled = true; resolve();});
    }));
    const rejection = assert.rejects(pending, /permission changed|cancelled/i);
    third.authorization.write({id: crypto.randomUUID()});
    for (const tick of [...ticks]) tick();
    await rejection;
    assert(cancelled, 'A silent watcher must not retain an in-flight workspace grant');
    assert(!third.controller.status().armed);
    third.controller.dispose();
    assert.equal(ticks.size, 0, 'Disposal must stop every reconciliation timer');
    console.log('Workspace permission storage passed scope, revocation, cancellation and malformed-record checks.');
  } finally {
    first.controller.dispose();
    second.controller.dispose();
    fs.rmSync(directory, {force: true, recursive: true});
  }
}

check().catch(error => {
  console.error(error);
  process.exitCode = 1;
});

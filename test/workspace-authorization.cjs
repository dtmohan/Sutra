'use strict';

// Cross-layer regression: native chat -> private bridge -> source, workflow and
// budget receipts. Only VS Code UI and inference responses are simulated.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const {pathToFileURL} = require('node:url');
const {install} = require('../native_provider');
const {createStore} = require('../workspace_authorization');

const python = process.platform === 'win32' ? 'python' : 'python3';
const runner = path.join(__dirname, 'workspace-authorization-bridge.py');
const models = {low: 'small', mid: 'medium', high: 'large'};
const action = 'karma_native_action';
const noop = () => {};
const disposable = () => ({dispose: noop});
class EventEmitter { event = disposable; fire() {} dispose() {} }
class Text { constructor(value) { this.value = value; } }
class Call { constructor(callId, name, input) { Object.assign(this, {callId, name, input}); } }
class Result { constructor(callId, content) { Object.assign(this, {callId, content}); } }
class Cancellation {
  constructor() {
    const listeners = new Set();
    this.token = {isCancellationRequested: false, onCancellationRequested: listener => {
      listeners.add(listener);
      return {dispose: () => listeners.delete(listener)};
    }};
    this.cancel = () => {
      this.token.isCancellationRequested = true;
      for (const listener of listeners) listener();
    };
  }
  dispose() {}
}
function jsonFile(filename) {
  return fs.existsSync(filename) ? JSON.parse(fs.readFileSync(filename, 'utf8')) : {};
}
function project(base, name) {
  const root = path.join(base, name);
  const workspace = path.join(root, 'repo');
  const storage = path.join(root, 'ledger');
  const profile = path.join(root, 'profile');
  fs.mkdirSync(path.join(workspace, 'src'), {recursive: true});
  fs.mkdirSync(profile);
  const run = (args, input) => JSON.parse(execFileSync(python, ['-B', runner, ...args], {
    input: JSON.stringify(input), encoding: 'utf8', maxBuffer: 8 * 1024 * 1024, timeout: 30000,
  }));
  const bridge = request => run(['--workspace', workspace, '--storage', storage], request).data;
  const snapshot = () => run(['snapshot', storage], {});
  return {workspace, storage, profile, bridge, snapshot};
}
function host(fixture, buttons = {}) {
  const {workspace, profile, bridge} = fixture;
  const prompts = [];
  const requests = [];
  const commands = new Map();
  const subscriptions = [];
  let provider, tool;
  const vscode = {
    EventEmitter, CancellationTokenSource: Cancellation, LanguageModelTextPart: Text,
    LanguageModelToolCallPart: Call, LanguageModelToolResultPart: Result,
    LanguageModelToolResult: class { constructor(content) { this.content = content; } },
    LanguageModelChatMessageRole: {User: 1, Assistant: 2}, LanguageModelChatToolMode: {Auto: 1, Required: 2},
    workspace: {
      isTrusted: true, workspaceFolders: [{uri: {fsPath: workspace, scheme: 'file', toString: () => pathToFileURL(workspace).href}}],
      getConfiguration: () => ({get: (_key, fallback) => fallback}),
    },
    window: {
      showWarningMessage: async (message, _options, ...choices) => {
        prompts.push({message, choices});
        return choices.find(choice => buttons[choice]);
      },
      showInformationMessage: async () => undefined,
    },
    commands: {
      registerCommand: (name, callback) => { commands.set(name, callback); return disposable(); },
      executeCommand: async (name, ...args) => commands.get(name)?.(...args),
    },
    lm: {
      registerLanguageModelChatProvider: (_name, handler) => { provider = handler; return disposable(); },
      registerTool: (_name, handler) => { tool = handler; return disposable(); },
    },
  };
  const memento = path.join(profile, 'workspace-state.json');
  const context = {
    subscriptions, globalStorageUri: {fsPath: profile}, workspaceState: {
      get: key => jsonFile(memento)[key],
      update: async (key, value) => {
        const state = jsonFile(memento);
        state[key] = value;
        fs.writeFileSync(memento, JSON.stringify(state));
      },
    },
  };
  const controller = install(vscode, context, {
    workspace: () => workspace, authorization: createStore(vscode, context),
    isBusy: () => false, modelSettings: () => models, configureModels: async () => models,
    onStatus: noop, publishReview: noop, showDiff: async () => {},
    request: async request => {
      requests.push(structuredClone(request));
      try { return bridge(request); }
      catch (error) {
        if (error.stdout) throw new Error(JSON.parse(error.stdout).error);
        throw error;
      }
    },
  });
  const token = new Cancellation().token;
  async function send(prompt, tier = 'low') {
    const parts = [];
    await provider.provideLanguageModelChatResponse({id: 'karma-' + tier},
      [{role: 1, content: [new Text(prompt)]}], {tools: [{name: action}], modelOptions: {_conversationId: 'review-chat'}},
      {report: part => parts.push(part)}, token);
    return parts;
  }
  function close() {
    controller.dispose();
    for (const subscription of subscriptions.reverse()) subscription.dispose();
  }
  return {controller, requests, prompts, tool, send, close, token, command: vscode.commands.executeCommand};
}
function lastFlow(snapshot) { return snapshot.flows.at(-1); }
function sameTask(before, after) {
  assert.equal(after.session, before.session);
  assert.deepEqual(after.contract, before.contract);
  assert.deepEqual(after.budget.base_limits, before.budget.base_limits);
  assert.equal(lastFlow(after).flow_id, lastFlow(before).flow_id);
  assert.equal(lastFlow(after).conversation_id, lastFlow(before).conversation_id);
  assert.deepEqual(lastFlow(after).binding, lastFlow(before).binding);
  assert(after.valid, 'Ledger and invocation receipts must verify');
}
function charged(before, after) {
  for (const [key, used] of Object.entries(before.budget.used)) {
    assert(after.budget.used[key] >= used, 'Spent ' + key + ' was refunded');
  }
}
const text = parts => parts.filter(part => part instanceof Text).map(part => part.value).join('\n');

async function reviewJourney(base) {
  const fixture = project(base, 'review');
  for (let i = 0; i < 4; i++) {
    fs.writeFileSync(path.join(fixture.workspace, 'src', `part${i}.py`),
      `def part${i}(value):\n    return max(0, value) * 2\n` + '# Paging fixture to spend actual source bytes.\n'.repeat(42));
  }
  fs.writeFileSync(path.join(fixture.workspace, 'private.py'), 'OUTSIDE_APPROVED_TASK_SCOPE = 1\n');
  fixture.bridge({op: 'auto_new', goal: 'Review project Cedar CED724 in src only.', roots: ['src'],
    review_source_kib: 10, review_tokens: 80000, review_calls: 20});
  const prompt = 'Review the high-level architecture of project Cedar CED724. Read src only. Keep the STP kernel unchanged.';
  let h = host(fixture, {'Read only': true});
  try {
    assert(!h.controller.status().armed);
    assert.match(text(await h.send(prompt)), /paused/i);
    const paused = fixture.snapshot();
    const pending = lastFlow(paused);
    assert.equal(pending.state, 'PAUSED');
    assert.equal(pending.stop.status, 'context_budget');
    assert(pending.execution_id);
    assert(paused.budget.used.source_bytes > 5000);
    assert.equal(paused.budget.used.calls, 0, 'No synthesis call fits the advertised low-tier context');
    assert.equal(h.prompts.length, 1);

    // Reopen the actual provider and permission store against the same disk
    // state. Starting the provider may not resume a saved request.
    h.close();
    h = host(fixture, {'Increase allocation': true});
    assert(h.controller.status().armed);
    assert.equal(h.requests.length, 0);
    assert.deepEqual(fixture.snapshot(), paused);
    const retry = text(await h.send('continue', 'high'));
    assert.match(retry, /Resuming the saved Sutra workflow/);
    assert.match(retry, /(Task budget|Orchestration) .*source_bytes/);
    assert.equal(h.prompts.length, 0, 'Reload changed neither the grant nor model authorization');
    const sourceStop = fixture.snapshot();
    sameTask(paused, sourceStop);
    assert.equal(lastFlow(sourceStop).execution_id, pending.execution_id);
    assert.equal(lastFlow(sourceStop).stop.status, 'task_budget');
    assert.deepEqual(sourceStop.budget, paused.budget);

    await h.command('karma.disarmNative');
    assert(!h.controller.status().armed);
    h.close();
    h = host(fixture);
    assert(!h.controller.status().armed, 'Revocation survives reload');
    const since = h.requests.length;
    assert.match(text(await h.send('continue', 'high')), /access was not granted/i);
    assert.equal(h.prompts.length, 1);
    assert(h.requests.slice(since).every(request => ['pending', 'inspect'].includes(request.event)),
      'Declining authorization must stop before source and inference work');
    assert.deepEqual(fixture.snapshot(), sourceStop);

    h.close();
    h = host(fixture, {'Read only': true, 'Increase allocation': true});
    await h.command('karma.armNative');
    await h.command('karma.increaseAllocation');
    const amended = fixture.snapshot();
    sameTask(paused, amended);
    assert.deepEqual(amended.budget.used, sourceStop.budget.used);
    assert.equal(amended.budget.allocation_version, 1);
    assert(amended.budget.limits.source_bytes > paused.budget.limits.source_bytes);
    assert.notEqual(lastFlow(amended).execution_id, pending.execution_id);
    assert.equal(amended.executions.find(node => node.id === pending.execution_id).state, 'DONE');
    assert.equal(amended.packets.length, 0, 'Allocation confirmation must never start inference');

    const response = text(await h.send('continue', 'high'));
    assert.match(response, /Entry/, response);
    const done = fixture.snapshot();
    sameTask(amended, done);
    charged(amended, done);
    assert.deepEqual(done.budget.limits, amended.budget.limits);
    assert.equal(done.budget.allocation_version, 1);
    assert.equal(lastFlow(done).state, 'DONE');
    assert.equal(done.executions.at(-1).state, 'DONE');
    assert(done.budget.used.calls > amended.budget.used.calls);
    const review = done.packets.find(({role}) => role === 'chat_reviewer');
    assert.equal(review?.model, 'large');
    assert.equal(review.packet.request, prompt);
    assert.deepEqual(review.packet.contract, done.contract);
    assert.match(JSON.stringify(review.packet.memory), /CED724/);
    assert(!JSON.stringify(review.packet).includes('OUTSIDE_APPROVED_TASK_SCOPE'));
    for (const name of ['edits_allowed', 'tests_allowed', 'network_allowed', 'dependencies_allowed']) {
      assert.equal(review.packet.contract.constraints[name], false);
    }
    assert.equal(h.prompts.length, 2, 'Only renewed workspace consent and explicit allocation were requested');
    console.log('Native review retained its contract, conversation, receipts and spending through context rejection, reload/model switch, source-budget stop, revocation and allocation recovery.');
  } finally { h.close(); }
}

async function actionJourney(base, apply = false) {
  const fixture = project(base, apply ? 'action-accepted' : 'action-revoked');
  const file = path.join(fixture.workspace, 'src', 'retry.py');
  const original = 'MAX_ATTEMPTS = 3\ndef retry():\n    for attempt in range(MAX_ATTEMPTS - 1):\n        pass\n';
  fs.writeFileSync(file, original);
  fixture.bridge({op: 'new', goal: 'Fix retry in src/retry.py.', constraints: {
    native_task: true, allowed_paths: ['src/retry.py'], network_allowed: false, dependencies_allowed: false,
    edits_allowed: true, tests_allowed: false, max_actions: 4, max_tools: 100,
    review_calls: 12, review_tokens: 100000, review_source_kib: 512, review_seconds: 1800, review_page_kib: 512,
  }});
  const h = host(fixture, {'Workspace agent': true, 'Apply edit': true});
  let reopened;
  try {
    const parts = await h.send('Fix retry in src/retry.py.');
    const call = parts.find(part => part instanceof Call);
    assert(call, text(parts));
    assert.equal(call.name, action);
    const proposed = fixture.snapshot();
    assert.equal(lastFlow(proposed).state, 'AWAITING_ACTION');
    if (apply) {
      fs.writeFileSync(path.join(fixture.storage, 'telemetry.json'), JSON.stringify({
        wall_time: Date.now() / 1000, thermal_state: 0, memory_pressure_raw: 1,
        free_bytes: 8 * 1024 ** 3, inactive_bytes: 0
      }));
      const applied = await h.tool.invoke({input: call.input}, h.token);
      assert.equal(JSON.parse(applied.content[0].value).status, 'applied');
      assert.notEqual(fs.readFileSync(file, 'utf8'), original);
      const executed = fixture.snapshot();
      assert.equal(lastFlow(executed).state, 'DONE');
      assert.deepEqual(executed.contract.constraints, proposed.contract.constraints);
      assert.equal(executed.contract.goal, proposed.contract.goal);
      assert.equal(executed.contract.implementation_present, true);
      assert.equal(executed.session, proposed.session);
      assert.deepEqual(lastFlow(executed).binding, lastFlow(proposed).binding);
      assert.equal(lastFlow(executed).conversation_id, lastFlow(proposed).conversation_id);
      assert(executed.valid);
      charged(proposed, executed);
      assert.equal(h.requests.filter(request => request.op === 'native_execute').length, 1);
      console.log('A fresh approved edit crossed the same bridge and changed only its scoped fixture.');
      return;
    }
    reopened = host(fixture);
    assert(reopened.controller.status().armed);
    await assert.rejects(reopened.tool.invoke({input: call.input}, reopened.token), /ticket/i);
    assert.equal(reopened.requests.length, 0, 'Restoring authorization may not revive an old action ticket');
    const requestsBeforeRevoke = h.requests.length;
    await reopened.command('karma.disarmNative');
    await assert.rejects(h.tool.invoke({input: call.input}, h.token), /authorization|permission|revoked|access|ticket/i);
    assert.equal(h.requests.length, requestsBeforeRevoke, 'Revocation in a second host reaches the original ticket before any execution request');
    assert.deepEqual(fixture.snapshot(), proposed);
    assert.equal(fs.readFileSync(file, 'utf8'), original);
    console.log('An admitted edit proposal remained in the ledger, but neither reload nor revocation let its old action ticket execute.');
  } finally { reopened?.close(); h.close(); }
}

(async () => {
  const base = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'sutra-native-auth-')));
  try {
    await reviewJourney(base);
    if (process.platform !== 'win32') {
      await actionJourney(base);
      await actionJourney(base, true);
    }
  } finally { fs.rmSync(base, {recursive: true, force: true}); }
})().catch(error => { console.error(error); process.exitCode = 1; });

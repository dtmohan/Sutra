'use strict';

// Cross-layer regression: native chat -> private bridge -> source, workflow and
// budget receipts. Only VS Code UI and inference responses are simulated.
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
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
function project(base, name, allocation = {enabled: false}) {
  const root = path.join(base, name);
  const workspace = path.join(root, 'repo');
  const storage = path.join(root, 'ledger');
  const profile = path.join(root, 'profile');
  fs.mkdirSync(path.join(workspace, 'src'), {recursive: true});
  fs.mkdirSync(profile);
  const run = (args, input) => JSON.parse(execFileSync(python, ['-B', runner, ...args], {
    input: JSON.stringify(input), encoding: 'utf8', maxBuffer: 8 * 1024 * 1024, timeout: 30000,
    env: {...process.env, SUTRA_WORKSPACE_ALLOCATION: JSON.stringify(allocation)},
  }));
  const bridge = request => run(['--workspace', workspace, '--storage', storage], request).data;
  const snapshot = () => run(['snapshot', storage], {});
  return {workspace, storage, profile, bridge, snapshot, setAllocation: policy => { allocation = policy; }};
}
function host(fixture, buttons = {}) {
  const {workspace, profile, bridge} = fixture;
  const prompts = [];
  const requests = [];
  const synthesisReads = [];
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
      // These fixtures seed the original workspace task slot before native chat opens.
      // Use its bridge route so recovery exercises the exact preseeded contract.
      const bridgeRequest = {...request};
      if (bridgeRequest.op === 'orchestrate') delete bridgeRequest.chat_key;
      requests.push(structuredClone(request));
      try {
        const result = bridge(bridgeRequest);
        if (result._fixture_synthesis_reads !== undefined) synthesisReads.push(result._fixture_synthesis_reads);
        return result;
      } catch (error) {
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
  return {controller, requests, prompts, synthesisReads, tool, send, close, token, command: vscode.commands.executeCommand};
}
function lastFlow(snapshot) { return snapshot.flows.findLast(flow => flow.binding) || snapshot.flows.at(-1); }
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

const reviewPrompt = 'Review the high-level architecture of project Cedar CED724. Read src only. Keep the STP kernel unchanged.';
const sourceMtime = 1700000000;

function reviewProject(base, name, sourceKiB, allocation, limits = {}) {
  const fixture = project(base, name, allocation);
  for (let i = 0; i < 4; i++) {
    const file = path.join(fixture.workspace, 'src', `part${i}.py`);
    fs.writeFileSync(file,
      `def part${i}(value):\n    return max(0, value) * 2\n` + '# Paging fixture to spend actual source bytes.\n'.repeat(42));
    fs.utimesSync(file, sourceMtime, sourceMtime);
  }
  fs.writeFileSync(path.join(fixture.workspace, 'private.py'), 'OUTSIDE_APPROVED_TASK_SCOPE = 1\n');
  fixture.bridge({op: 'auto_new', goal: 'Review project Cedar CED724 in src only.', roots: ['src'],
    review_source_kib: sourceKiB, review_tokens: 80000, review_calls: 20, ...limits});
  return fixture;
}

function assertReviewResult(done) {
  assert.equal(lastFlow(done).state, 'DONE');
  assert.equal(done.executions.at(-1).state, 'DONE');
  const review = done.packets.find(({role}) => role === 'chat_reviewer');
  assert.equal(review?.model, 'large');
  assert.equal(review.packet.request, reviewPrompt);
  assert.deepEqual(review.packet.contract, done.contract);
  assert.match(JSON.stringify(review.packet.memory), /CED724/);
  assert(!JSON.stringify(review.packet).includes('OUTSIDE_APPROVED_TASK_SCOPE'));
  for (const name of ['edits_allowed', 'tests_allowed', 'network_allowed', 'dependencies_allowed']) {
    assert.equal(review.packet.contract.constraints[name], false);
  }
  return review.packet;
}

function assertReviewed(before, done) {
  sameTask(before, done);
  charged(before, done);
  assert.deepEqual(done.budget.limits, before.budget.limits);
  assert.equal(done.budget.allocation_version, before.budget.allocation_version);
  assert.equal(lastFlow(done).execution_id, lastFlow(before).execution_id);
  assert(done.budget.used.calls > before.budget.used.calls);
  return assertReviewResult(done);
}

function changePreparedSource(fixture, paused) {
  const oldPage = paused.synthesis_receipts[0];
  const name = Object.keys(oldPage.versions)[0];
  const selected = path.join(fixture.workspace, name);
  const original = fs.readFileSync(selected, 'utf8');
  const changed = original.replace(' * 2', ' * 3');
  assert.notEqual(changed, original);
  assert.equal(Buffer.byteLength(changed), Buffer.byteLength(original));
  fs.writeFileSync(selected, changed);
  fs.utimesSync(selected, sourceMtime, sourceMtime);
  assert.equal(fs.statSync(selected).mtimeMs, sourceMtime * 1000);
  return {oldPage, name, changed};
}

async function authorizeSavedRequest(h, fixture, prompt, tier = 'low', asParts = false) {
  const before = fixture.snapshot(), prompts = h.prompts.length;
  const waiting = text(await h.send(prompt, tier));
  assert.match(waiting, /Workspace access needed/, waiting);
  assert.equal(h.prompts.length, prompts, 'A model response must not wait on a permission modal');
  assert(!h.controller.status().working, 'The response must finish before explicit workspace consent');
  const saved = fixture.snapshot();
  assert.deepEqual(saved.contract, before.contract);
  assert.deepEqual(saved.budget, before.budget, 'Permission guidance cannot spend workspace task capacity');
  assert.deepEqual(saved.source_events, before.source_events, 'No source work may precede consent');
  assert.deepEqual(saved.synthesis_receipts, before.synthesis_receipts);
  const requestCount = h.requests.length;
  await h.command('karma.armNative');
  assert(h.controller.status().armed, 'The explicit Workspace Access choice must persist authorization');
  assert.equal(h.prompts.length, prompts + 1);
  assert.equal(h.requests.length, requestCount, 'Granting access alone cannot read source or dispatch inference');
  assert.deepEqual(fixture.snapshot(), saved, 'The explicit grant preserves the saved workflow and all spending');
  const continued = await h.send('continue', tier);
  return asParts ? continued : text(continued);
}

async function reviewJourney(base) {
  // This is the same narrow task in which re-reading a previously selected
  // source file used to trigger a second pause before the larger model ran.
  const fixture = reviewProject(base, 'review', 10);
  let h = host(fixture, {'Read only': true});
  try {
    assert(!h.controller.status().armed);
    assert.match(await authorizeSavedRequest(h, fixture, reviewPrompt), /paused/i);
    const preflight = fixture.snapshot();
    const pending = lastFlow(preflight);
    assert.equal(pending.state, 'PAUSED');
    assert.equal(pending.stop.status, 'context_budget');
    assert(pending.execution_id);
    assert.equal(preflight.budget.used.calls, 0);
    assert.deepEqual(h.synthesisReads, [0], 'An impossible context must stop before reading source for synthesis');
    assert.deepEqual(preflight.synthesis_receipts, [], 'The failed preflight cannot spend any synthesis source budget');
    assert(!preflight.source_events.includes('REVIEW_SOURCE_PREPARED'));
    assert.equal(h.prompts.length, 1);

    // Continue in a new host process first with a model that can carry the
    // mandatory context but cannot carry that context and the actual evidence.
    h.close();
    h = host(fixture);
    assert(h.controller.status().armed);
    assert.equal(h.requests.length, 0);
    assert.deepEqual(fixture.snapshot(), preflight);
    const attempt = text(await h.send('continue', 'mid'));
    assert.match(attempt, /Resuming the saved Sutra workflow/);
    assert.match(attempt, /paused/i);
    const evidenceStop = fixture.snapshot();
    sameTask(preflight, evidenceStop);
    assert.equal(lastFlow(evidenceStop).execution_id, pending.execution_id);
    assert.equal(lastFlow(evidenceStop).stop.status, 'context_budget');
    assert.equal(evidenceStop.budget.used.calls, 0);
    assert(evidenceStop.budget.used.source_bytes > preflight.budget.used.source_bytes, lastFlow(evidenceStop).stop.error);
    assert(evidenceStop.synthesis_receipts.length > 0, 'The second stop must occur after real source work');
    assert(evidenceStop.source_events.includes('REVIEW_SOURCE_PREPARED'));
    assert.equal(h.prompts.length, 0, 'Reload changed neither the grant nor model authorization');

    await h.command('karma.disarmNative');
    assert(!h.controller.status().armed);
    h.close();
    h = host(fixture);
    assert(!h.controller.status().armed, 'Revocation survives reload');
    const since = h.requests.length;
    assert.match(text(await h.send('continue', 'high')), /Workspace access needed/i);
    assert.equal(h.prompts.length, 0, 'Revoked access defers consent until the response has ended');
    await h.command('karma.armNative');
    assert(!h.controller.status().armed);
    assert.equal(h.prompts.length, 1);
    assert(h.requests.slice(since).every(request => ['pending', 'inspect'].includes(request.event)),
      'Declining authorization must stop before source and inference work');
    assert.deepEqual(fixture.snapshot(), evidenceStop);

    h.close();
    h = host(fixture, {'Read only': true});
    await h.command('karma.armNative');
    const response = text(await h.send('continue', 'high'));
    assert.match(response, /Entry/, response);
    const done = fixture.snapshot();
    const packet = assertReviewed(evidenceStop, done);
    assert.equal(done.budget.used.source_bytes, evidenceStop.budget.used.source_bytes, 'Model switch must not charge a duplicate source read');
    assert.equal(done.budget.used.operations, evidenceStop.budget.used.operations);
    assert.deepEqual(done.synthesis_receipts, evidenceStop.synthesis_receipts);
    assert(done.source_events.includes('REVIEW_SOURCE_REUSED'));
    assert(h.synthesisReads.some(reads => reads > 0), 'Reusing an analysis receipt still checks fresh source bytes');
    assert.deepEqual(packet.snapshot_receipts, evidenceStop.synthesis_receipts.map(({id}) => id));
    assert.equal(h.prompts.length, 1, 'Only renewed workspace consent was requested');
    console.log('One saved review passed mandatory context preflight, receipt reuse across model switches and reloads, and revocation with its original task and source budget.');
  } finally { h.close(); }
}

async function changedSourceJourney(base) {
  const fixture = reviewProject(base, 'changed-source', 36);
  let h = host(fixture, {'Read only': true});
  try {
    assert.match(await authorizeSavedRequest(h, fixture, reviewPrompt, 'mid'), /paused/i);
    const paused = fixture.snapshot();
    assert.equal(lastFlow(paused).stop.status, 'context_budget');
    assert(paused.synthesis_receipts.length > 0);
    assert.equal(paused.budget.used.calls, 0);

    const {oldPage, name, changed} = changePreparedSource(fixture, paused);

    h.close();
    h = host(fixture);
    const response = text(await h.send('continue', 'high'));
    assert.match(response, /Entry/, response);
    const done = fixture.snapshot();
    const packet = assertReviewed(paused, done);
    assert(done.budget.used.source_bytes > paused.budget.used.source_bytes);
    const byId = new Map(done.synthesis_receipts.map(receipt => [receipt.id, receipt]));
    assert(!packet.snapshot_receipts.includes(oldPage.id), 'A stale source receipt cannot be included in the new packet');
    const current = packet.snapshot_receipts.map(id => byId.get(id)?.versions[name]).find(Boolean);
    assert(current, 'The retry must account for the changed source');
    assert.notEqual(current, oldPage.versions[name]);
    assert.equal(current, crypto.createHash('sha256').update(changed).digest('hex'));
    assert.equal(h.prompts.length, 0, 'A source change must not grant new task scope');
    console.log('A changed file invalidated its source receipt and the same saved review charged fresh source before using the new version.');
  } finally { h.close(); }
}

async function automaticAllocationJourney(base) {
  const sourceLimit = 10 * 1024;
  const workspaceLimit = 14 * 1024;
  const fixture = reviewProject(base, 'automatic-allocation', sourceLimit / 1024, {source_bytes: sourceLimit});
  let h = host(fixture, {'Read only': true});
  try {
    assert.match(await authorizeSavedRequest(h, fixture, reviewPrompt, 'mid'), /paused/i);
    const original = fixture.snapshot();
    assert.equal(lastFlow(original).stop.status, 'context_budget');
    assert(original.synthesis_receipts.length > 0);
    assert.deepEqual(original.amendments, [], 'A model context stop grants no extra source capacity');
    const {oldPage, name, changed} = changePreparedSource(fixture, original);

    h.close();
    h = host(fixture);
    assert.match(text(await h.send('continue', 'high')), /paused/i);
    const atCap = fixture.snapshot();
    sameTask(original, atCap);
    assert.equal(lastFlow(atCap).stop.status, 'task_budget');
    assert.match(lastFlow(atCap).stop.error, /source_bytes/);
    assert.equal(lastFlow(atCap).execution_id, lastFlow(original).execution_id);
    assert.deepEqual(atCap.budget, original.budget, 'The workspace ceiling denies new work and grants');
    assert.deepEqual(atCap.amendments, []);

    fixture.setAllocation({source_bytes: workspaceLimit});
    await h.send('Hello.', 'high');
    const greeted = fixture.snapshot();
    assert.equal(greeted.flows.at(-1).state, 'DONE');
    assert.equal(greeted.flows.at(-1).binding, null);
    assert.deepEqual(lastFlow(greeted), lastFlow(atCap));
    assert.deepEqual(greeted.budget, atCap.budget);
    assert.deepEqual(greeted.amendments, []);

    await h.command('karma.disarmNative');
    h.close();
    h = host(fixture);
    assert.match(text(await h.send('continue', 'high')), /Workspace access needed/i);
    assert.equal(h.prompts.length, 0);
    await h.command('karma.armNative');
    assert(!h.controller.status().armed);
    const revoked = fixture.snapshot();
    assert.deepEqual(revoked, greeted, 'An available reserve cannot authorize a revoked workspace');

    h.close();
    h = host(fixture, {'Read only': true});
    await h.command('karma.armNative');
    const response = text(await h.send('continue', 'high'));
    assert.match(response, /Entry/, response);
    const done = fixture.snapshot();
    sameTask(atCap, done);
    charged(atCap, done);
    const packet = assertReviewResult(done);
    assert.equal(done.budget.allocation_version, 1);
    assert.equal(done.amendments.length, 1);
    const [amendment] = done.amendments;
    assert.equal(amendment.authority, 'host_workspace_policy');
    assert.deepEqual(amendment.proposal.used, atCap.budget.used);
    assert.deepEqual(Object.keys(amendment.proposal.additions), ['source_bytes']);
    assert(amendment.proposal.additions.source_bytes > 0);
    assert.deepEqual(done.budget.limits, {...atCap.budget.limits, source_bytes: amendment.proposal.after.source_bytes});
    assert(done.budget.limits.source_bytes <= workspaceLimit);
    assert(done.budget.used.source_bytes > atCap.budget.used.source_bytes);
    assert(done.budget.used.source_bytes <= done.budget.limits.source_bytes);
    assert(done.budget.used.calls > atCap.budget.used.calls);
    const oldExecution = atCap.executions.find(({id}) => id === lastFlow(atCap).execution_id);
    const renewed = done.executions.at(-1);
    assert.equal(done.executions.length, atCap.executions.length + 1);
    assert.notEqual(renewed.id, oldExecution.id);
    assert.equal(renewed.previous, oldExecution.id);
    assert.equal(done.executions.find(({id}) => id === oldExecution.id).state, 'DONE');
    assert.deepEqual(renewed.permissions, oldExecution.permissions);
    assert.equal(lastFlow(done).execution_id, renewed.id);
    assert(!packet.snapshot_receipts.includes(oldPage.id));
    const newPage = done.synthesis_receipts.find(({id, versions}) => packet.snapshot_receipts.includes(id) && versions[name]);
    assert.equal(newPage?.versions[name], crypto.createHash('sha256').update(changed).digest('hex'));
    assert.equal(h.prompts.length, 1, 'Only renewed read authorization requires user interaction');
    assert(!h.requests.some(request => ['allocation_preview', 'allocation_grant'].includes(request.event)));
    console.log('The same review resumed on a larger model with one source-only automatic amendment, retained spending, and a linked execution. A hard ceiling, unrelated chat, and revoked access did not grant capacity.');
  } finally { h.close(); }
}

async function exhaustedBindingJourney(base) {
  // Task creation spends the one allowed repository operation on the catalog.
  // Binding then observes actual cumulative exhaustion before an execution exists.
  const fixture = project(base, 'automatic-binding', {operations: 1});
  fs.writeFileSync(path.join(fixture.workspace, 'src', 'main.py'), 'def entry():\n    return 724\n');
  fixture.bridge({op: 'auto_new', goal: 'Review project Cedar CED724 in src only.', roots: ['src'], max_tools: 1});
  const h = host(fixture, {'Read only': true});
  try {
    assert.match(await authorizeSavedRequest(h, fixture, reviewPrompt, 'mid'), /paused/i);
    const before = fixture.snapshot();
    const paused = lastFlow(before);
    assert.equal(paused.stop.stage, 'BINDING');
    assert.equal(paused.stop.status, 'task_budget');
    assert.equal(paused.execution_id, null);
    assert.equal(before.budget.used.operations, 1);
    assert.deepEqual(before.amendments, []);

    fixture.setAllocation({operations: 12});
    const transition = (event, flow) => fixture.bridge({op: 'orchestrate', event,
      flow_id: flow.flow_id, cursor: flow.cursor, armed: true, binding: flow.binding, models, tier: 'high'});
    let next = transition('resume', paused);
    for (let attempts = 0; next.state === 'BINDING' && attempts < 2; attempts++) next = transition('step', next);
    assert.equal(next.state, 'WORKSPACE');
    const after = fixture.snapshot();
    assert(next.execution_id);
    sameTask(before, after);
    assert.deepEqual(after.budget.used, before.budget.used);
    assert.equal(after.budget.allocation_version, 1);
    assert.deepEqual(after.amendments[0].proposal.used, before.budget.used);
    assert.equal(after.amendments[0].authority, 'host_workspace_policy');
    assert.deepEqual(Object.keys(after.amendments[0].proposal.additions), ['operations']);
    assert(after.budget.limits.operations <= 12);
    assert.equal(after.executions.length, 1);
    assert.equal(after.executions[0].previous, null);
    console.log('A saved review blocked before its first execution recovered inside its workspace ceiling without a budget reset.');
  } finally { h.close(); }
}

async function automaticReceiptReuseJourney(base) {
  const operationLimit = 8;
  const workspaceLimit = 12;
  const fixture = reviewProject(base, 'automatic-receipt-reuse', 10, {operations: workspaceLimit},
    {max_tools: operationLimit});
  let h = host(fixture, {'Read only': true});
  try {
    assert.match(await authorizeSavedRequest(h, fixture, reviewPrompt, 'mid'), /paused/i);
    const before = fixture.snapshot();
    const paused = lastFlow(before);
    assert.equal(paused.stop.status, 'context_budget');
    assert.equal(before.budget.used.operations, operationLimit);
    assert(before.synthesis_receipts.length > 0);
    assert.deepEqual(before.amendments, []);

    h.close();
    h = host(fixture);
    const reply = text(await h.send('continue', 'high'));
    assert.match(reply, /Entry/, reply);
    const done = fixture.snapshot();
    sameTask(before, done);
    charged(before, done);
    const packet = assertReviewResult(done);
    assert.equal(done.budget.allocation_version, 1);
    assert.equal(done.amendments.length, 1);
    const [amendment] = done.amendments;
    assert.equal(amendment.authority, 'host_workspace_policy');
    assert.deepEqual(amendment.proposal.used, before.budget.used);
    assert.deepEqual(Object.keys(amendment.proposal.additions), ['operations']);
    assert(done.budget.limits.operations <= workspaceLimit);
    const successor = done.executions.at(-1);
    assert.equal(successor.previous, paused.execution_id);
    assert.equal(lastFlow(done).execution_id, successor.id);
    assert.equal(packet.snapshot_owner, paused.execution_id);
    assert.deepEqual(packet.snapshot_receipts, before.synthesis_receipts.map(({id}) => id));
    assert.deepEqual(done.synthesis_receipts, before.synthesis_receipts);
    assert(done.source_events.includes('REVIEW_SOURCE_REUSED'));
    assert.equal(done.budget.used.operations, operationLimit);
    assert.equal(done.budget.used.source_bytes, before.budget.used.source_bytes);
    assert.equal(h.prompts.length, 0);
    console.log('An automatic operation grant opened a linked execution and verified the unchanged predecessor receipts without paying for source again.');
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
    const parts = await authorizeSavedRequest(h, fixture, 'Fix retry in src/retry.py.', 'low', true);
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
    await changedSourceJourney(base);
    await automaticAllocationJourney(base);
    await exhaustedBindingJourney(base);
    await automaticReceiptReuseJourney(base);
    if (process.platform !== 'win32') {
      await actionJourney(base);
      await actionJourney(base, true);
    }
  } finally { fs.rmSync(base, {recursive: true, force: true}); }
})().catch(error => { console.error(error); process.exitCode = 1; });

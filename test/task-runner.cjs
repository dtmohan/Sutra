'use strict';
// Actual TCP coordinator, production bridge/orchestration and temporary ledgers.
// Only model transport and hardware samples are deterministic test fixtures.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const {spawn, spawnSync} = require('node:child_process');
const {CoordinatorClient, privateJSON, rpc} = require('../coordinator_client');
const taskRunner = require('../task_runner');

const root = path.resolve(__dirname, '..');
const helper = path.join(__dirname, 'task-runner-worker.py');
const python = process.env.KARMA_SMOKE_PYTHON || 'python3';
const chatKey = 'a'.repeat(64);
const terminal = new Set(['completed', 'cancelled', 'interrupted', 'failed']);
const environment = {SUTRA_RAM_CACHE_MIB: '0', SUTRA_THINKING: 'on', SUTRA_CHILD_THINKING: 'off'};
const checks = [];
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

async function until(read, predicate, label, timeout = 30000) {
  const deadline = Date.now() + timeout;
  let value;
  while (Date.now() < deadline) {
    value = await read();
    if (predicate(value)) return value;
    await pause(15);
  }
  throw Error(label + ': ' + JSON.stringify(value));
}

function helperJSON(subject, mode) {
  const result = spawnSync(python, ['-B', helper, mode, '--workspace', subject.workspace, '--storage', subject.storage],
    {encoding: 'utf8', timeout: 20000, maxBuffer: 5_000_000});
  assert.equal(result.status, 0, result.stderr || result.error?.message);
  return JSON.parse(result.stdout);
}

function rawRequest(subject, request) {
  const result = spawnSync(python, ['-B', helper, '--request-stream', '--workspace', subject.workspace,
    '--storage', subject.storage], {input: JSON.stringify({request, environment}) + '\n',
    encoding: 'utf8', timeout: 20000, maxBuffer: 5_000_000});
  assert.equal(result.status, 0, result.stderr || result.error?.message);
  return JSON.parse(result.stdout);
}

function outcome(promise) {
  return promise.then(value => ({value}), error => ({error}));
}

function fixture() {
  const directory = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'sutra-task-runner-')));
  const workspace = path.join(directory, 'workspace'), storage = path.join(directory, 'store');
  fs.mkdirSync(workspace); fs.mkdirSync(storage);
  const executable = path.join(directory, 'controlled-python');
  fs.writeFileSync(executable, '#!/usr/bin/env python3\nimport runpy,sys\n' +
    'sys.argv=[sys.argv[0]]+sys.argv[3:]\nrunpy.run_path(' + JSON.stringify(helper) + ',run_name="__main__")\n',
  {mode: 0o700});
  const permissions = path.join(directory, 'workspace-permissions');
  fs.mkdirSync(permissions, {mode: 0o700});
  const permissionPath = path.join(permissions, 'a'.repeat(64) + '.json');
  fs.writeFileSync(permissionPath, JSON.stringify({workspace, readOnly: true, generation: 1}), {mode: 0o600});
  const authorization = {path: permissionPath,
    digest: crypto.createHash('sha256').update(fs.readFileSync(permissionPath)).digest('hex')};
  const subject = {directory, workspace, storage, authorization, permissionPath, clients: [], processes: []};
  subject.control = value => {
    const target = path.join(storage, 'runner-controls.json'), temporary = target + '.tmp';
    fs.writeFileSync(temporary, JSON.stringify(value), {mode: 0o600}); fs.renameSync(temporary, target);
  };
  subject.events = () => {
    const file = path.join(storage, 'runner-events.ndjson');
    if (!fs.existsSync(file)) return [];
    return fs.readFileSync(file, 'utf8').split('\n').filter(Boolean).flatMap(line => {
      try {return [JSON.parse(line)];} catch {return [];}
    });
  };
  subject.control({});
  subject.seed = helperJSON(subject, '--seed');
  subject.observe = () => helperJSON(subject, '--observe');
  subject.client = () => {
    const client = new CoordinatorClient({workspace, storage, python: executable, extensionPath: root, pollMs: 15,
      spawn(...args) {const child = spawn(...args); subject.processes.push(child); return child;}});
    subject.clients.push(client); return client;
  };
  subject.endpoint = () => privateJSON(path.join(storage, 'coordinator', 'endpoint.json'));
  subject.status = ticket => rpc(subject.endpoint(), 'status', {id: ticket.id, chat_key: ticket.chatKey});
  subject.close = async () => {
    for (const client of subject.clients) client.dispose();
    let endpoint;
    try {endpoint = subject.endpoint();} catch {}
    if (endpoint) {
      try {
        await rpc(endpoint, 'cancel_all');
        await until(() => rpc(endpoint, 'status'), state => !state.busy, 'Owned runner did not stop', 20000);
        await rpc(endpoint, 'shutdown');
        await until(() => {
          try {process.kill(endpoint.pid, 0); return false;} catch (error) {return error.code === 'ESRCH';}
        }, Boolean, 'Owned coordinator did not exit', 5000);
      } catch {
        try {process.kill(endpoint.pid, 'SIGTERM');} catch {}
      }
    }
    fs.rmSync(directory, {recursive: true, force: true});
  };
  return subject;
}

async function start(subject, client, request = subject.seed.request, options = {}) {
  const allocated = await client.allocate(chatKey);
  const ticket = {id: allocated.id, chatKey, wholeTask: true};
  const {requestEnvironment = environment, ...transportOptions} = options;
  const pending = outcome(client.send(request, requestEnvironment,
    {transport: ticket, authorization: subject.authorization, ...transportOptions}));
  await until(async () => {
    try {return await subject.status(ticket);}
    catch (error) {if (error.code === 'COORDINATOR_REQUEST_MISSING') return null; throw error;}
  }, Boolean, 'Allocated task was not submitted');
  return {ticket, pending};
}

function savedFlow(observation, id) {
  const flow = observation.flows.find(item => item.id === id);
  assert(flow, 'The same saved workflow must remain in the ledger');
  return flow;
}

function assertPrefix(before, after) {
  assert.deepEqual(after.receipts.slice(0, before.receipts.length), before.receipts,
    'Accepted or quarantined past receipts must never be rewritten or replayed');
}

async function assertKernelChain(subject, ticket, continuation) {
  const status = await subject.status(ticket), observation = subject.observe();
  const rows = observation.runner_decisions.filter(row => row.record.runner_request_id === ticket.id);
  assert(rows.length, 'Runner decisions must enter the existing audit chain');
  let previous = null;
  for (const row of rows) {
    const record = row.record;
    assert.equal(record.request_digest, status.digest);
    assert.equal(record.flow_id, subject.seed.flow.flow_id);
    assert.equal(record.flow_receipt, record.cursor);
    assert.deepEqual(record.previous_decision, previous);
    assert.equal(record.kernel.adapter, 'karma');
    assert.equal(record.kernel.profile, 'task-continuation-v1');
    assert.equal(record.kernel.grants_permissions, false);
    assert.equal(record.kernel.replenishes_budget, false);
    assert.equal(record.kernel.decision, record.disposition === 'continue' ? 'ALLOW' :
      ['wait', 'approval', 'evidence'].includes(record.disposition) ? 'BRIDGE' : 'FIN');
    previous = {version: 1, seq: row.seq, hash: row.hash};
  }
  if (continuation) {
    assert.equal(continuation.runner_request_id, ticket.id);
    const row = rows.find(row => row.seq === continuation.receipt.seq);
    assert(row);
    assert.equal(row.hash, continuation.receipt.hash);
    assert.deepEqual(continuation.kernel, row.record.kernel);
  }
  assert(observation.audit_valid);
  return rows;
}

async function stopReceipt(subject, ticket) {
  const status = await until(async () => {
    const row = await subject.status(ticket);
    if (row.stop_audit_error) throw Error(row.stop_audit_error);
    return row;
  }, row => !!row.stop_receipt, 'Stop did not receive its administrative kernel receipt');
  const rows = await assertKernelChain(subject, ticket);
  const last = rows.at(-1);
  assert.equal(last.record.runner_stopped, true);
  assert.equal(last.record.kernel.decision, 'FIN');
  assert.deepEqual(status.stop_receipt, {version: 1, seq: last.seq, hash: last.hash});
  assert(!status.stop_receipt_pending);
  return status;
}

function assertNoDomainChange(before, after) {
  const {runner_decisions: beforeDecisions, ...oldDomain} = before;
  const {runner_decisions: afterDecisions, ...newDomain} = after;
  assert.deepEqual(newDomain, oldDomain, 'Administrative kernel audit must not alter task work or spending');
}

async function detachedCompletionAndReplay() {
  const subject = fixture();
  try {
    subject.control({block_stage: 'subagent'});
    const client = subject.client(), {ticket, pending} = await start(subject, client);
    await until(subject.events, rows => rows.some(row => row.kind === 'model_started'), 'Child did not start');
    const endpoint = subject.endpoint();
    client.dispose();
    assert.equal((await pending).error.code, 'COORDINATOR_DETACHED');
    subject.control({});
    const completed = await until(() => subject.status(ticket), row => terminal.has(row.state), 'Detached task did not finish');
    assert.equal(completed.state, 'completed', completed.error);
    assert.equal(completed.response.data.state, 'DONE');
    assert(completed.response.data.data.report_markdown.includes('## Main components'));
    assert(completed.response.data.data.review_flow_diagram);
    assert.equal(completed.response.data.conversation.result.accepted_output.intent, 'workspace');
    const usage = completed.response.data.runner_usage;
    assert(usage.total_calls > 3);
    assert.equal(usage.total_calls, new Set(usage.calls.map(call => call.invocation_id)).size);
    assert(usage.calls.some(call => call.stage === 'conversation'));
    assert(usage.calls.some(call => call.stage === 'report_reviewer'));
    const decisions = await assertKernelChain(subject, ticket, completed.response.data.continuation);
    let permit;
    for (const event of subject.events()) {
      if (event.kind === 'bridge_started') {
        assert.equal(event.request.task_runner.request_id, ticket.id);
        assert.equal(event.request.task_runner.request_digest, completed.digest);
        if (permit) assert.deepEqual(event.request.continuation_receipt, permit);
        else assert.equal(event.request.continuation_receipt, undefined);
      } else if (event.kind === 'bridge_returned') permit = event.continuation.receipt;
    }
    const after = subject.observe(), flow = savedFlow(after, subject.seed.flow.flow_id);
    const initial = savedFlow(subject.seed.observation, flow.id);
    assert.equal(flow.state, 'DONE');
    assert.deepEqual(flow.contract, initial.contract);
    assert.deepEqual(flow.usage.limits, initial.usage.limits);
    assert(after.verified); assert(after.audit_valid);
    for (const event of subject.events().filter(row => row.kind === 'model_started')) {
      assert.equal(event.thinking, environment.SUTRA_THINKING);
      assert.equal(event.child_thinking, environment.SUTRA_CHILD_THINKING);
    }
    assertPrefix(subject.seed.observation, after);
    assert(subject.events().filter(row => row.kind === 'bridge_started' && row.request.event === 'step').length > 3);
    assert(flow.children.every(child => child.cursor.ordinal >= 2), 'Child checkpoints must survive the detached run');
    const reloaded = subject.client();
    const replay = await reloaded.resume(ticket);
    assert.equal(subject.endpoint().pid, endpoint.pid);
    assert.equal(replay.data.data.result.invocation_id, completed.response.data.data.result.invocation_id);
    const repeated = await reloaded.send(subject.seed.request, environment,
      {transport: ticket, authorization: subject.authorization});
    assert.equal(repeated.data.state, 'DONE');
    assert.deepEqual(subject.observe(), after, 'Attaching and repeated submission of one ticket must not dispatch again');
    const changed = {...subject.seed.request, tier: 'high'};
    await assert.rejects(reloaded.send(changed, environment,
      {transport: ticket, authorization: subject.authorization}), /changed|digest|rebind/i);
    await assert.rejects(reloaded.send(subject.seed.request, {...environment, SUTRA_THINKING: 'off'},
      {transport: ticket, authorization: subject.authorization}), /changed|digest|rebind/i);
    const final = completed.response.data, latest = final.continuation.receipt;
    const probe = {...subject.seed.request, event: 'runner_summary', cursor: final.cursor, binding: final.binding,
      task_runner: {version: 1, request_id: ticket.id, request_digest: completed.digest}, continuation_receipt: latest};
    const stale = {version: 1, seq: decisions[0].seq, hash: decisions[0].hash};
    for (const bad of [
      {...probe, continuation_receipt: stale},
      {...probe, continuation_receipt: {...latest, hash: '0'.repeat(64)}},
      {...probe, task_runner: {...probe.task_runner, request_id: crypto.randomBytes(16).toString('hex')}},
      {...probe, task_runner: {...probe.task_runner, request_digest: '0'.repeat(64)}},
    ]) {
      const rejected = rawRequest(subject, bad);
      assert.equal(rejected.ok, false);
      assert.match(rejected.error, /continuation|runner|receipt|digest/i);
    }
    assert.deepEqual(subject.observe(), after, 'Rejected stale, forged and cross-UID permits must not change paid work');
    await reloaded.ack(ticket);
    checks.push('A detached real saved review advances through child checkpoints and claim publication; replay retains one execution and aggregate usage');
  } finally {await subject.close();}
}

async function stopQueued() {
  const subject = fixture();
  try {
    subject.control({hold_bridge: true});
    const client = subject.client();
    const head = outcome(client.send({op: 'state'}, environment));
    await until(subject.events, rows => rows.some(row => row.kind === 'bridge_started'), 'Queue head did not start');
    const {ticket, pending} = await start(subject, client);
    await until(() => subject.status(ticket), row => row.state === 'queued', 'Whole task did not queue');
    await client.cancel(ticket);
    assert.equal((await pending).error.code, 'COORDINATOR_CANCELLED');
    subject.control({});
    assert.equal((await head).value.ok, true);
    await stopReceipt(subject, ticket);
    const after = subject.observe();
    assertNoDomainChange(subject.seed.observation, after);
    assert(!subject.events().some(row => row.kind === 'model_started'));
    checks.push('Stop before first dispatch cancels the queued task without source operations or spending');
  } finally {await subject.close();}
}

async function hardwareWaitAndFreshRecovery() {
  const subject = fixture();
  try {
    subject.control({hardware_stage: 'subagent', hardware_ready: false});
    const client = subject.client(), {ticket, pending} = await start(subject, client);
    await until(() => subject.status(ticket), row => row.state === 'waiting', 'Resource stop was not retained as a wait');
    await until(subject.events, rows => rows.filter(row => row.kind === 'bridge_started' &&
      row.request.event === 'continuation_probe').length >= 2, 'Runner did not refresh readiness');
    assert(!subject.events().some(row => row.kind === 'model_started'), 'Readiness polling must never retry inference');
    const waited = subject.observe(), flow = savedFlow(waited, subject.seed.flow.flow_id);
    const original = savedFlow(subject.seed.observation, flow.id);
    assert.deepEqual(flow.usage, original.usage);
    assert.deepEqual(flow.contract, original.contract);
    subject.control({hardware_stage: 'subagent', hardware_ready: true});
    const result = await pending;
    assert(!result.error, result.error?.message);
    assert.equal(result.value.data.state, 'DONE', JSON.stringify(result.value.data.stop || result.value.data.recovery));
    assert(subject.events().some(row => row.kind === 'bridge_started' && row.request.event === 'resume' &&
      typeof row.request.continuation_fingerprint === 'string'));
    const after = subject.observe();
    assert(after.verified); assertPrefix(waited, after);
    assert.deepEqual(savedFlow(after, flow.id).usage.limits, original.usage.limits);
    checks.push('External waiting uses fresh readiness probes with no inference; recovery resumes the same contract and consumed budget');
  } finally {await subject.close();}
}

async function busyWaitAndReasonChange() {
  const subject = fixture();
  try {
    subject.control({busy_stage: 'subagent', runtime_busy: true});
    const events = [], client = subject.client();
    const {ticket, pending} = await start(subject, client, subject.seed.request, {
      onActivity: event => events.push(event),
      requestEnvironment: {...environment, SUTRA_RUNTIME_DIR: path.join(subject.storage, 'runtime'), SUTRA_ACTIVITY: '1'},
    });
    const waiting = await until(() => subject.status(ticket), row => row.readiness?.checks >= 1 && row.state === 'waiting',
      'Busy runtime did not complete a readiness check');
    const notices = () => events.filter(event => event.kind === 'workflow' && event.progress);
    await until(notices, rows => rows.some(row => /inference slot is busy/.test(row.progress)), 'Busy notice was not replayed');
    assert.match(waiting.readiness.message, /Child review is waiting.*local inference slot is busy/);
    assert(waiting.readiness.next_check_at > waiting.readiness.since);
    assert.equal(notices().filter(row => row.disposition === 'wait').length, 1);
    assert.match(notices().find(row => row.disposition === 'wait').progress, /next readiness check is scheduled in about 1 s/);
    assert(!events.some(row => row.kind === 'start'));

    subject.control({busy_stage: 'subagent', runtime_busy: true, hardware_ready: false});
    const pressure = await until(() => subject.status(ticket), row => /memory pressure is warning/.test(row.readiness?.message),
      'A changed readiness blocker did not update its status');
    await until(notices, rows => rows.filter(row => row.disposition === 'wait').length === 2, 'Changed blocker was not displayed');
    assert.equal(pressure.readiness.since, waiting.readiness.since);
    assert(pressure.readiness.checks > waiting.readiness.checks);
    const waited = subject.observe(), flow = savedFlow(waited, subject.seed.flow.flow_id);
    const original = savedFlow(subject.seed.observation, flow.id);
    assert.deepEqual(flow.usage, original.usage);
    assert.deepEqual(flow.contract, original.contract);
    assert(!events.some(row => row.kind === 'start'));
    assert(!subject.events().some(row => row.kind === 'model_started'));

    subject.control({hardware_ready: true});
    const result = await pending;
    assert(!result.error, result.error?.message);
    assert.equal(result.value.data.state, 'DONE');
    assert.equal(notices().filter(row => row.disposition === 'wait').length, 2);
    const ready = events.findIndex(row => row.kind === 'workflow' && /Readiness check passed.*resuming the saved task/.test(row.progress));
    const firstCall = events.findIndex(row => row.kind === 'start');
    assert(ready >= 0 && firstCall > ready, 'Present successful readiness before the next model call');
    const receipts = await assertKernelChain(subject, ticket, result.value.data.continuation);
    const permission = receipts.find(row => row.record.next_event === 'resume');
    assert(permission?.record.readiness_hash, 'Successful readiness needs its bound proof in the existing chain');
    assert.equal(permission.record.readiness.ready, true);
    assertPrefix(waited, subject.observe());
    checks.push('Busy runtime and changed hardware blocker appear once each. Readiness is announced before inference and retains spending, scope and chained proof');
  } finally {await subject.close();}
}

async function stopWhileWaiting() {
  const subject = fixture();
  try {
    subject.control({hardware_stage: 'subagent', hardware_ready: false});
    const client = subject.client(), {ticket, pending} = await start(subject, client);
    await until(() => subject.status(ticket), row => row.state === 'waiting', 'Task did not wait');
    await client.cancel(ticket);
    assert.equal((await pending).error.code, 'COORDINATOR_CANCELLED');
    const stop = await stopReceipt(subject, ticket);
    const count = subject.events().filter(row => row.kind === 'bridge_started' && row.request.event !== 'runner_stop').length;
    subject.control({hardware_stage: 'subagent', hardware_ready: true});
    await pause(300);
    assert.equal(subject.events().filter(row => row.kind === 'bridge_started' && row.request.event !== 'runner_stop').length, count);
    assert(!subject.events().some(row => row.kind === 'model_started'));
    const after = subject.observe();
    assert.deepEqual(savedFlow(after, subject.seed.flow.flow_id).usage,
      savedFlow(subject.seed.observation, subject.seed.flow.flow_id).usage);
    const decisions = after.runner_decisions.filter(row => row.record.runner_request_id === ticket.id);
    const prior = decisions.at(-2), saved = savedFlow(after, subject.seed.flow.flow_id);
    assert(prior, 'The Stop FIN follows the previously issued wait permit');
    const revoked = rawRequest(subject, {...subject.seed.request, event: 'continuation_probe',
      cursor: saved.cursor, binding: saved.binding,
      task_runner: {version: 1, request_id: ticket.id, request_digest: stop.digest},
      continuation_receipt: {version: 1, seq: prior.seq, hash: prior.hash}});
    assert.equal(revoked.ok, false);
    assert.match(revoked.error, /stopped|revoked/i);
    assert.deepEqual(subject.observe(), after, 'Stop revokes the old UID permit before readiness or model execution');
    checks.push('Stop between readiness operations prevents the next probe or inference even after resources recover');
  } finally {await subject.close();}
}

async function restartSettledWaitingTask() {
  const subject = fixture();
  try {
    subject.control({hardware_stage: 'subagent', hardware_ready: false});
    const client = subject.client(), {ticket, pending} = await start(subject, client);
    await until(() => subject.status(ticket), row => row.state === 'waiting', 'Task did not reach a settled wait');
    const before = subject.observe(), original = savedFlow(before, subject.seed.flow.flow_id);
    const endpoint = subject.endpoint();
    client.dispose();
    assert.equal((await pending).error.code, 'COORDINATOR_DETACHED');
    // Kill only this temporary, idle-between-operations service. A running or
    // cancelling request is deliberately not treated as a settled checkpoint.
    process.kill(endpoint.pid, 'SIGTERM');
    await until(() => {
      try {process.kill(endpoint.pid, 0); return false;} catch (error) {return error.code === 'ESRCH';}
    }, Boolean, 'Old temporary coordinator did not exit', 5000);
    const restored = subject.client();
    await restored.ensure();
    assert.notEqual(subject.endpoint().instance_id, endpoint.instance_id);
    const waiting = await until(() => subject.status(ticket), row => row.state === 'waiting',
      'Settled waiting intent was not restored by its original ticket');
    assert.equal(waiting.id, ticket.id);
    assert.equal(waiting.workflow.flow_id, original.id);
    assert(!subject.events().some(row => row.kind === 'model_started'));
    const preserved = subject.observe(), saved = savedFlow(preserved, original.id);
    assert.equal(saved.cursor, original.cursor);
    assert.deepEqual(saved.contract, original.contract);
    assert.deepEqual(saved.usage, original.usage);
    assertPrefix(before, preserved);
    const resumed = outcome(restored.resume(ticket));
    subject.control({hardware_stage: 'subagent', hardware_ready: true});
    const result = await resumed;
    assert(!result.error, result.error?.message);
    assert.equal(result.value.data.state, 'DONE', JSON.stringify(result.value.data.stop || result.value.data.recovery));
    const after = subject.observe(), done = savedFlow(after, original.id);
    assert.deepEqual(done.contract, original.contract);
    assert.deepEqual(done.usage.limits, original.usage.limits);
    assert(after.verified); assertPrefix(before, after);
    checks.push('A settled waiting task survives coordinator process restart under the same runtime, preserving its original ticket, cursor and budget');
  } finally {await subject.close();}
}

async function unknownOutcomeProbeReturnsStoppedCheckpoint() {
  const subject = fixture();
  try {
    subject.control({hardware_stage: 'subagent', hardware_ready: false});
    const client = subject.client(), {ticket, pending} = await start(subject, client);
    await until(() => subject.status(ticket), row => row.state === 'waiting', 'Task did not wait before the unknown observation');
    const before = subject.observe(), original = savedFlow(before, subject.seed.flow.flow_id);
    subject.control({hardware_stage: 'subagent', hardware_ready: false, owned_unknown: true});
    const result = await pending;
    assert(!result.error, result.error?.message);
    assert.equal(result.value.data.state, 'PAUSED');
    assert.equal(result.value.data.continuation.disposition, 'stopped');
    assert.equal(result.value.data.continuation.next_event, null);
    assert.equal(result.value.data.continuation_probe.observation.blocker, 'unknown_outcome');
    assert(!result.value.data.data?.report_markdown);
    assert(!subject.events().some(row => row.kind === 'model_started'));
    const count = subject.events().filter(row => row.kind === 'bridge_started').length;
    subject.control({hardware_stage: 'subagent', hardware_ready: true});
    await pause(200);
    assert.equal(subject.events().filter(row => row.kind === 'bridge_started').length, count);
    const after = subject.observe(), stopped = savedFlow(after, original.id);
    assert.deepEqual(stopped.usage, original.usage);
    assert.deepEqual(stopped.contract, original.contract);
    assertPrefix(before, after); assert(after.verified);
    checks.push('A readiness probe discovering an unknown outcome returns its stopped checkpoint, without inference or stale waiting guidance');
  } finally {await subject.close();}
}

async function revocationBeforeNextStep() {
  const subject = fixture();
  try {
    subject.control({block_stage: 'subagent'});
    const client = subject.client(), {ticket, pending} = await start(subject, client);
    await until(subject.events, rows => rows.some(row => row.kind === 'model_started'), 'First admitted child did not start');
    fs.writeFileSync(subject.permissionPath, JSON.stringify({workspace: subject.workspace, revoked: true}), {mode: 0o600});
    subject.control({});
    const result = await pending;
    assert.equal(result.error.code, 'COORDINATOR_CANCELLED');
    await stopReceipt(subject, ticket);
    const after = subject.observe(), flow = savedFlow(after, subject.seed.flow.flow_id);
    assert.equal(subject.events().filter(row => row.kind === 'model_started').length, 1);
    assert.equal(flow.usage.used.calls, savedFlow(subject.seed.observation, flow.id).usage.used.calls + 1);
    assert(after.verified); assertPrefix(subject.seed.observation, after);
    assert.equal((await subject.status(ticket)).backend_completion_verified, false);
    checks.push('Workspace authorization is rechecked between steps; revocation lets only the already admitted call settle');
  } finally {await subject.close();}
}

async function invalidContinuationDoesNotDispatchAgain() {
  const subject = fixture();
  try {
    subject.control({corrupt_continuation: true});
    const client = subject.client(), {ticket, pending} = await start(subject, client);
    const result = await pending;
    assert.equal(result.error.code, 'COORDINATOR_FAILED');
    assert.match(result.error.message, /continuation checkpoint|binding|identity/i);
    const status = await subject.status(ticket);
    assert.equal(status.state, 'failed');
    assert.equal(status.host_completion_received, false);
    assert(!status.response, 'A failed continuation must not publish an old partial response as success');
    assert.equal(subject.events().filter(row => row.kind === 'model_started').length, 1);
    const after = subject.observe(), saved = savedFlow(after, subject.seed.flow.flow_id);
    assert.equal(saved.state, 'CHILDREN');
    assert.deepEqual(saved.contract, savedFlow(subject.seed.observation, saved.id).contract);
    assert(after.verified); assertPrefix(subject.seed.observation, after);
    checks.push('A mismatched backend continuation stops before another dispatch and retains the actual paid checkpoint without publishing stale success');
  } finally {await subject.close();}
}

async function interruptedWriterAndExplicitModelSwitch() {
  const subject = fixture();
  try {
    subject.control({block_stage: 'synthesis'});
    const client = subject.client(), {ticket, pending} = await start(subject, client);
    await until(subject.events, rows => rows.some(row => row.kind === 'model_started' && row.stage === 'synthesis'),
      'Writer did not start');
    await client.cancel(ticket);
    assert.equal((await pending).error.code, 'COORDINATOR_CANCELLED');
    await stopReceipt(subject, ticket);
    const stopped = subject.observe(), paused = savedFlow(stopped, subject.seed.flow.flow_id);
    assert.equal(paused.state, 'PAUSED');
    assert(stopped.invocations.some(row => row.role === 'chat_reviewer' && row.status === 'quarantined'));
    assert(stopped.verified);
    const calls = subject.events().filter(row => row.kind === 'model_started').length;
    await pause(150);
    assert.equal(subject.events().filter(row => row.kind === 'model_started').length, calls);
    subject.control({});
    const reloaded = subject.client();
    const request = {...subject.seed.request, event: 'resume', cursor: paused.cursor, binding: paused.binding, tier: 'high'};
    const next = await start(subject, reloaded, request);
    const result = await next.pending;
    assert(!result.error, result.error?.message);
    assert.equal(result.value.data.state, 'DONE', JSON.stringify(result.value.data.stop || result.value.data.recovery));
    const after = subject.observe(), done = savedFlow(after, paused.id);
    assert.equal(done.id, paused.id);
    assert.deepEqual(done.contract, paused.contract);
    assert.deepEqual(done.usage.limits, paused.usage.limits);
    assert(done.usage.used.tokens >= paused.usage.used.tokens, 'Interrupted spending must not be refunded');
    assert(done.usage.used.calls > paused.usage.used.calls);
    assert.deepEqual(done.children.map(child => child.node), paused.children.map(child => child.node));
    assertPrefix(stopped, after); assert(after.verified);
    assert(after.invocations.some(row => row.role === 'chat_reviewer' && row.model === 'fixture-large' && row.status === 'accepted'));
    const writer = subject.events().find(row => row.kind === 'model_started' && row.model === 'fixture-large' && row.parent_notes > 0);
    assert(writer, 'The resumed writer must receive the accepted child checkpoint context');
    assert.equal(writer.goal, paused.contract.goal);
    assert.equal(writer.constraints.network_allowed, false);
    assert.equal(writer.constraints.edits_allowed, false);
    assert(writer.memory_has_pin, 'The resumed model must receive the saved conversation requirement');
    const resumed = subject.events().filter(row => row.kind === 'bridge_started' && row.request.tier === 'high');
    assert(resumed.length > 1);
    for (const row of resumed) assert.deepEqual(row.request.models, subject.seed.request.models);
    checks.push('Stop during a writer preserves unknown charged work and child checkpoints; explicit High resume retains the same task and limits');
  } finally {await subject.close();}
}

(async () => {
  const checkpoint={flow_id:'a'.repeat(32),cursor:'b'.repeat(64),binding:{session:'saved'},state:'CHILDREN'};
  const plan={version:1,...checkpoint,disposition:'continue',next_event:'step',fingerprint:'c'.repeat(64),reason:'Runnable.',
    runner_request_id:'d'.repeat(32),receipt:{version:1,seq:1,hash:'e'.repeat(64)},
    kernel:{adapter:'karma',profile:'task-continuation-v1',decision:'ALLOW',grants_permissions:false,replenishes_budget:false}};
  assert.deepEqual(taskRunner.plan({...checkpoint,continuation:plan}),plan);
  assert.throws(()=>taskRunner.plan({...checkpoint,continuation:{...plan,binding:{session:'other'}}}));
  assert.throws(()=>taskRunner.plan({...checkpoint,continuation:{...plan,receipt:undefined}}));
  assert.throws(()=>taskRunner.plan({...checkpoint,continuation:{...plan,kernel:{...plan.kernel,decision:'FIN'}}}));
  assert.throws(()=>taskRunner.plan({...checkpoint,continuation:plan},{...plan,flow_id:'d'.repeat(32)}));
  const next=taskRunner.nextRequest({models:{low:'small'},tier:'low',confirmed:true,authorization:{kind:'user'},
    action:{kind:'edit'},proposal_id:'one-shot'},plan);
  assert.equal(next.confirmed,undefined);assert.equal(next.authorization,undefined);assert.equal(next.action,undefined);
  assert.equal(next.proposal_id,undefined);assert.deepEqual(next.models,{low:'small'});
  assert.deepEqual(next.continuation_receipt,plan.receipt);
  assert.equal(taskRunner.activity(checkpoint,plan).progress,'','Generic disposition text must not clutter chat');
  assert.equal(taskRunner.activity({...checkpoint,progress:'Reading files.'},plan).progress,'Reading files.');
  assert.match(taskRunner.activity(checkpoint,{...plan,disposition:'wait'}).progress,/fresh readiness check/);
  const mib=1024**2, remote={profile:'remote-nvidia-v1',provider:'remote_llama',blocker:'model_admission',
    gpu_allocation:{decision:'BRIDGE',available_bytes:1792*mib,system_reserve_bytes:1024*mib},
    host_allocation:{decision:'ALLOW',available_bytes:20*1024*mib,system_reserve_bytes:1024*mib},
    runtime:{plan:{estimated_increment_bytes:832*mib,estimated_host_increment_bytes:1024*mib}}};
  const remoteWait={...checkpoint,state:'PAUSED',stop:{resume_phase:'SYNTHESIS',measurement:remote,
    error:'Should not forward provider diagnostics <img src=x>'}};
  const waitPlan={...plan,disposition:'wait',wait_kind:'model_capacity'};
  const shortfall=taskRunner.activity(remoteWait,waitPlan,{delay:4000}).progress;
  assert.match(shortfall,/Report synthesis is waiting.*Remote GPU memory is short by 64 MiB/);
  assert.match(shortfall,/1\.75 GiB available, with 1\.81 GiB required including reserve.*in about 4 s/);
  assert(!shortfall.includes('host RAM')&&!shortfall.includes('<img'));
  const shifted={...remoteWait,continuation_probe:{observation:{blocking_measurement:{...remote,
    gpu_allocation:{...remote.gpu_allocation,decision:'ALLOW'},
    host_allocation:{...remote.host_allocation,decision:'BRIDGE',available_bytes:1536*mib}}}}};
  assert.match(taskRunner.waitObservation(shifted,waitPlan).message,/Remote host RAM is short by 0\.50 GiB/);
  assert.equal(taskRunner.waitObservation(shifted,waitPlan).condition,'remote-capacity:ram');
  const scenarios = [detachedCompletionAndReplay, stopQueued, hardwareWaitAndFreshRecovery, busyWaitAndReasonChange,
    stopWhileWaiting, restartSettledWaitingTask, unknownOutcomeProbeReturnsStoppedCheckpoint, revocationBeforeNextStep,
    invalidContinuationDoesNotDispatchAgain, interruptedWriterAndExplicitModelSwitch];
  const selected = new Set(process.argv.slice(2));
  for (const name of selected) assert(scenarios.some(run => run.name === name), 'Unknown runner scenario: ' + name);
  for (const run of scenarios) {
    if (selected.size && !selected.has(run.name)) continue;
    await run();
    process.stdout.write('PASS ' + checks.at(-1) + '\n');
  }
  process.stdout.write(JSON.stringify({passed: true, scenarios: checks.length, checks}) + '\n');
})().catch(error => {process.stderr.write(error.stack + '\n'); process.exitCode = 1;});

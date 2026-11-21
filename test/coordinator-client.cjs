'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const {CoordinatorClient, privateJSON, rpc} = require('../coordinator_client');
const root = path.resolve(__dirname, '..');
const checks = [];
const pause = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

async function until(read, predicate, message) {
  const deadline = Date.now() + 7000;
  let value;
  while (Date.now() < deadline) {
    value = await read();
    if (predicate(value)) return value;
    await pause(10);
  }
  throw Error(message + ': ' + JSON.stringify(value));
}

function fixture({controlled = false} = {}) {
  const directory = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'sutra-client-test-')));
  const workspace = path.join(directory, 'workspace'), storage = path.join(directory, 'store');
  fs.mkdirSync(workspace); fs.mkdirSync(storage);
  let python = process.env.KARMA_SMOKE_PYTHON || 'python3';
  if (controlled) {
    // A test-only Python executable accepts the production bridge arguments,
    // then runs a controlled fixture. No production injection option is added.
    python = path.join(directory, 'controlled-python');
    const worker = path.join(__dirname, 'coordinator-worker.py');
    fs.writeFileSync(python, '#!/usr/bin/env python3\nimport runpy,sys\n' +
      'sys.argv=[sys.argv[0]]+sys.argv[3:]\n' +
      'runpy.run_path(' + JSON.stringify(worker) + ',run_name="__main__")\n', {mode: 0o700});
  }
  const options = {workspace, storage, extensionPath: root, python, pollMs: 10};
  const subject = {directory, workspace, storage, options, clients: []};
  subject.client = () => {
    const client = new CoordinatorClient(options); subject.clients.push(client); return client;
  };
  subject.endpoint = () => privateJSON(path.join(storage, 'coordinator', 'endpoint.json'));
  subject.rows = () => {
    const file = path.join(storage, 'worker-dispatches.ndjson');
    if (!fs.existsSync(file)) return [];
    return fs.readFileSync(file, 'utf8').trim().split('\n').filter(Boolean).map(JSON.parse);
  };
  subject.close = async () => {
    for (const client of subject.clients) client.dispose();
    let endpoint;
    try {endpoint = subject.endpoint();} catch {}
    if (endpoint) {
      try {
        await rpc(endpoint, 'cancel_all');
        await until(() => rpc(endpoint, 'status'), value => !value.busy, 'Owned runner did not finish cancellation');
        await rpc(endpoint, 'shutdown');
      } catch {}
      await until(() => {
        try {process.kill(endpoint.pid, 0); return false;} catch (error) {return error.code === 'ESRCH';}
      }, Boolean, 'Owned coordinator process did not stop');
    }
    fs.rmSync(directory, {recursive: true, force: true});
  };
  return subject;
}

async function realBridge() {
  const subject = fixture();
  try {
    const first = subject.client();
    const empty = await first.send({op: 'state'}, {SUTRA_RAM_CACHE_MIB: '0'});
    assert.equal(empty.ok, true); assert.equal(empty.data.session, null);
    fs.writeFileSync(path.join(subject.workspace, 'a.py'), 'CEDAR_LIMIT = 724\n');
    const created = await first.send({op: 'new', goal: 'Preserve Cedar CED724 and its read scope.',
      constraints: {allowed_paths: ['a.py'], network_allowed: false, dependencies_allowed: false,
        edits_allowed: false, tests_allowed: false, timeout_ms: 250, max_attempts: 3, output_format: 'plain text'}},
    {SUTRA_RAM_CACHE_MIB: '0', SUTRA_THINKING: 'on'});
    assert.equal(created.ok, true);
    const endpoint = subject.endpoint();
    assert.deepEqual(fs.readdirSync(path.join(subject.storage, 'coordinator')).filter(name => /^startup-.*\.json$/.test(name)), []);
    checks.push('Successful bootstrap removes its private startup record instead of accumulating configuration files');
    const worker = created.host_memory.worker_pid;
    const authorityOnly = await first.send({op: 'state'}, {SUTRA_RAM_CACHE_MIB: '0'},
      {transport: {authority: {generation: 'retained-grant', profile: 'read_only'}}});
    assert.equal(authorityOnly.ok, true);
    const notifications = (await first.snapshot()).jobs;
    assert(notifications.length > 0);
    assert(notifications.every(job => job.acknowledged), 'Authority-only transport has no reconnect ID and must auto-acknowledge its result');
    checks.push('Authority-only transport metadata without a durable request ID still acknowledges its completed result');
    first.dispose();
    assert.doesNotThrow(() => process.kill(endpoint.pid, 0));
    assert.doesNotThrow(() => process.kill(worker, 0));
    const reloaded = subject.client();
    const restored = await reloaded.send({op: 'state'}, {SUTRA_RAM_CACHE_MIB: '0', SUTRA_THINKING: 'off'});
    assert.equal(subject.endpoint().pid, endpoint.pid);
    assert.equal(restored.host_memory.worker_pid, worker);
    assert.deepEqual(restored.data.contract, created.data.contract);
    assert.equal(restored.data.session, created.data.session);
    assert.equal(restored.data.contract.contract.constraints.network_allowed, false);
    checks.push('Actual client starts a detached runner and real bridge; UI disposal/recreation preserves task and permissions');
  } finally {await subject.close();}
}

async function detachedRequest() {
  const subject = fixture({controlled: true});
  try {
    const client = subject.client(), chatKey = 'a'.repeat(64);
    const allocated = await client.allocate(chatKey);
    const ticket = {id: allocated.id, chatKey};
    const pending = client.send({op: 'state', tag: 'ui-reload', delay_ms: 350},
      {SUTRA_THINKING: 'on'}, {transport: ticket});
    const rejected = assert.rejects(pending, error => error.code === 'COORDINATOR_DETACHED');
    await until(subject.rows, rows => rows.length === 1, 'Controlled client request did not start');
    const endpoint = subject.endpoint();
    const pythonPid = subject.rows()[0].pid;
    client.dispose();
    await rejected;
    assert.doesNotThrow(() => process.kill(pythonPid, 0));
    const reloaded = subject.client(), activity = [];
    const response = await reloaded.resume(ticket, {onActivity: event => activity.push(event)});
    assert.equal(response.ok, true); assert.equal(response.data.request.tag, 'ui-reload');
    assert.equal(response.data.environment.SUTRA_THINKING, 'on');
    assert.equal(subject.endpoint().pid, endpoint.pid);
    assert.equal(subject.rows().length, 1);
    assert(activity.some(event => event.kind === 'thinking'));
    await reloaded.ack(ticket);
    checks.push('An accepted request survives actual client disposal; a fresh client resumes its ID and activity without dispatching twice');

    let stopped = false;
    const displayToken = {get isCancellationRequested() {return stopped;}};
    const secondId = await reloaded.allocate(chatKey), displayTicket = {id: secondId.id, chatKey};
    const displaying = reloaded.send({op: 'state', tag: 'display-detach', delay_ms: 300},
      {SUTRA_THINKING: 'off'}, {transport: displayTicket, token: displayToken});
    const displayRejected = assert.rejects(displaying, error => error.code === 'COORDINATOR_DETACHED');
    await until(subject.rows, rows => rows.length === 2, 'Display trial did not start');
    stopped = true;
    await displayRejected;
    const retained = await reloaded.resume(displayTicket);
    assert.equal(retained.data.request.tag, 'display-detach');
    assert.equal(subject.rows().filter(row => row.request.tag === 'display-detach').length, 1);
    await reloaded.ack(displayTicket);
    checks.push('Display cancellation detaches from accepted work; subsequent reconnect retains the original environment and single execution');

    const cancelId = await reloaded.allocate(chatKey), cancelTicket = {id: cancelId.id, chatKey};
    const running = reloaded.send({op: 'state', tag: 'explicit-cancel', delay_ms: 1500}, {}, {transport: cancelTicket});
    const cancelled = assert.rejects(running, error => error.code === 'COORDINATOR_CANCELLED' && error.terminalAtAttach === false);
    await until(subject.rows, rows => rows.length === 3, 'Explicit cancellation trial did not start');
    await reloaded.cancel(cancelTicket);
    await cancelled;
    await assert.rejects(reloaded.resume(cancelTicket),error=>error.code==='COORDINATOR_CANCELLED'&&error.terminalAtAttach===true);
    assert.equal(subject.rows().filter(row => row.request.tag === 'explicit-cancel').length, 1);
    await reloaded.ack(cancelTicket);
    assert.equal((await reloaded.snapshot()).busy, false);
    checks.push('Explicit runner cancellation is distinct from display detachment and leaves the service available');
  } finally {await subject.close();}
}

async function concurrentClients() {
  const subject = fixture({controlled: true});
  try {
    const clients = [subject.client(), subject.client()];
    const ids = await Promise.all(clients.map(client => client.allocate('b'.repeat(64))));
    assert.notEqual(ids[0].id, ids[1].id);
    assert.equal(ids[0].instance_id, ids[1].instance_id);
    const endpoint = subject.endpoint();
    await Promise.all(clients.map(client => client.snapshot()));
    assert.equal(subject.endpoint().pid, endpoint.pid);
    checks.push('Concurrent UI clients bootstrap one coordinator and receive distinct request IDs in the same instance');
  } finally {await subject.close();}
}

async function emptyBootstrapLock() {
  const subject = fixture({controlled: true});
  try {
    const privateRoot = path.join(subject.storage, 'coordinator');
    fs.mkdirSync(privateRoot, {mode: 0o700});
    fs.mkdirSync(path.join(privateRoot, 'bootstrap.lock'), {mode: 0o700});
    const client = subject.client();
    const ticket = await client.allocate('c'.repeat(64));
    assert.match(ticket.id, /^[a-f0-9]{32}$/);
    assert(!fs.existsSync(path.join(privateRoot, 'bootstrap.lock')));
    assert.deepEqual(fs.readdirSync(privateRoot).filter(name => /^startup-.*\.json$/.test(name)), []);
    checks.push('A crash-created empty bootstrap lock recovers without an owner record or duplicate runner');
  } finally {await subject.close();}
}

async function main() {
  await realBridge();
  await detachedRequest();
  await concurrentClients();
  await emptyBootstrapLock();
  console.log(JSON.stringify({passed: true, checks}, null, 2));
}

main().catch(error => {console.error(error); process.exitCode = 1;});

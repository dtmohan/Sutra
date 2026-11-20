'use strict';
// Real local sockets/processes, controlled Python transport, and one real ledger.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const net = require('node:net');
const crypto = require('node:crypto');
const {spawn, spawnSync} = require('node:child_process');
const {EventEmitter} = require('node:events');
const {digestFor, Coordinator, readPrivate, atomicPrivate} = require('../coordinator_service');
const {rpc: clientRPC} = require('../coordinator_client');
const root = path.resolve(__dirname, '..');
const checks = [];
const pause = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
const terminal = new Set(['completed', 'cancelled', 'interrupted', 'failed']);

async function until(read, predicate, message, milliseconds = 7000) {
  const deadline = Date.now() + milliseconds;
  let value;
  while (Date.now() < deadline) {
    value = await read();
    if (predicate(value)) return value;
    await pause(10);
  }
  throw new Error(message + ': ' + JSON.stringify(value));
}

function exchange(subject, op, fields = {}, overrides = {}) {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection({host: '127.0.0.1', port: subject.endpoint.port});
    let buffer = '', settled = false;
    function finish(error, result) {
      if (settled) return;
      settled = true; socket.destroy();
      if (error) reject(error); else resolve(result);
    }
    socket.setTimeout(5000, () => finish(Error('Coordinator test RPC timed out')));
    socket.on('error', error => finish(error));
    socket.on('close', () => {if (!settled) finish(Error('Coordinator closed before a complete response'));});
    socket.on('connect', () => socket.write(JSON.stringify({
      version: 1, token: subject.endpoint.token, runtime_hash: subject.endpoint.runtime_hash,
      scope: subject.endpoint.scope, op, ...fields, ...overrides,
    }) + '\n'));
    socket.on('data', chunk => {
      buffer += chunk;
      if (buffer.length > 5_000_000) return finish(Error('Coordinator response is unbounded'));
      const newline = buffer.indexOf('\n');
      if (newline >= 0) {
        try {finish(null, JSON.parse(buffer.slice(0, newline)));}
        catch (error) {finish(error);}
      }
    });
  });
}

async function rpc(subject, op, fields = {}) {
  const reply = await exchange(subject, op, fields);
  assert.equal(reply.ok, true, reply.error);
  return reply.data;
}

function dispatches(subject) {
  const file = path.join(subject.storage, 'worker-dispatches.ndjson');
  if (!fs.existsSync(file)) return [];
  return fs.readFileSync(file, 'utf8').trim().split('\n').filter(Boolean).map(JSON.parse);
}

async function fixture({real = false, directory} = {}) {
  directory ||= fs.mkdtempSync(path.join(os.tmpdir(), 'sutra-coordinator-test-'));
  directory = fs.realpathSync(directory);
  const workspace = path.join(directory, 'workspace'), storage = path.join(directory, 'store');
  fs.mkdirSync(workspace, {recursive: true}); fs.mkdirSync(storage, {recursive: true});
  let extensionPath = root;
  if (!real) {
    extensionPath = path.join(directory, 'fixture-extension');
    fs.mkdirSync(path.join(extensionPath, 'harness'), {recursive: true});
    fs.copyFileSync(path.join(__dirname, 'coordinator-worker.py'), path.join(extensionPath, 'harness', 'vscode_bridge.py'));
  }
  const state = path.join(storage, 'coordinator');
  fs.mkdirSync(state, {mode: 0o700, recursive: true});
  const configPath = path.join(state, 'startup-test.json');
  const config = {workspace, storage, python: process.env.KARMA_SMOKE_PYTHON || 'python3',
    extensionPath, runtimeHash: crypto.createHash('sha256').update('coordinator-integration-test').digest('hex')};
  fs.writeFileSync(configPath, JSON.stringify(config), {mode: 0o600});
  const child = spawn(process.execPath, [path.join(root, 'coordinator_service.js'), '--config', configPath],
    {stdio: ['ignore', 'pipe', 'pipe'], env: {...process.env, ELECTRON_RUN_AS_NODE: '1'}});
  let errors = '';
  child.stdout.on('data', () => {});
  child.stderr.on('data', chunk => {errors = (errors + chunk).slice(-20000);});
  child.on('error', error => {errors += error.message;});
  const endpointPath = path.join(state, 'endpoint.json');
  const endpoint = await until(() => {
    if (child.exitCode !== null) throw Error('Coordinator startup failed: ' + errors);
    try {return JSON.parse(fs.readFileSync(endpointPath, 'utf8'));} catch {return null;}
  }, value => !!value && value.pid === child.pid, 'Coordinator did not become ready');
  const subject = {directory, workspace, storage, child, config, configPath, endpoint, endpointPath};
  subject.close = async () => {
    if (child.exitCode === null) {
      try {await rpc(subject, 'shutdown');} catch {}
      if (child.exitCode === null) child.kill('SIGTERM');
      await until(() => child.exitCode !== null || child.signalCode !== null,
        value => value, 'Owned test coordinator did not stop');
    }
    fs.rmSync(directory, {recursive: true, force: true});
  };
  return subject;
}

async function allocated(subject, request, environment = {}, chat_key = 'a'.repeat(64), authorization) {
  const allocation = await rpc(subject, 'allocate');
  const value = {id: allocation.id, digest: digestFor(request, environment, authorization), request, environment, chat_key,
    ...(authorization ? {authorization} : {})};
  await rpc(subject, 'submit', value);
  return value;
}

function status(subject, value) {
  return rpc(subject, 'status', {id: value.id, chat_key: value.chat_key});
}

async function finished(subject, value) {
  return until(() => status(subject, value), row => terminal.has(row.state), 'Request did not settle');
}

async function ack(subject, value) {
  return rpc(subject, 'ack', {id: value.id, chat_key: value.chat_key});
}

async function ownershipAndRecovery() {
  const subject = await fixture();
  try {
    const value = await allocated(subject, {op: 'state', tag: 'detached', delay_ms: 160},
      {SUTRA_THINKING: 'on', SUTRA_CHILD_THINKING: 'off'});
    await until(() => dispatches(subject), rows => rows.length === 1, 'Python did not start');
    const foreignSummary = await rpc(subject, 'status', {chat_key: 'b'.repeat(64)});
    assert.equal(foreignSummary.active, null, 'Aggregate status must not leak another chat request');
    assert.equal(foreignSummary.jobs.length, 0);
    // Every RPC has its own closed socket. Losing the UI connection does not
    // signal the Python child or turn a new attach into another invocation.
    const reconnected = await rpc(subject, 'submit', value);
    assert.equal(reconnected.id || reconnected.request_id, value.id);
    const result = await finished(subject, value);
    assert.equal(result.state, 'completed');
    assert.equal(result.response.data.request.tag, 'detached');
    assert.equal(result.response.data.environment.SUTRA_THINKING, 'on');
    assert.equal(dispatches(subject).length, 1);
    const replay = await rpc(subject, 'events', {id: value.id, chat_key: value.chat_key, after: 0});
    assert(replay.events.some(row => row.event.kind === 'thinking'));
    const cursor = replay.last_event_seq;
    const noDuplicates = await rpc(subject, 'events', {id: value.id, chat_key: value.chat_key, after: cursor});
    assert.equal(noDuplicates.events.length, 0);
    assert.equal((fs.statSync(subject.endpointPath).mode & 0o077), 0);
    checks.push('UI detach and repeated attach preserve one execution, its environment and ordered activity');

    const changedEnvironment = {...value.environment, SUTRA_THINKING: 'off'};
    const changed = await exchange(subject, 'submit', {...value, environment: changedEnvironment,
      digest: digestFor(value.request, changedEnvironment)});
    assert.equal(changed.ok, false);
    const changedModel = {...value.request, model: 'another-model'};
    assert.equal((await exchange(subject, 'submit', {...value, request: changedModel,
      digest: digestFor(changedModel, value.environment)})).ok, false);
    assert.equal(dispatches(subject).length, 1);
    checks.push('Existing request identity cannot be rebound to a new model or reasoning environment');

    const count = dispatches(subject).length;
    const malformedId = await rpc(subject, 'allocate');
    assert.equal((await exchange(subject, 'submit', {...value, id: malformedId.id, digest: '0'.repeat(64)})).ok, false);
    const tooLarge = {op: 'state', body: '\u03b1'.repeat(100001)};
    assert.equal((await exchange(subject, 'submit', {...value, id: malformedId.id,
      request: tooLarge, digest: digestFor(tooLarge, value.environment)})).ok, false);
    assert.equal((await exchange(subject, 'events', {id: value.id, chat_key: value.chat_key, after: cursor + 1})).ok, false);
    assert.equal(dispatches(subject).length, count);
    checks.push('Bad digests, UTF-8 byte overflow and invalid event cursors are rejected before host dispatch');

    for (const overrides of [{version: 2}, {version: undefined}, {version: '1'},
      {token: '0'.repeat(64)}, {scope: '0'.repeat(64)}, {runtime_hash: '0'.repeat(64)}]) {
      const rejected = await exchange(subject, 'status', {id: value.id, chat_key: value.chat_key}, overrides);
      assert.equal(rejected.ok, false);
      assert(!rejected.data, 'Rejected callers must not receive job details');
    }
    for (const op of ['status', 'events', 'cancel', 'ack']) {
      const rejected = await exchange(subject, op, {id: value.id, chat_key: 'b'.repeat(64)});
      assert.equal(rejected.ok, false, op + ' leaked across a chat boundary');
    }
    checks.push('Protocol version, token, runtime, workspace and per-chat bindings protect status, events, cancellation and results');
    await ack(subject, value);

    const cancelled = await allocated(subject, {op: 'state', tag: 'cancel', delay_ms: 1500});
    await until(() => dispatches(subject), rows => rows.length === 2, 'Cancelled Python call did not start');
    await rpc(subject, 'cancel', {id: cancelled.id, chat_key: cancelled.chat_key});
    const settled = await finished(subject, cancelled);
    assert.equal(settled.state, 'cancelled');
    assert.equal(settled.backend_completion_verified, false);
    const oldPid = dispatches(subject)[1].pid;
    assert.throws(() => process.kill(oldPid, 0), /ESRCH|not found/);
    await ack(subject, cancelled);
    const next = await allocated(subject, {op: 'state', tag: 'after-cancel'});
    const nextResult = await finished(subject, next);
    assert.equal(nextResult.state, 'completed');
    assert.notEqual(nextResult.response.data.worker_pid, oldPid);
    assert.equal(dispatches(subject).filter(row => row.request.tag === 'cancel').length, 1);
    await ack(subject, next);
    checks.push('Explicit cancellation settles only after child exit; the service remains available without replay');
  } finally {await subject.close();}
}

async function bounds() {
  const subject = await fixture();
  try {
    const first = await allocated(subject, {op: 'state', tag: 'queue-head', delay_ms: 1500});
    await until(() => dispatches(subject), rows => rows.length === 1, 'Queue head did not start');
    const values = [first];
    // The byte reservation can be tighter than the eight-record ceiling: every
    // admitted live request reserves room for its maximum result and activity.
    for (let i = 1; i < 3; ++i) values.push(await allocated(subject, {op: 'state', tag: 'queue-' + i}));
    const overflow = await rpc(subject, 'allocate');
    const request = {op: 'state', tag: 'overflow'};
    const refused = await exchange(subject, 'submit', {id: overflow.id, request, environment: {},
      digest: digestFor(request, {}), chat_key: first.chat_key});
    assert.equal(refused.ok, false);
    const queueTail = values[values.length - 1];
    assert.equal((await status(subject, queueTail)).state, 'queued');
    await rpc(subject, 'cancel', {id: queueTail.id, chat_key: queueTail.chat_key});
    assert.equal((await finished(subject, queueTail)).state, 'cancelled');
    assert(!dispatches(subject).some(row => row.request.tag === 'queue-2'));
    await rpc(subject, 'cancel', {id: first.id, chat_key: first.chat_key});
    for (const value of values) {await finished(subject, value); await ack(subject, value);}
    checks.push('Queue capacity is bounded; cancelling a queued request never starts its Python operation');

    // Unacknowledged results remain available when another submit is refused.
    const completed = [];
    for (let i = 0; i < 8; ++i) {
      const value = await allocated(subject, {op: 'state', tag: 'unacked-' + i});
      assert.equal((await finished(subject, value)).state, 'completed'); completed.push(value);
    }
    const extra = await rpc(subject, 'allocate');
    const extraRequest = {op: 'state', tag: 'unacked-overflow'};
    assert.equal((await exchange(subject, 'submit', {id: extra.id, request: extraRequest, environment: {},
      digest: digestFor(extraRequest, {}), chat_key: first.chat_key})).ok, false);
    assert.equal((await status(subject, completed[0])).response.data.request.tag, 'unacked-0');
    for (const value of completed) await ack(subject, value);
    const later = await allocated(subject, {op: 'state', tag: 'prune-acknowledged'});
    await finished(subject, later); await ack(subject, later);
    assert.equal((await exchange(subject, 'submit', completed[0])).ok, false);
    checks.push('Capacity pressure preserves unacknowledged output and rejects retired IDs rather than replaying them');

    const events = await allocated(subject, {op: 'state', tag: 'bounded-events', event_calls: 150});
    await finished(subject, events);
    const replay = await rpc(subject, 'events', {id: events.id, chat_key: events.chat_key, after: 0});
    assert(replay.dropped_before > 0, 'Activity replay should disclose evicted diagnostic events');
    assert(Buffer.byteLength(JSON.stringify(replay.events)) <= 600000);
    assert.equal((await status(subject, events)).state, 'completed');
    await ack(subject, events);
    checks.push('Diagnostic event replay has a byte bound and reports gaps without changing execution settlement');
  } finally {await subject.close();}
}

async function crashRecovery() {
  let subject = await fixture();
  const directory = subject.directory;
  try {
    const value = await allocated(subject, {op: 'state', tag: 'service-crash', delay_ms: 900});
    await until(() => dispatches(subject), rows => rows.length === 1, 'Crash trial did not start');
    const childPid = dispatches(subject)[0].pid;
    subject.child.kill('SIGKILL');
    await until(() => subject.child.signalCode, value => value === 'SIGKILL', 'Test daemon did not exit');
    subject = await fixture({directory});
    const retained = await status(subject, value);
    assert.equal(retained.state, 'interrupted');
    assert.equal(retained.backend_completion_verified, false);
    assert(!retained.response, 'A crash cannot invent the lost host response');
    const submitAgain = await rpc(subject, 'submit', value);
    assert.equal(submitAgain.state, 'interrupted');
    await until(() => {
      try {process.kill(childPid, 0); return false;} catch (error) {return error.code === 'ESRCH';}
    }, value => value, 'Old pipe worker did not exit after its owner disappeared');
    assert.equal(dispatches(subject).filter(row => row.request.tag === 'service-crash').length, 1);
    await ack(subject, value);
    const next = await allocated(subject, {op: 'state', tag: 'after-crash'});
    assert.equal((await finished(subject, next)).state, 'completed');
    await ack(subject, next);
    checks.push('Daemon death retains an interrupted identity; restart and reattach never replay the lost operation');
  } finally {await subject.close();}
}

async function missingTransportRecovery() {
  let subject = await fixture();
  const directory = subject.directory;
  try {
    const old = await allocated(subject,{op:'state',tag:'evicted-before-restart'});
    await finished(subject,old);await ack(subject,old);
    let retained;
    for (let index=0;index<8;index++) {
      retained = await allocated(subject,{op:'state',tag:'advance-cache-'+index});
      await finished(subject,retained);await ack(subject,retained);
    }
    assert.equal((await status(subject,old)).response_evicted,true);
    const count = dispatches(subject).length;
    await rpc(subject,'shutdown');
    await until(() => subject.child.exitCode !== null || subject.child.signalCode !== null,
      Boolean,'Missing-record trial did not stop its owned service');
    subject = await fixture({directory});
    await assert.rejects(clientRPC(subject.endpoint,'status',{id:old.id,chat_key:old.chat_key}),
      error => error.code === 'COORDINATOR_REQUEST_MISSING' && /unknown/i.test(error.message));
    const reserved = await rpc(subject,'allocate');
    await assert.rejects(clientRPC(subject.endpoint,'status',{id:reserved.id,chat_key:old.chat_key}),
      error => error.code === 'COORDINATOR_REQUEST_MISSING');
    const foreign = await exchange(subject,'status',{id:retained.id,chat_key:'b'.repeat(64)});
    assert.equal(foreign.ok,false);assert.notEqual(foreign.code,'COORDINATOR_REQUEST_MISSING');
    const unauthenticated = await exchange(subject,'status',{id:old.id,chat_key:old.chat_key},{token:'0'.repeat(64)});
    assert.equal(unauthenticated.ok,false);assert.notEqual(unauthenticated.code,'COORDINATOR_REQUEST_MISSING');
    await pause(20);
    assert.equal(dispatches(subject).length,count,'Missing transport recovery must not resubmit or replay an operation');
    checks.push('Missing old-instance and allocated-only records return a typed unknown-state error through the actual client without replay or weakening chat/auth failures');
  } finally {await subject.close();}
}

const seedLedger = String.raw`
import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(sys.argv[1])/'harness'))
from vscode_bridge import handle,dispatch
from invocation_gateway import InvocationLedger
from test_vscode_bridge import FakeModel
from review_budget import usage
workspace,storage=map(Path,sys.argv[2:4])
(workspace/'src').mkdir(exist_ok=True);(workspace/'tests').mkdir(exist_ok=True)
(workspace/'src/retry.py').write_text('BACKOFF_SECONDS = 0.375\n')
(workspace/'tests/test_retry.py').write_text('def test_pending():\n    pass\n')
contract={'op':'new','goal':'Preserve the Cedar retry review, CED724, and source evidence.',
 'constraints':{'allowed_paths':['src/retry.py','tests/test_retry.py'],
 'network_allowed':False,'dependencies_allowed':False,'edits_allowed':False,
 'tests_allowed':False,'native_task':True,'max_tools':1000,'review_calls':40,
 'review_tokens':250000,'review_source_kib':65536,'review_seconds':1800,
 'review_page_kib':512,'timeout_ms':250,'max_attempts':3,'output_format':'plain text'}}
handle(storage,workspace,contract)
s=InvocationLedger(storage/'state.sqlite')
try:
 meta=json.loads((storage/'workspace.json').read_text())
 out=dispatch(s,meta,workspace,storage,{'prompt':'Review this source.','model':'fixture-reviewer',
  'pages':['src/retry.py#1']},FakeModel())
 assert out['result']['admission']['admitted']
 (storage/'workspace.json').write_text(json.dumps(meta))
 sid=meta['active']
 before={'session':sid,'contract':s.current_contract(sid),'usage':usage(s,sid),
  'receipt_rows':s.db.execute('SELECT id,record_json FROM invocation_receipts ORDER BY seq').fetchall(),
  'checkpoint':out['result']['invocation_id']}
 print(json.dumps(before))
finally:s.close()
`;

const observeLedger = String.raw`
import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(sys.argv[1])/'harness'))
from invocation_gateway import InvocationLedger
from review_budget import usage
storage=Path(sys.argv[2]);s=InvocationLedger(storage/'state.sqlite')
try:
 sid=json.loads((storage/'workspace.json').read_text())['active']
 print(json.dumps({'session':sid,'contract':s.current_contract(sid),'usage':usage(s,sid),
  'receipt_rows':s.db.execute('SELECT id,record_json FROM invocation_receipts ORDER BY seq').fetchall(),
  'checkpoint':json.loads((storage/'workspace.json').read_text())['checkpoint'],
  'verified':s.verify_invocations(sid)}))
finally:s.close()
`;

function python(script, args) {
  const run = spawnSync(process.env.KARMA_SMOKE_PYTHON || 'python3', ['-B', '-c', script, ...args],
    {encoding: 'utf8', timeout: 10000, maxBuffer: 2_000_000});
  assert.equal(run.status, 0, run.stderr || run.error?.message);
  return JSON.parse(run.stdout);
}

async function realLedgerContinuity() {
  const directory = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'sutra-coordinator-ledger-')));
  const workspace = path.join(directory, 'workspace'), storage = path.join(directory, 'store');
  fs.mkdirSync(workspace); fs.mkdirSync(storage);
  const before = python(seedLedger, [root, workspace, storage]);
  assert.equal(before.usage.used.calls, 1);
  assert(before.usage.used.tokens > 0);
  const subject = await fixture({real: true, directory});
  try {
    const first = await allocated(subject, {op: 'state'}, {SUTRA_RAM_CACHE_MIB: '0', SUTRA_THINKING: 'on'});
    const reattached = await rpc(subject, 'submit', first);
    assert.equal(reattached.id, first.id);
    const firstResult = await finished(subject, first);
    assert.equal(firstResult.state, 'completed');
    assert.equal(firstResult.response.data.checkpoint, before.checkpoint);
    assert.deepEqual(firstResult.response.data.contract, before.contract);
    assert(firstResult.response.data.last_result.summary.includes('0.375'));
    const pid = firstResult.response.host_memory.worker_pid;
    await ack(subject, first);
    const next = await allocated(subject, {op: 'state'}, {SUTRA_RAM_CACHE_MIB: '0', SUTRA_THINKING: 'off'});
    const nextResult = await finished(subject, next);
    assert.equal(nextResult.state, 'completed');
    assert.equal(nextResult.response.host_memory.worker_pid, pid);
    assert.deepEqual(nextResult.response.data.contract, before.contract);
    await ack(subject, next);
    const after = python(observeLedger, [root, storage]);
    assert.equal(after.verified, true); delete after.verified;
    assert.deepEqual(after, before, 'Transport reattach and settings changes must not alter charged history');
    checks.push('Real Python bridge keeps checkpoint, source context, contract, permissions and consumed spending across reattach');
  } finally {await subject.close();}
}

async function journalWriteFailure() {
  const directory = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'sutra-coordinator-journal-')));
  const config = {workspace: directory, storage: directory, extensionPath: root, python: 'python3',
    runtimeHash: 'a'.repeat(64)};
  const originalRename = fs.renameSync;
  let complete, calls = 0;
  const workerFactory = () => {
    const child = new EventEmitter();
    return {child, closed: false, send() {calls++; return new Promise(resolve => {complete = resolve;});},
      dispose() {this.closed = true; child.emit('close', 0, null);}};
  };
  const controller = new Coordinator(config, directory, crypto.randomBytes(16).toString('hex'), {workerFactory});
  const request = {op: 'state'}, environment = {};
  const allocated = controller.allocate();
  const value = {...allocated, request, environment, digest: digestFor(request, environment), chat_key: ''};
  try {
    controller.submit(value);
    await until(() => !!complete, Boolean, 'Controlled request did not start');
    const committed = fs.readFileSync(controller.journalFile);
    fs.renameSync = (from, to) => {
      if (to === controller.journalFile) throw Object.assign(Error('Controlled journal write failure'), {code: 'EIO'});
      return originalRename(from, to);
    };
    complete({ok: true, data: {result: 'not durably retained'}});
    await until(() => controller.active === null, Boolean, 'Failed settlement did not stop');
    const observed = controller.rpc({op: 'status', id: value.id});
    assert.equal(observed.state, 'interrupted');
    assert.equal(observed.host_completion_received, false);
    assert.equal(observed.response, undefined, 'Undurable output must not be published as a completed result');
    assert.equal(controller.rpc({op: 'status'}).persistence_unavailable, true);
    assert.throws(() => controller.rpc({op: 'allocate'}), /persistence|admission|accepting/i);
    assert.equal(calls, 1);
    assert.deepEqual(fs.readFileSync(controller.journalFile), committed);
    fs.renameSync = originalRename;
    await controller.close();
    const recovered = new Coordinator(config, directory, crypto.randomBytes(16).toString('hex'), {workerFactory});
    assert.equal(recovered.rpc({op: 'status', id: value.id}).state, 'interrupted');
    await pause(20);
    assert.equal(calls, 1, 'Restart must not replay a completed-but-undurable request');
    await recovered.close();
    checks.push('A completion journal failure withholds output and stops admission; restart retains uncertainty without replay');
  } finally {
    fs.renameSync = originalRename;
    if (!controller.stopping) await controller.close();
    fs.rmSync(directory, {recursive: true, force: true});
  }
}

async function journalContentIntegrity() {
  const subject = await fixture();
  try {
    const value = await allocated(subject,{op:'state',tag:'journal-original'});
    await finished(subject,value);
    const before = dispatches(subject).length;
    await rpc(subject,'shutdown');
    await until(() => subject.child.exitCode !== null || subject.child.signalCode !== null,
      Boolean,'Journal integrity trial did not stop its owned service');
    const journal = path.join(subject.storage,'coordinator','journal.json');
    const original = fs.readFileSync(journal);
    for (const mutate of [
      record => {record.body.jobs[0].response.data.answer = 'Altered cached answer without a new digest';},
      record => {record.body.jobs[0].state = 'cancelled';},
    ]) {
      const changed = JSON.parse(original);mutate(changed);
      fs.writeFileSync(journal,JSON.stringify(changed));
      const restart = spawnSync(process.execPath,[path.join(root,'coordinator_service.js'),'--config',subject.configPath],
        {encoding:'utf8',timeout:3000,env:{...process.env,ELECTRON_RUN_AS_NODE:'1'}});
      assert.notEqual(restart.status,0,'An altered cache must not start a service');
      assert.match(restart.stderr,/journal.*digest|digest.*journal/i);
      assert(!fs.existsSync(subject.endpointPath),'Unverified cached output must not be published');
      assert.equal(dispatches(subject).length,before,'Failed verification must not dispatch or replay work');
    }
    fs.writeFileSync(journal,original);
    const recovered = new Coordinator(subject.config,path.dirname(journal),crypto.randomBytes(16).toString('hex'));
    const valid = recovered.rpc({op:'status',id:value.id,chat_key:value.chat_key});
    assert.equal(valid.state,'completed');
    assert.equal(valid.response.data.request.tag,'journal-original');
    assert.equal(valid.response.data.answer,undefined);
    await recovered.close();
    checks.push('Domain-separated journal digests reject altered cached answers and states before restart publication or replay');
  } finally {await subject.close();}
}

async function queuedAuthorization() {
  const subject = await fixture();
  try {
    const grants = path.join(path.dirname(subject.storage), 'workspace-permissions');
    fs.mkdirSync(grants, {mode: 0o700});
    const grant = path.join(grants, 'f'.repeat(64) + '.json');
    const raw = JSON.stringify({id: 'grant-v1', profile: 'workspace_agent'});
    fs.writeFileSync(grant, raw, {mode: 0o600});
    const proof = {path: grant, digest: crypto.createHash('sha256').update(raw).digest('hex')};
    const first = await allocated(subject, {op: 'state', tag: 'grant-head', delay_ms: 250});
    await until(() => dispatches(subject), rows => rows.length === 1, 'Authorization trial head did not start');
    const queued = await allocated(subject, {op: 'state', tag: 'revoked-before-dispatch'}, {}, 'a'.repeat(64), proof);
    fs.writeFileSync(grant, JSON.stringify({id: 'grant-v2', profile: 'read_only'}));
    assert.equal((await finished(subject, first)).state, 'completed');
    const stopped = await finished(subject, queued);
    assert.equal(stopped.state, 'cancelled');
    assert.match(stopped.error, /authorization.*changed|changed.*authorization/i);
    assert(!dispatches(subject).some(row => row.request.tag === 'revoked-before-dispatch'));
    await ack(subject, first); await ack(subject, queued);
    checks.push('Queued work rechecks exact workspace authorization bytes before Python dispatch; revocation starts no operation');

    fs.writeFileSync(grant, raw);
    const active = await allocated(subject, {op: 'state', tag: 'stop-all-head', delay_ms: 1500}, {}, 'a'.repeat(64), proof);
    await until(() => dispatches(subject), rows => rows.some(row => row.request.tag === 'stop-all-head'), 'Stop-all trial did not start');
    const tail = await allocated(subject, {op: 'state', tag: 'stop-all-tail'}, {}, 'b'.repeat(64), proof);
    const cancelling = await rpc(subject, 'cancel_all');
    assert.equal(cancelling.cancelled_queued, 1);
    assert.equal(cancelling.backend_completion_verified, false);
    assert.equal((await finished(subject, active)).state, 'cancelled');
    assert.equal((await finished(subject, tail)).state, 'cancelled');
    assert(!dispatches(subject).some(row => row.request.tag === 'stop-all-tail'));
    await ack(subject, active); await ack(subject, tail);
    checks.push('Workspace-wide Stop clears queued work before signalling the running child, including other chat queues');
  } finally {await subject.close();}
}

async function privateRecords() {
  const directory = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'sutra-coordinator-records-')));
  try {
    const file = path.join(directory, 'record.json');
    atomicPrivate(file, {value: 'private'});
    assert.deepEqual(readPrivate(file), {value: 'private'});
    assert.equal(fs.statSync(file).mode & 0o077, 0);
    const linked = path.join(directory, 'hardlink.json');
    fs.linkSync(file, linked);
    assert.throws(() => readPrivate(file), /private|unlinked/);
    assert.throws(() => atomicPrivate(file, {value: 'replacement'}), /private|unlinked/);
    fs.unlinkSync(linked);
    const symlink = path.join(directory, 'symlink.json');
    fs.symlinkSync(file, symlink);
    assert.throws(() => readPrivate(symlink));
    assert.throws(() => readPrivate(file, 1), /bound/);
    if (process.platform !== 'win32') {
      fs.chmodSync(file, 0o644);
      assert.throws(() => readPrivate(file), /private/);
      fs.chmodSync(file, 0o600);
      const fifo = path.join(directory, 'fifo');
      const made = spawnSync('mkfifo', ['-m', '600', fifo], {encoding: 'utf8', timeout: 1000});
      assert.equal(made.status, 0, made.stderr);
      const result = spawnSync(process.execPath, ['-e',
        "try {require(process.argv[1]).readPrivate(process.argv[2]);process.exitCode=2;} catch {process.exitCode=0;}",
        path.join(root, 'coordinator_service.js'), fifo], {timeout: 1000, encoding: 'utf8'});
      assert.equal(result.status, 0, 'Non-regular metadata must fail without blocking a UI or service startup');
    }
    checks.push('Private metadata rejects shared permissions, links, oversized records and blocking FIFO paths');
  } finally {fs.rmSync(directory, {recursive: true, force: true});}
}

async function main() {
  await ownershipAndRecovery();
  await bounds();
  await crashRecovery();
  await missingTransportRecovery();
  await realLedgerContinuity();
  await journalWriteFailure();
  await journalContentIntegrity();
  await queuedAuthorization();
  await privateRecords();
  console.log(JSON.stringify({passed: true, checks}, null, 2));
}

main().catch(error => {console.error(error); process.exitCode = 1;});

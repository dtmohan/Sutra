'use strict';
// Local transport coordination only. Task authority and spending remain in the
// existing Python gateway and ledger; this cache never replays interrupted work.
const fs = require('node:fs');
const path = require('node:path');
const net = require('node:net');
const crypto = require('node:crypto');
const {spawn} = require('node:child_process');
const {BridgeWorker, preventIdleSleep} = require('./bridge_worker');
const activity = require('./model_activity');
const taskRunner = require('./task_runner');
const {CoordinatorTelemetry} = require('./coordinator_telemetry');

const VERSION = 1;
const MAX_FRAME = 4_500_000;
const MAX_REQUEST = 200_000;
const MAX_RESPONSE = 4_000_000;
const MAX_EVENTS = 512 * 1024;
const MAX_JOURNAL = 16 * 1024 * 1024;
const MAX_JOBS = 8;
const TERMINAL = new Set(['completed', 'cancelled', 'interrupted', 'failed']);
const ID = /^[a-f0-9]{32}$/;
const HASH = /^[a-f0-9]{64}$/;

function canonical(value) {
  if (value === null || typeof value !== 'object') {
    if (value === undefined || typeof value === 'function' || typeof value === 'symbol' ||
        typeof value === 'number' && !Number.isFinite(value)) throw new Error('Invalid canonical coordinator value');
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  return '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}';
}
function hash(value) {return crypto.createHash('sha256').update(value).digest('hex');}
function digestFor(request, environment, authorization, runner) {
  return hash(canonical({request,environment,
    ...(authorization === undefined ? {} : {authorization}), ...(runner === undefined ? {} : {runner})}));
}
function scopeFor(workspace, storage) {return hash(canonical({workspace:path.resolve(workspace), storage:path.resolve(storage)}));}
function journalRecord(body) {
  // Content evidence only, not an authenticity claim against a caller that can
  // rewrite both private data and its digest. Task receipts remain authoritative.
  return {version:VERSION,body,digest:hash('sutra-coordinator-journal-v1:' + canonical(body))};
}
function verifiedJournal(record) {
  if (!record || typeof record !== 'object' || Array.isArray(record) ||
      Object.keys(record).sort().join(',') !== 'body,digest,version' || record.version !== VERSION ||
      !record.body || typeof record.body !== 'object' || Array.isArray(record.body) || typeof record.digest !== 'string' || !HASH.test(record.digest)) {
    throw new Error('Invalid coordinator journal integrity record; cached results are withheld');
  }
  const expected = journalRecord(record.body).digest;
  if (!crypto.timingSafeEqual(Buffer.from(record.digest),Buffer.from(expected))) {
    throw new Error('Coordinator journal content digest mismatch; cached results are withheld');
  }
  return record.body;
}
function privateStat(info, directory = false) {
  if ((directory ? !info.isDirectory() : !info.isFile() || info.nlink !== 1) ||
      process.getuid && info.uid !== process.getuid() || process.platform !== 'win32' && info.mode & 0o077) {
    throw new Error('Coordinator files and directories must be private and unlinked');
  }
}
function readPrivateBytes(file, limit = 8192) {
  const descriptor = fs.openSync(file, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0) | (fs.constants.O_NONBLOCK || 0));
  try {
    const info = fs.fstatSync(descriptor); privateStat(info);
    if (info.size > limit) throw new Error('Coordinator record exceeds its bound');
    const raw = Buffer.alloc(limit + 1), length = fs.readSync(descriptor,raw,0,raw.length,0);
    if (length > limit) throw new Error('Coordinator record exceeds its bound');
    return raw.subarray(0,length);
  } finally {fs.closeSync(descriptor);}
}
function readPrivate(file, limit = 8192) {return JSON.parse(readPrivateBytes(file,limit).toString('utf8'));}

function atomicPrivate(file, value, limit = MAX_JOURNAL) {
  const raw = canonical(value);
  if (Buffer.byteLength(raw) > limit) throw new Error('Coordinator journal capacity exceeded');
  try {privateStat(fs.lstatSync(file));} catch (error) {if (error.code !== 'ENOENT') throw error;}
  const temp = file + '.' + crypto.randomBytes(8).toString('hex') + '.tmp';
  let descriptor;
  try {
    descriptor = fs.openSync(temp, fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_WRONLY, 0o600);
    fs.writeFileSync(descriptor, raw); fs.fsyncSync(descriptor); fs.closeSync(descriptor); descriptor = undefined;
    fs.renameSync(temp, file);
    if (process.platform !== 'win32') {
      const parent = fs.openSync(path.dirname(file),fs.constants.O_RDONLY);
      try {fs.fsyncSync(parent);} finally {fs.closeSync(parent);}
    }
  } finally {
    if (descriptor !== undefined) fs.closeSync(descriptor);
    try {fs.unlinkSync(temp);} catch (error) {if (error.code !== 'ENOENT') throw error;}
  }
}
function acquireLock(directory, instance) {
  const file = path.join(directory, 'service.lock');
  try {
    const fd = fs.openSync(file, fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_WRONLY, 0o600);
    fs.writeFileSync(fd, canonical({pid:process.pid, instance_id:instance}));fs.fsyncSync(fd);
    return () => {
      try {
        const info = fs.lstatSync(file), held = fs.fstatSync(fd);
        if (info.ino === held.ino && info.dev === held.dev) fs.unlinkSync(file);
      } finally {fs.closeSync(fd);}
    };
  } catch (error) {
    if (error.code !== 'EEXIST') throw error;
    const before = fs.lstatSync(file), owner = readPrivate(file);
    if (!Number.isSafeInteger(owner.pid) || owner.pid <= 0) throw new Error('Coordinator startup is already in progress');
    try {process.kill(owner.pid, 0);} catch (probe) {
      if (probe.code !== 'ESRCH') throw new Error('Coordinator service ownership cannot be verified');
      const after = fs.lstatSync(file);
      if (before.ino !== after.ino || before.dev !== after.dev) throw new Error('Coordinator service ownership changed');
      fs.unlinkSync(file);
      return acquireLock(directory, instance);
    }
    throw new Error('A coordinator service already owns this workspace');
  }
}
function validateConfig(config, configFile) {
  if (!config || typeof config !== 'object' ||
      !['workspace','storage','python','extensionPath','runtimeHash'].every(key => typeof config[key] === 'string') ||
      !HASH.test(config.runtimeHash) || !path.isAbsolute(config.workspace) || !path.isAbsolute(config.storage) ||
      !path.isAbsolute(config.extensionPath) || !config.python || config.python.includes('\0')) throw new Error('Invalid coordinator configuration');
  config = {...config, workspace:fs.realpathSync(config.workspace), storage:fs.realpathSync(config.storage),
    extensionPath:fs.realpathSync(config.extensionPath)};
  const directory = path.join(config.storage, 'coordinator');
  privateStat(fs.lstatSync(directory), true);
  if (fs.realpathSync(path.dirname(configFile)) !== directory) throw new Error('Coordinator configuration must belong to its private storage');
  return config;
}
function safeError(error) {return String(error?.message || error).slice(0, 1500);}
function decodeCounter(id, instance) {
  if (typeof id !== 'string' || !ID.test(id) || !id.startsWith(instance.slice(0,16))) return null;
  const value = Number.parseInt(id.slice(16), 16);
  return Number.isSafeInteger(value) && value > 0 ? value : null;
}
function cancellation() {
  const callbacks = new Set();
  return {isCancellationRequested:false,
    onCancellationRequested(callback) {callbacks.add(callback);return {dispose:() => callbacks.delete(callback)};},
    cancel() {if (this.isCancellationRequested) return;this.isCancellationRequested = true;for (const callback of callbacks) callback();}};
}

class Coordinator {
  constructor(config, directory, instance, options = {}) {
    this.config = config;this.directory = directory;this.instance = instance;this.scope = scopeFor(config.workspace, config.storage);
    this.journalFile = path.join(directory, 'journal.json');this.worker = null;this.active = null;
    this.queue = [];this.stopping = false;this.lastContact = Date.now();this.workerFactory = options.workerFactory;
    this.eventFlushes = new Map();this.waitTimers = new Map();this.telemetry = null;
    let previous;
    try {previous = verifiedJournal(readPrivate(this.journalFile, MAX_JOURNAL));} catch (error) {if (error.code !== 'ENOENT') throw error;}
    if (previous && (previous.version !== VERSION || previous.scope !== this.scope || !Array.isArray(previous.jobs) ||
        previous.jobs.length > MAX_JOBS)) throw new Error('Coordinator journal belongs to another workspace or version');
    this.state = {version:VERSION, scope:this.scope, instance_id:instance, issued:0, retired_through:0, jobs:previous?.jobs || []};
    const seen = new Set();
    for (const job of this.state.jobs) {
      if (!job || !ID.test(job.id) || !HASH.test(job.digest) || seen.has(job.id) || !Array.isArray(job.events) ||
          !['queued','running','waiting','cancelling',...TERMINAL].includes(job.state) || typeof job.chat_key !== 'string') {
        throw new Error('Invalid retained coordinator request');
      }
      if (job.runner && (canonical(taskRunner.runner(job.runner,job.initial_request)) !== canonical(job.runner) ||
          digestFor(job.initial_request,job.environment,job.authorization,job.runner) !== job.digest)) {
        throw new Error('The retained task-runner submission changed');
      }
      seen.add(job.id);
      if (job.runner && job.stop_receipt_pending) {
        job.state='cancelled';this.queueStopAudit(job);continue;
      }
      if (!TERMINAL.has(job.state)) {
        if (job.runner && job.runner_runtime_hash===config.runtimeHash && ['queued','waiting'].includes(job.state)) {
          // Running/cancelling work has an unknown outcome and is never replayed.
          // A queued checkpoint was fsynced before dispatch; its next operation
          // still rechecks the cursor, exact grant and normal gateway admission.
          if (job.continuation) {
            const descriptor=taskRunner.plan({...job.workflow,continuation:job.continuation},undefined,job.id);
            if (job.state==='waiting') this.waitForReadiness(job,descriptor);
            else {
              const mode=descriptor.disposition==='wait'?'probe':descriptor.disposition==='continue'?'flow':'summary';
              this.enqueue(job,taskRunner.nextRequest(job.initial_request,descriptor,
                mode==='summary'?'runner_summary':descriptor.next_event),mode);
            }
          } else this.queue.push({job,request:job.initial_request,environment:job.environment});
          continue;
        }
        job.state = 'interrupted';job.finished_at = Date.now();job.backend_completion_verified = false;
        job.error = 'The coordinator ended without a completion receipt. Work was not replayed; recover through the saved task.';
      }
    }
    this.persist();
    this.syncTelemetry();
    if (this.queue.length) setImmediate(() => this.runNext());
  }
  persist() {
    try {atomicPrivate(this.journalFile, journalRecord(this.state));}
    catch (error) {
      this.persistenceError = safeError(error);
      const active = this.active;
      if (active) queueMicrotask(() => {if (this.active === active) active.token.cancel();});
      throw error;
    }
  }
  find(id, chat) {
    if (typeof id !== 'string' || !ID.test(id)) throw new Error('Invalid coordinator request identity');
    const job = this.state.jobs.find(item => item.id === id);
    if (job && job.chat_key !== chat) throw new Error('Coordinator request belongs to another chat');
    return job;
  }
  retired(id) {
    const seq = decodeCounter(id, this.instance);
    return seq !== null && seq <= this.state.retired_through;
  }
  publicJob(job, includeResponse = false) {
    const value = {id:job.id, request_id:job.id, digest:job.digest, state:job.state, chat_key:job.chat_key,
      operation:job.operation, created_at:job.created_at, started_at:job.started_at ?? null,
      finished_at:job.finished_at ?? null, last_event_seq:job.last_event_seq, dropped_before:job.dropped_before,
      acknowledged:!!job.acknowledged, backend_completion_verified:job.backend_completion_verified === true,
      host_completion_received:job.host_completion_received === true};
    if (job.runner) Object.assign(value,{runner:job.runner,steps:job.steps || 0,workflow:job.workflow || null});
    if (job.readiness && ['waiting','queued','running'].includes(job.state)) {
      const {phase,message,since,checks}=job.readiness;
      value.readiness={phase,message,since,checks,next_check_at:job.state==='waiting'?job.next_probe_at:null};
    }
    if (job.stop_receipt_pending) value.stop_receipt_pending=true;
    if (job.stop_receipt) value.stop_receipt=job.stop_receipt;
    if (job.stop_kernel) value.stop_kernel=job.stop_kernel;
    if (job.stop_audit_error) value.stop_audit_error=job.stop_audit_error;
    if (includeResponse && job.error) value.error = job.error;
    if (this.persistenceError && TERMINAL.has(job.state)) {
      value.state = 'interrupted';value.host_completion_received = false;
      value.error = 'Coordinator journal persistence failed; completion could not be retained. Admission is stopped and work will not be replayed.';
    } else if (includeResponse && job.response !== undefined) value.response = job.response;
    return value;
  }
  reserveSpace() {
    const reserve = this.state.jobs.filter(job => !TERMINAL.has(job.state)||job.stop_receipt_pending).length * (MAX_RESPONSE + MAX_EVENTS + MAX_REQUEST + 8192);
    return Buffer.byteLength(canonical(this.state)) + 256 + reserve;
  }
  prune() {
    while (this.state.jobs.length >= MAX_JOBS || this.reserveSpace() + MAX_RESPONSE + MAX_EVENTS + MAX_REQUEST + 8192 > MAX_JOURNAL) {
      const index = this.state.jobs.findIndex(job => job.acknowledged && TERMINAL.has(job.state) && !job.stop_receipt_pending);
      if (index < 0) throw new Error('Coordinator retained-result capacity is full; acknowledge saved results before submitting more work');
      const [job] = this.state.jobs.splice(index, 1), seq = decodeCounter(job.id, this.instance);
      if (seq !== null) this.state.retired_through = Math.max(this.state.retired_through, seq);
    }
  }
  allocate() {
    if (this.stopping || this.persistenceError || this.stopAuditBlocked() || !Number.isSafeInteger(this.state.issued + 1)) throw new Error('Coordinator is not accepting requests; inspect pending Stop receipts');
    const next = this.state.issued + 1;
    this.state.issued = next;this.persist();
    return {id:this.instance.slice(0,16) + next.toString(16).padStart(16,'0'), instance_id:this.instance};
  }
  authorization(proof, verify = false) {
    if (!proof || typeof proof !== 'object' || Array.isArray(proof) ||
        Object.keys(proof).sort().join(',') !== 'digest,path' || typeof proof.path !== 'string' ||
        !path.isAbsolute(proof.path) || !HASH.test(proof.digest || '')) throw new Error('Invalid coordinator authorization proof');
    const directory = path.join(path.dirname(this.config.storage),'workspace-permissions');
    if (path.resolve(proof.path) !== proof.path || path.dirname(proof.path) !== directory || !/^[a-f0-9]{64}\.json$/.test(path.basename(proof.path))) {
      throw new Error('Coordinator authorization proof is outside workspace permission storage');
    }
    if (verify) {
      privateStat(fs.lstatSync(directory),true);
      const raw = readPrivateBytes(proof.path,8192);
      if (hash(raw) !== proof.digest) throw new Error('Workspace permission record changed');
    }
    return {path:proof.path,digest:proof.digest};
  }
  submit(frame) {
    const chat = frame.chat_key ?? '';
    if (typeof chat !== 'string' || chat.length > 256 || !ID.test(frame.id || '') || !HASH.test(frame.digest || '') ||
        !frame.request || typeof frame.request !== 'object' || Array.isArray(frame.request) ||
        !frame.environment || typeof frame.environment !== 'object' || Array.isArray(frame.environment)) throw new Error('Invalid coordinator submission');
    const runner = taskRunner.runner(frame.runner,frame.request);
    if (Buffer.byteLength(canonical({request:frame.request, environment:frame.environment,
        ...(frame.authorization === undefined ? {} : {authorization:frame.authorization}), ...(runner ? {runner} : {})})) + 1 > MAX_REQUEST) throw new Error('Coordinator request exceeds its transport allocation');
    if (digestFor(frame.request,frame.environment,frame.authorization,runner) !== frame.digest) throw new Error('Coordinator request digest does not match its payload');
    const authorization = frame.authorization === undefined ? undefined : this.authorization(frame.authorization);
    const old = this.find(frame.id, chat);
    if (old) {
      if (old.digest !== frame.digest) throw new Error('Coordinator request changed after submission');
      return this.publicJob(old, true);
    }
    const seq = decodeCounter(frame.id, this.instance);
    if (seq === null || seq > this.state.issued || this.retired(frame.id)) throw new Error('Coordinator request identity expired or was already acknowledged; work will not be replayed');
    if (this.stopping || this.persistenceError || this.stopAuditBlocked()) throw new Error('Coordinator admission is stopped; inspect pending Stop receipts');
    this.prune();
    // A request with an older allocated sequence may have expired during pruning.
    if (this.retired(frame.id)) throw new Error('Coordinator request identity expired before admission');
    const job = {id:frame.id, digest:frame.digest, chat_key:chat, operation:String(frame.request.op || '').slice(0,80),
      state:'queued', created_at:Date.now(), events:[], event_bytes:0, last_event_seq:0, dropped_before:0,
      acknowledged:false, backend_completion_verified:false, ...(authorization ? {authorization} : {}),
      ...(runner ? {runner,runner_runtime_hash:this.config.runtimeHash,initial_request:frame.request,environment:frame.environment,steps:0} : {})};
    this.state.jobs.push(job);this.persist();this.syncTelemetry();
    this.queue.push({job, request:frame.request, environment:frame.environment});
    setImmediate(() => this.runNext());
    return this.publicJob(job);
  }
  event(job, event) {
    const row = {seq:++job.last_event_seq,event};
    job.events.push(row);job.event_bytes += Buffer.byteLength(canonical(row));
    while (job.event_bytes > MAX_EVENTS || job.events.length > 1024) {
      let index = job.events.findIndex(item => ['thinking','progress'].includes(item.event.kind));
      if (index < 0) index = 0;
      const [removed] = job.events.splice(index, 1);
      job.event_bytes -= Buffer.byteLength(canonical(removed));job.dropped_before = Math.max(job.dropped_before,removed.seq);
    }
    // Progress is diagnostic; coalesce disk writes instead of serializing the
    // full bounded cache for every token chunk. State transitions and completion
    // flush synchronously, and authoritative gateway receipts are unaffected.
    if (!this.eventFlushes.has(job.id)) {
      const timer = setTimeout(() => {
        this.eventFlushes.delete(job.id);
        try {this.persist();} catch {/* Persistence failure already stops admission. */}
      },250);timer.unref();this.eventFlushes.set(job.id,timer);
    }
  }
  makeWorker() {
    if (this.worker && !this.worker.closed) return this.worker;
    const options = {spawn,command:this.config.python,
      args:['-B',path.join(this.config.extensionPath,'harness','vscode_bridge.py'),'--workspace',this.config.workspace,
        '--storage',this.config.storage,'--request-stream'],
      options:{cwd:this.config.workspace,stdio:['pipe','pipe','pipe'],env:{...process.env}},
      activity:activity.decoder,
      keepAwake:() => preventIdleSleep({spawn,pid:process.pid}),
      onClose:() => {}};
    const worker = this.workerFactory ? this.workerFactory(options) : new BridgeWorker(options);
    worker.hostStopped = false;
    worker.stopped = new Promise(resolve => worker.child.once('close', () => {worker.hostStopped = true;resolve();}));
    this.worker = worker;
    return worker;
  }
  async stopWorker(worker) {
    if (worker.hostStopped) return;
    let timer;
    try {
      await Promise.race([worker.stopped, new Promise(resolve => {timer=setTimeout(() => {try {worker.child.kill('SIGKILL');} catch {}resolve();},10000);})]);
    } finally {clearTimeout(timer);}
    // Do not publish cancellation or start competing work until the actual
    // child exit is observed. This says nothing about a shared Ollama request.
    await worker.stopped;
  }
  pendingTasks() {return this.state.jobs.filter(job => job.runner && (!TERMINAL.has(job.state)||job.stop_receipt_pending)).length;}
  stopAuditBlocked() {return this.state.jobs.some(job=>job.stop_receipt_pending&&job.stop_audit_error);}
  queueStopAudit(job,reason='Explicit runner Stop') {
    if (!job.runner || job.stop_receipt || this.queue.some(item=>item.job===job&&item.mode==='stop') ||
        this.active?.job===job&&this.active.mode==='stop') return;
    job.stop_receipt_pending=true;job.stop_reason ||= reason.slice(0,500);delete job.stop_audit_error;
    const request={op:'orchestrate',event:'runner_stop',
      runner_stop:{version:1,request_digest:job.digest,reason:job.stop_reason}};
    if (job.initial_request.chat_key!==undefined) request.chat_key=job.initial_request.chat_key;
    const flow=job.workflow?.flow_id || job.initial_request.flow_id;
    if (flow) request.flow_id=flow;
    // Immediate transport cutoff does not wait behind another chat's inference.
    // The durable administrative FIN is drained before ordinary queued work.
    this.queue.unshift({job,request,environment:job.environment,mode:'stop'});
  }
  syncTelemetry() {
    if (this.pendingTasks() && !this.stopping && !this.persistenceError) {
      this.telemetry ??= new CoordinatorTelemetry({...this.config,
        onError:message => {process.stderr.write('Sutra runner telemetry: '+message+'\n');}});
      this.telemetry.ensure();
    } else {this.telemetry?.dispose();this.telemetry=null;}
  }
  checkAuthority(job) {
    if (!job.authorization) return;
    try {this.authorization(job.authorization,true);}
    catch {
      throw Object.assign(new Error('Workspace authorization changed before the next task step; automatic execution was stopped.'),
        {code:'COORDINATOR_AUTHORIZATION_CHANGED'});
    }
  }
  enqueue(job, request, mode='flow') {
    job.state='queued';
    this.queue.push({job,request,environment:job.environment,mode});
  }
  waitForReadiness(job, continuation) {
    job.state='waiting';
    job.wait_attempts=(job.wait_attempts || 0)+1;
    // These delays schedule observations only. They never retry an inference,
    // expand an allocation or establish that an interrupted request finished.
    const delay=Math.min(30000,1000*Math.pow(2,Math.min(job.wait_attempts-1,5)));
    job.next_probe_at=Date.now()+delay;
    clearTimeout(this.waitTimers.get(job.id));
    const timer=setTimeout(() => {
      this.waitTimers.delete(job.id);
      if (job.state!=='waiting' || this.stopping || this.persistenceError) return;
      this.enqueue(job,taskRunner.nextRequest(job.initial_request,continuation,'continuation_probe'),'probe');
      try {this.persist();} catch {return;}
      setImmediate(() => this.runNext());
    },delay);
    timer.unref();this.waitTimers.set(job.id,timer);
    return delay;
  }
  async advanceTask(job, item, response, worker, token) {
    if (!response.ok) {job.response=response;job.state='failed';return;}
    const data=response.data, previous=job.continuation;
    const continuation=taskRunner.plan(data,previous,job.id);
    if (item.mode==='summary') {
      if (continuation.cursor!==previous.cursor || continuation.fingerprint!==previous.fingerprint) {
        throw new Error('The saved task changed before its final response could be collected');
      }
      for (const key of ['runner_usage','conversation']) {
        if (data[key]!==undefined) job.response.data[key]=data[key];
      }
      job.state='completed';return;
    }
    if (item.mode!=='probe') {
      job.steps++;
      job.wait_attempts=0;
      // A runnable receipt is dispatched once. An unchanged checkpoint is a
      // protocol failure, not permission to repeat a model/source operation.
      if (previous && continuation.disposition==='continue' && continuation.next_event==='step' &&
          continuation.cursor===previous.cursor && continuation.fingerprint===previous.fingerprint) {
        throw new Error('The task made no durable progress; automatic continuation stopped at the saved checkpoint');
      }
      job.response=response;
    }
    if (item.mode==='probe' && !['continue','wait'].includes(continuation.disposition)) job.response=response;
    job.continuation=continuation;
    job.workflow=taskRunner.checkpoint(data,continuation);
    if (token.isCancellationRequested) throw new Error('The task was stopped before its next stage');
    const prior=job.readiness, checks=(prior?.checks||0)+Number(!!data.continuation_probe);
    if (continuation.disposition==='wait') {
      const wait=taskRunner.waitObservation(data,continuation);
      job.readiness={...wait,phase:'waiting',since:prior?.since??Date.now(),checks};
      const delay=this.waitForReadiness(job,continuation);
      if (prior?.phase!=='waiting'||prior.condition!==wait.condition) this.event(job,taskRunner.activity(data,continuation,{wait,delay}));
      return;
    }
    const activity=taskRunner.activity(data,continuation);
    if (continuation.next_event==='resume') {
      job.readiness={phase:'ready',message:activity.progress,since:prior?.since??Date.now(),checks};
    } else delete job.readiness;
    this.event(job,activity);
    if (continuation.disposition==='continue') {
      this.enqueue(job,taskRunner.nextRequest(job.initial_request,continuation));
    } else {
      // A read-only summary collects complete usage once. It cannot infer or
      // acquire missing evidence, and never substitutes a stale final report.
      this.enqueue(job,taskRunner.nextRequest(job.initial_request,continuation,'runner_summary'),'summary');
    }
  }
  async runNext() {
    if (this.active || this.stopping || this.persistenceError || !this.queue.length) return;
    if (this.stopAuditBlocked() && this.queue[0].mode!=='stop') return;
    const item = this.queue.shift(), {job} = item;
    const stopAudit=item.mode==='stop';
    if (stopAudit ? !job.stop_receipt_pending : job.state !== 'queued') return this.runNext();
    const token = cancellation();this.active = {job,token,mode:item.mode};
    let worker;
    try {
      if (!stopAudit) {job.state = 'running';job.started_at ??= Date.now();}
      this.persist();
      if (!stopAudit) this.checkAuthority(job);
      worker = this.makeWorker();this.active.worker = worker;
      const request=job.runner?{...item.request,task_runner:{version:1,request_id:job.id,request_digest:job.digest}}:item.request;
      const response = await worker.send(request,item.environment,{token,onActivity:event => this.event(job,event)});
      if (Buffer.byteLength(canonical(response)) > MAX_RESPONSE) throw new Error('Coordinator response exceeds its retained-result allocation');
      if (stopAudit) {
        const data=response.data,receipt=data?.receipt;
        if (!response.ok || data?.stopped!==true || data.runner_request_id!==job.id || data.request_digest!==job.digest ||
            data.kernel?.profile!=='task-continuation-v1' || data.kernel?.decision!=='FIN' ||
            data.kernel?.grants_permissions!==false || data.kernel?.replenishes_budget!==false ||
            receipt?.version!==1 || !Number.isSafeInteger(receipt.seq) || receipt.seq<1 || !HASH.test(receipt.hash||'')) {
          throw new Error('The backend did not retain the runner Stop in its kernel audit chain');
        }
        job.stop_receipt=receipt;job.stop_kernel=data.kernel;job.stop_receipt_pending=false;delete job.stop_audit_error;return;
      }
      if (token.isCancellationRequested) throw new Error('The task was stopped before its response was published');
      if (job.runner) await this.advanceTask(job,item,response,worker,token);
      else {job.response = response;job.state = response.ok ? 'completed' : 'failed';}
      if (job.response && Buffer.byteLength(canonical(job.response)) > MAX_RESPONSE) throw new Error('Coordinator response exceeds its retained-result allocation');
      // This is the host response boundary, not proof that a model answer is true.
      job.host_completion_received = TERMINAL.has(job.state);job.backend_completion_verified = false;
    } catch (error) {
      if (worker && (token.isCancellationRequested || worker.closed)) await this.stopWorker(worker);
      if (stopAudit) {job.stop_audit_error=safeError(error);return;}
      job.state = this.persistenceError ? 'interrupted' : token.isCancellationRequested || error.code === 'COORDINATOR_AUTHORIZATION_CHANGED' ? 'cancelled' : 'failed';
      job.error = safeError(error);job.backend_completion_verified = false;delete job.response;
      this.queue=this.queue.filter(pending => pending.job!==job||pending.mode==='stop');
      if (job.state==='cancelled') this.queueStopAudit(job,job.error);
      clearTimeout(this.waitTimers.get(job.id));this.waitTimers.delete(job.id);
    } finally {
      if (TERMINAL.has(job.state)) job.finished_at = Date.now();
      clearTimeout(this.eventFlushes.get(job.id));this.eventFlushes.delete(job.id);
      try {this.persist();} catch {job.state = 'interrupted';job.host_completion_received = false;}
      this.active = null;this.lastContact = Date.now();
      this.syncTelemetry();
      if (!this.persistenceError) setImmediate(() => this.runNext());
    }
  }
  cancel(job) {
    if (TERMINAL.has(job.state)) {
      if (job.stop_receipt_pending) {this.queueStopAudit(job);this.persist();setImmediate(()=>this.runNext());}
      return this.publicJob(job,true);
    }
    if (['queued','waiting'].includes(job.state)) {
      this.queue = this.queue.filter(item => item.job !== job);job.state = 'cancelled';job.finished_at = Date.now();
      clearTimeout(this.waitTimers.get(job.id));this.waitTimers.delete(job.id);
      job.error = job.runner && job.steps ? 'Task stopped between stages. Settled work and all spending remain saved.' : 'Request cancelled before host dispatch; no inference was started.';
      this.queueStopAudit(job);this.persist();this.syncTelemetry();setImmediate(()=>this.runNext());
    } else if (this.active?.job === job) {
      job.state = 'cancelling';this.queueStopAudit(job);this.persist();this.active.token.cancel();
    }
    return this.publicJob(job,true);
  }
  cancelAll() {
    const queued = this.state.jobs.filter(job => ['queued','waiting'].includes(job.state)), now = Date.now();
    this.queue=this.queue.filter(item=>item.mode==='stop');
    for (const job of queued) {
      clearTimeout(this.waitTimers.get(job.id));this.waitTimers.delete(job.id);
      job.state = 'cancelled';job.finished_at = now;
      job.error = 'Task stopped before its next host dispatch. Previously settled work remains charged.';
      this.queueStopAudit(job);
    }
    const active = this.active;
    if (active&&active.mode!=='stop') {active.job.state = 'cancelling';this.queueStopAudit(active.job);}
    // Clear every queued request before signalling active work. A fast worker
    // cancellation must not allow the next stale-authority request to start.
    try {this.persist();} finally {if(active?.mode!=='stop')active?.token.cancel();this.syncTelemetry();setImmediate(()=>this.runNext());}
    return {cancelled_queued:queued.length,active:active ? this.publicJob(active.job) : null,
      jobs:this.state.jobs.map(job => this.publicJob(job)),busy:!!active,backend_completion_verified:false};
  }
  rpc(frame) {
    this.lastContact = Date.now();
    if (this.persistenceError && !['status','events','cancel','cancel_all'].includes(frame.op)) throw new Error('Coordinator journal persistence failed; admission is stopped');
    if (frame.op === 'allocate') return this.allocate();
    if (frame.op === 'submit') return this.submit(frame);
    if (frame.op === 'cancel_all') return this.cancelAll();
    const chat = frame.chat_key ?? '';
    if (typeof chat !== 'string' || chat.length > 256) throw new Error('Invalid coordinator chat scope');
    if (frame.op === 'status' && frame.id === undefined) return {version:VERSION,instance_id:this.instance,scope:this.scope,
      active:this.active && (frame.all === true || this.active.job.chat_key === chat) ? this.publicJob(this.active.job) : null,
      jobs:this.state.jobs.filter(job => frame.all === true || job.chat_key === chat).map(job => this.publicJob(job)),
      busy:!!this.active, queued:this.queue.length, pending_tasks:this.pendingTasks(),
      waiting:this.state.jobs.filter(job => job.state==='waiting').length,persistence_unavailable:!!this.persistenceError};
    const job = this.find(frame.id,chat);
    if (!job) {
      if (['status','ack'].includes(frame.op) && this.retired(frame.id)) return {id:frame.id,request_id:frame.id,state:'acknowledged',acknowledged:true,response_evicted:true};
      throw Object.assign(new Error('The coordinator has no retained record for this request. Its execution state is unknown; work will not be replayed.'),
        {code:'COORDINATOR_REQUEST_MISSING'});
    }
    if (frame.op === 'status') return this.publicJob(job,true);
    if (frame.op === 'events') {
      const after = frame.after ?? 0;
      if (!Number.isSafeInteger(after) || after < 0 || after > job.last_event_seq) throw new Error('Invalid coordinator event cursor');
      return {id:job.id,events:job.events.filter(item => item.seq > after),last_event_seq:job.last_event_seq,
        dropped_before:job.dropped_before,state:job.state,
        active_calls:job.events.filter(item => ['start','control'].includes(item.event.kind)).map(item => item.event)};
    }
    if (frame.op === 'cancel') return this.cancel(job);
    if (frame.op === 'ack') {
      if (!TERMINAL.has(job.state)) throw new Error('A running request cannot be acknowledged');
      job.acknowledged = true;this.persist();return this.publicJob(job);
    }
    throw new Error('Unknown coordinator operation');
  }
  async close() {
    if (this.active || (this.queue.length || this.pendingTasks()) && !this.persistenceError) throw new Error('Coordinator has running, queued or waiting work');
    this.stopping = true;
    for (const timer of this.eventFlushes.values()) clearTimeout(timer);this.eventFlushes.clear();
    for (const timer of this.waitTimers.values()) clearTimeout(timer);this.waitTimers.clear();
    this.telemetry?.dispose();this.telemetry=null;
    this.worker?.dispose();if (!this.persistenceError) this.persist();
  }
}

async function start(configFile, options = {}) {
  const config = validateConfig(readPrivate(configFile),configFile), directory = path.join(config.storage,'coordinator');
  const instance = crypto.randomBytes(16).toString('hex'), token = crypto.randomBytes(32).toString('hex');
  const unlock = acquireLock(directory,instance);
  let controller, server, idleTimer, closed = false;
  const sockets = new Set(), endpointFile = path.join(directory,'endpoint.json');
  try {controller = new Coordinator(config,directory,instance,options);} catch (error) {unlock();throw error;}
  const close = async () => {
    if (closed) return;await controller.close();closed = true;clearInterval(idleTimer);
    for (const socket of sockets) socket.end();
    await new Promise(resolve => server.close(resolve));
    try {const current=readPrivate(endpointFile);if(current.instance_id===instance)fs.unlinkSync(endpointFile);} catch(error) {if(error.code!=='ENOENT')throw error;}
    unlock();
  };
  server = net.createServer(socket => {
    sockets.add(socket);socket.on('close',() => sockets.delete(socket));socket.on('error',() => {});
    socket.setTimeout(5000,() => socket.destroy());let buffer='',handled=false;
    socket.setEncoding('utf8');
    socket.on('data',chunk => {
      if (handled) return;buffer += chunk;
      if (Buffer.byteLength(buffer) > MAX_FRAME) {handled=true;socket.end(JSON.stringify({ok:false,error:'Coordinator frame exceeds its allocation'})+'\n');return;}
      const end=buffer.indexOf('\n');if(end<0)return;handled=true;
      let reply;
      try {
        const frame=JSON.parse(buffer.slice(0,end));
        if (!frame || typeof frame !== 'object' || frame.version !== VERSION || typeof frame.token !== 'string' || !HASH.test(frame.token) ||
            !crypto.timingSafeEqual(Buffer.from(frame.token),Buffer.from(token)) ||
            frame.scope !== controller.scope || frame.runtime_hash !== config.runtimeHash) throw new Error('Coordinator protocol, authentication or runtime binding failed');
        if (frame.op==='shutdown') {
          if (controller.active || (controller.queue.length || controller.pendingTasks()) && !controller.persistenceError) throw new Error('Coordinator has running, queued or waiting work');
          reply={ok:true,data:{stopping:true}};
          socket.end(JSON.stringify(reply)+'\n',() => {void close().catch(error => {process.stderr.write(safeError(error)+'\n');process.exitCode=1;});});return;
        }
        reply={ok:true,data:controller.rpc(frame)};
      } catch (error) {reply={ok:false,error:safeError(error),
        ...(error.code === 'COORDINATOR_REQUEST_MISSING' ? {code:error.code} : {})};}
      const raw=JSON.stringify(reply);
      if (Buffer.byteLength(raw)>MAX_FRAME) socket.end(JSON.stringify({ok:false,error:'Coordinator reply exceeds its allocation'})+'\n');
      else socket.end(raw+'\n');
    });
  });
  await new Promise((resolve,reject) => {server.once('error',reject);server.listen(0,'127.0.0.1',resolve);});
  try {
    atomicPrivate(endpointFile,{version:VERSION,port:server.address().port,pid:process.pid,instance_id:instance,token,
      runtime_hash:config.runtimeHash,scope:controller.scope},8192);
    // Only an idle service retires itself. Every saved result remains on disk.
    idleTimer=setInterval(() => {
      if (!controller.active && !controller.queue.length && !controller.pendingTasks() && Date.now()-controller.lastContact>15*60*1000) void close().catch(() => {});
    },30000);idleTimer.unref();
  } catch (error) {server.close();unlock();throw error;}
  return {server,controller,close};
}

if (require.main === module) {
  const args=process.argv.slice(2);
  if (args.length!==2||args[0]!=='--config') {process.stderr.write('Use --config <private coordinator configuration>\n');process.exitCode=1;}
  else start(args[1]).catch(error => {process.stderr.write(safeError(error)+'\n');process.exitCode=1;});
}
module.exports={VERSION,MAX_FRAME,MAX_REQUEST,MAX_RESPONSE,MAX_EVENTS,MAX_JOURNAL,MAX_JOBS,
  canonical,digestFor,scopeFor,readPrivate,atomicPrivate,Coordinator,start};

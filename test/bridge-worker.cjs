'use strict';
const assert = require('node:assert/strict');
const {EventEmitter} = require('node:events');
const {BridgeWorker, preventIdleSleep} = require('../bridge_worker');

function fixture() {
  const writes = [], signals = [], activities = [], children = [], power=[];
  const worker = new BridgeWorker({
    command:'python', args:[], options:{},
    keepAwake:()=>{const lease={released:false};power.push(lease);return ()=>{if(!lease.released){lease.released=true;lease.releases=(lease.releases||0)+1;}};},
    activity:onActivity => ({
      write:chunk => {activities.push(String(chunk));onActivity(String(chunk));},
      end(){}, close(){}
    }),
    spawn() {
      const child = new EventEmitter();
      child.stdout = new EventEmitter();
      child.stderr = new EventEmitter();
      child.stdin = new EventEmitter();
      child.stdin.write = raw => writes.push(JSON.parse(raw));
      child.kill = signal => {signals.push(signal);queueMicrotask(() => child.emit('close',null,signal));};
      children.push(child);
      return child;
    }
  });
  const reply = (value, end = true) => {
    children[0].stdout.emit('data', JSON.stringify(value)+'\n');
    if (end) children[0].stderr.emit('data', 'SUTRA_BRIDGE_REQUEST_END\n');
  };
  return {worker,writes,signals,activities,children,reply,power};
}

async function main() {
  const subject = fixture();
  assert.equal(subject.power.length,0,'An idle bridge must not prevent idle sleep');
  let settled = false;
  const first = subject.worker.send({op:'state'}, {SUTRA_THINKING:'on'});
  first.then(() => {settled=true;});
  assert.equal(subject.power.length,1);assert(!subject.power[0].released);
  await assert.rejects(subject.worker.send({op:'state'}, {}), /already running/);
  subject.reply({ok:true,data:{turn:1}}, false);
  await Promise.resolve();
  assert.equal(settled,false,'stdout must not finish the request before its activity stream');
  subject.children[0].stderr.emit('data','late reasoning frame\nSUTRA_BRIDGE_REQUEST_END\n');
  assert.deepEqual((await first).data,{turn:1});
  assert(subject.activities.includes('late reasoning frame\n'));
  assert(subject.power[0].released);assert.equal(subject.power[0].releases,1);

  const second = subject.worker.send({op:'state'}, {SUTRA_THINKING:'off',SUTRA_STRATA_PORT:undefined});
  subject.children[0].stderr.emit('data','SUTRA_BRIDGE_REQUEST_END\n');
  subject.children[0].stdout.emit('data','{"ok":true,"data":{"turn":2}}\n');
  assert.deepEqual((await second).data,{turn:2});
  assert.equal(subject.power.length,2);assert(subject.power.every(lease=>lease.released));
  assert.equal(subject.children.length,1,'ordinary requests must reuse one bridge process');
  assert.equal(subject.writes[1].environment.SUTRA_STRATA_PORT,null);
  assert.equal(subject.writes[1].environment.SUTRA_THINKING,'off');
  assert.equal(subject.writes.length,2);
  subject.worker.dispose();
  await Promise.resolve();

  const cancelled = fixture();
  let cancel;
  const pending = cancelled.worker.send({op:'work'}, {}, {token:{
    isCancellationRequested:false,onCancellationRequested(callback){cancel=callback;return {dispose(){}};}
  }});
  cancel();
  await assert.rejects(pending,/cancelled/);
  assert.deepEqual(cancelled.signals,['SIGTERM']);
  assert(cancelled.power[0].released);assert.equal(cancelled.power[0].releases,1);
  assert.equal(cancelled.writes.length,1,'cancelled work must not be replayed');
  await assert.rejects(cancelled.worker.send({op:'work'}, {}),/closed/);

  const failed = fixture();
  const broken = failed.worker.send({op:'execute'}, {});
  failed.reply({ok:false,error:'Interrupted action',fatal:true});
  await assert.rejects(broken,/Interrupted action/);
  assert.equal(failed.writes.length,1);
  assert(failed.worker.closed);assert(failed.power[0].released);
  await Promise.resolve();

  const malformed = fixture();
  const bad = malformed.worker.send({op:'state'}, {});
  malformed.children[0].stdout.emit('data','not JSON\n');
  await assert.rejects(bad,/Unexpected|JSON/);
  assert(malformed.worker.closed);assert(malformed.power[0].released);
  await Promise.resolve();
  const exited = fixture();
  const abandoned = exited.worker.send({op:'work'}, {});
  exited.children[0].emit('close',1,null);
  await assert.rejects(abandoned,/before replying/);
  assert(exited.power[0].released);

  const disposed = fixture();
  const interrupted = disposed.worker.send({op:'work'}, {});
  disposed.worker.dispose();await assert.rejects(interrupted,/stopped/);
  assert(disposed.power[0].released);assert.equal(disposed.power[0].releases,1);

  const expired = fixture();
  const setTimeoutReal=global.setTimeout;let watchdog;
  global.setTimeout=(callback,ms,...args)=>{
    if(ms===480000){watchdog=callback;return {unref(){}};}
    return setTimeoutReal(callback,ms,...args);
  };
  let timed;
  try {timed=expired.worker.send({op:'work'}, {});} finally {global.setTimeout=setTimeoutReal;}
  watchdog();await assert.rejects(timed,/deadline/);
  assert(expired.power[0].released);assert.equal(expired.power[0].releases,1);

  let launches=0;preventIdleSleep({platform:'linux',spawn(){launches++;}})();assert.equal(launches,0);
  const notes=[];
  preventIdleSleep({platform:'darwin',pid:123,spawn(){throw Error('not installed');},report:t=>notes.push(t)})();
  assert.equal(notes.length,1);assert(notes[0].includes('not installed'));
  const owned=new EventEmitter(),kills=[];owned.kill=signal=>kills.push(signal);owned.unref=()=>{};
  const release=preventIdleSleep({platform:'darwin',pid:123,spawn(command,args,options){
    assert.equal(command,'/usr/bin/caffeinate');assert.deepEqual(args,['-i','-w','123']);
    assert.equal(options.stdio,'ignore');assert.deepEqual(options.env,{PATH:'/usr/bin:/bin'});
    return owned;
  }});
  release();release();assert.deepEqual(kills,['SIGTERM']);
  const early=new EventEmitter();early.kill=()=>{throw Error('Ended assertion must not be killed twice');};
  const ended=preventIdleSleep({platform:'darwin',pid:123,spawn:()=>early,report:t=>notes.push(t)});
  early.emit('error',Error('not permitted'));ended();assert.equal(notes.length,2);

  console.log('Persistent bridge tests passed for reuse, per-request settings, ordered activity, cancellation, scoped idle-sleep protection and no replay.');
}

main().catch(error => {console.error(error);process.exitCode=1;});

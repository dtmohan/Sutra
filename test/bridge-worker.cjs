'use strict';
const assert = require('node:assert/strict');
const {EventEmitter} = require('node:events');
const {BridgeWorker} = require('../bridge_worker');

function fixture() {
  const writes = [], signals = [], activities = [], children = [];
  const worker = new BridgeWorker({
    command:'python', args:[], options:{},
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
  return {worker,writes,signals,activities,children,reply};
}

async function main() {
  const subject = fixture();
  let settled = false;
  const first = subject.worker.send({op:'state'}, {SUTRA_THINKING:'on'});
  first.then(() => {settled=true;});
  await assert.rejects(subject.worker.send({op:'state'}, {}), /already running/);
  subject.reply({ok:true,data:{turn:1}}, false);
  await Promise.resolve();
  assert.equal(settled,false,'stdout must not finish the request before its activity stream');
  subject.children[0].stderr.emit('data','late reasoning frame\nSUTRA_BRIDGE_REQUEST_END\n');
  assert.deepEqual((await first).data,{turn:1});
  assert(subject.activities.includes('late reasoning frame\n'));

  const second = subject.worker.send({op:'state'}, {SUTRA_THINKING:'off',SUTRA_STRATA_PORT:undefined});
  subject.children[0].stderr.emit('data','SUTRA_BRIDGE_REQUEST_END\n');
  subject.children[0].stdout.emit('data','{"ok":true,"data":{"turn":2}}\n');
  assert.deepEqual((await second).data,{turn:2});
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
  assert.equal(cancelled.writes.length,1,'cancelled work must not be replayed');
  await assert.rejects(cancelled.worker.send({op:'work'}, {}),/closed/);

  const failed = fixture();
  const broken = failed.worker.send({op:'execute'}, {});
  failed.reply({ok:false,error:'Interrupted action',fatal:true});
  await assert.rejects(broken,/Interrupted action/);
  assert.equal(failed.writes.length,1);
  assert(failed.worker.closed);
  await Promise.resolve();

  const malformed = fixture();
  const bad = malformed.worker.send({op:'state'}, {});
  malformed.children[0].stdout.emit('data','not JSON\n');
  await assert.rejects(bad,/Unexpected|JSON/);
  assert(malformed.worker.closed);
  await Promise.resolve();
  console.log('Persistent bridge tests passed for reuse, per-request settings, ordered activity, cancellation and no replay.');
}

main().catch(error => {console.error(error);process.exitCode=1;});

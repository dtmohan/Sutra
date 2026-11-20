'use strict';

// A request owns this lease; an idle persistent worker never does. -i leaves
// display sleep and explicit system sleep available. -w releases the assertion
// if the extension host exits unexpectedly before its normal cleanup can run.
function preventIdleSleep({spawn, platform = process.platform, pid = process.pid, report = () => {}}) {
  if (platform !== 'darwin') return () => {};
  let child, released = false;
  const note = error => {try {report('Idle-sleep protection unavailable: ' + error.message);} catch {}};
  try {
    child = spawn('/usr/bin/caffeinate', ['-i', '-w', String(pid)],
      {stdio:'ignore', env:{PATH:'/usr/bin:/bin'}});
    child.once('error', error => {released = true;note(error);});
    child.once('close', () => {released = true;});
    child.unref?.();
  } catch (error) {note(error);return () => {};}
  return () => {
    if (released) return;
    released = true;
    try {child.kill('SIGTERM');} catch (error) {note(error);}
  };
}

class BridgeWorker {
  constructor({spawn, command, args, options, activity, keepAwake = () => () => {}, onSpawn = () => {}, onClose = () => {}}) {
    this.pending = null;
    this.buffer = '';
    this.stderrBuffer = '';
    this.closed = false;
    this.exitTimer = null;
    this.activity = activity;
    this.keepAwake = keepAwake;
    this.onClose = onClose;
    this.child = spawn(command, args, options);
    onSpawn(this.child);
    this.child.stdout.on('data', chunk => this.receive(chunk));
    this.child.stderr.on('data', chunk => this.receiveStderr(chunk));
    this.child.stdin.on('error', error => this.fail(error));
    this.child.on('error', error => this.fail(error));
    this.child.on('close', (code, signal) => {
      this.closed = true;
      clearTimeout(this.exitTimer);
      this.onClose(this.child);
      this.finish(new Error(this.pending?.stopReason ||
        'Bridge worker exited before replying (code ' + code + ', signal ' + (signal || 'none') +
        '). Interrupted work will be reconciled on the next request.'));
    });
  }

  receiveStderr(chunk) {
    this.stderrBuffer += chunk;
    if (Buffer.byteLength(this.stderrBuffer) > 4_000_000) {
      this.fail(new Error('Bridge activity exceeds its transport allocation.'));
      return;
    }
    let end;
    while ((end = this.stderrBuffer.indexOf('\n')) >= 0) {
      const line = this.stderrBuffer.slice(0, end);
      this.stderrBuffer = this.stderrBuffer.slice(end + 1);
      if (!this.pending) continue;
      if (line === 'SUTRA_BRIDGE_REQUEST_END') {
        this.pending.stderrEnded = true;
        this.finishResponse();
      } else this.pending.events.write(line + '\n');
    }
  }

  finishResponse() {
    if (!this.pending?.response || !this.pending.stderrEnded) return;
    const response = this.pending.response;
    if (this.pending.stopReason) this.finish(new Error(this.pending.stopReason));
    else if (response.fatal) this.fail(new Error(response.error || 'Bridge worker interrupted.'));
    else this.finish(null, response);
  }

  receive(chunk) {
    this.buffer += chunk;
    if (Buffer.byteLength(this.buffer) > 4_000_000) {
      this.fail(new Error('Bridge worker response exceeds its transport allocation.'));
      return;
    }
    let end;
    while ((end = this.buffer.indexOf('\n')) >= 0) {
      const line = this.buffer.slice(0, end);
      this.buffer = this.buffer.slice(end + 1);
      if (!this.pending || !line.trim()) continue;
      let response;
      try {
        response = JSON.parse(line);
        if (typeof response?.ok !== 'boolean') throw new Error('Invalid bridge worker response.');
      } catch (error) {
        this.fail(error);
        return;
      }
      this.pending.response = response;
      this.finishResponse();
    }
  }

  finish(error, response) {
    const pending = this.pending;
    if (!pending) return;
    this.pending = null;
    clearTimeout(pending.timer);
    clearTimeout(pending.killTimer);
    pending.cancellation?.dispose();
    pending.releaseAwake?.();
    pending.events.end();
    pending.events.close();
    if (error) pending.reject(new Error(error.message + (pending.stderr ? '\n' + pending.stderr : '')));
    else {
      if (!response.ok && pending.stderr) response.error += '\n' + pending.stderr;
      pending.resolve(response);
    }
  }

  fail(error) {
    if (!this.closed) {
      this.closed = true;
      this.child.kill('SIGTERM');
      this.exitTimer = setTimeout(() => this.child.kill('SIGKILL'), 10000);
      this.exitTimer.unref?.();
      this.onClose(this.child);
    }
    this.finish(error);
  }

  send(request, environment, {token, onActivity = () => {}} = {}) {
    if (this.closed) return Promise.reject(new Error('Bridge worker is closed.'));
    if (this.pending) return Promise.reject(new Error('A bridge worker request is already running.'));
    if (token?.isCancellationRequested) return Promise.reject(new Error('Sutra request cancelled before dispatch.'));
    const values = Object.fromEntries(Object.entries(environment).map(([key, value]) => [key, value ?? null]));
    const raw = JSON.stringify({request, environment: values});
    if (Buffer.byteLength(raw) + 1 > 200000) return Promise.reject(new Error('Request exceeds its transport allocation.'));
    return new Promise((resolve, reject) => {
      const pending = {resolve, reject, events:null, stderr:'', stderrEnded:false, response:null, timer:null, killTimer:null,
        cancellation:null, stopReason:null, releaseAwake:null};
      pending.events = this.activity(onActivity, text => {pending.stderr = (pending.stderr + text).slice(-10000);});
      this.pending = pending;
      pending.releaseAwake = this.keepAwake();
      const cancel = reason => {
        if (this.pending !== pending || pending.stopReason) return;
        pending.stopReason = reason;
        pending.releaseAwake?.();
        // The signal interrupts this request's lifecycle.deadline context. The
        // worker exits, dropping RAM, and later handles reconcile durable work.
        this.closed = true;
        this.child.kill('SIGTERM');
        pending.killTimer = setTimeout(() => {
          this.child.kill('SIGKILL');
          this.finish(new Error(reason));
          this.onClose(this.child);
        }, 10000);
      };
      pending.timer = setTimeout(() => cancel(
        'Sutra bridge exceeded its deadline. Interrupted work will be reconciled.'), 480000);
      pending.cancellation = token?.onCancellationRequested(() => cancel(
        'Sutra request cancelled. Interrupted work will be reconciled.'));
      try {
        if (!pending.stopReason) this.child.stdin.write(raw + '\n');
      } catch (error) {
        this.fail(error);
      }
    });
  }

  dispose() {
    this.fail(new Error('Sutra bridge worker stopped. Durable work is retained for recovery.'));
  }
}

module.exports = {BridgeWorker, preventIdleSleep};

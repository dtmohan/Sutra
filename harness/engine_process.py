"""Sutra-owned, serialized token transport for a local native engine.

The worker owns tokenization, task state and admission. This module owns only
the child it starts and the READY / GEN / T / DONE pipe exchange. It imports no
Strata Control code and never discovers or ends another process by name/PID.
"""
import collections
import math
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import threading
import time


MAX_FRAME_BYTES = 65536
MAX_QUEUED_BYTES = 1024 * 1024
MAX_INPUT_TOKENS = 262144
MAX_OUTPUT_TOKENS = 262144
MAX_TOKEN_ID = 2**31 - 1
MAX_STDERR_BYTES = 32768
POLL_SECONDS = .05


class EngineError(RuntimeError):
    """An owned native process could not complete a trusted exchange."""


class EngineBusy(EngineError):
    """The process pipe already belongs to another operation."""


class EngineCancelled(EngineError):
    """An owned startup was cancelled before generation became ready."""


class EngineProtocolError(EngineError):
    """The stream cannot safely be handed to another request."""


class EngineTimeout(TimeoutError):
    """A bounded exchange expired; engine_result records a drained request."""


def _integer(value, name, lower=0, upper=MAX_TOKEN_ID):
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError('Invalid '+name)
    return value


def _number(value, name, lower=0., upper=1e12):
    if type(value) not in (int, float) or not math.isfinite(value) or not lower <= value <= upper:
        raise ValueError('Invalid '+name)
    return float(value)


def _text(value, name, maximum=4096):
    if (not isinstance(value, str) or not value or len(value) > maximum or
            any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError('Invalid '+name)
    return value


def _tokens(values, name, maximum=MAX_INPUT_TOKENS):
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= maximum:
        raise ValueError('Invalid '+name)
    return [_integer(value, name+' token') for value in values]


def parse_done(line):
    """Parse the native positional dialect, retaining bounded extra metrics."""
    fields = line.split()
    if len(fields) < 6 or fields[0] != 'DONE':
        raise EngineProtocolError('Incomplete native DONE frame')
    try:
        result = {
            'generated': _integer(int(fields[1]), 'generated count', upper=MAX_OUTPUT_TOKENS),
            'prompt_tokens': _integer(int(fields[2]), 'prompt count', upper=MAX_INPUT_TOKENS),
            'prompt_ms': _number(float(fields[3]), 'prompt milliseconds'),
            'decode_ms': _number(float(fields[4]), 'decode milliseconds'),
        }
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', fields[5]):
            raise ValueError('Invalid native finish reason')
        result['finish'] = fields[5]
        names = ('drafts_accepted', 'drafts_offered', 'reused', 'hits', 'lookups',
                 'ram_blobs', 'file_blobs', 'file_mb', 'prompt_read', 'offloaded')
        index = 6
        for name in names:
            if index >= len(fields) or '=' in fields[index]:
                break
            raw = fields[index]
            result[name] = (_number(float(raw), name) if name == 'file_mb' else
                            _integer(int(raw), name, upper=2**53-1))
            index += 1
        for raw in fields[index:]:
            key, separator, value = raw.partition('=')
            if (not separator or not re.fullmatch(r'[a-z][a-z0-9_]{0,47}', key) or
                    key in result or len(value) > 80):
                raise ValueError('Invalid native DONE extension')
            if re.fullmatch(r'\d+', value):
                result[key] = _integer(int(value), key, upper=2**53-1)
            else:
                result[key] = _number(float(value), key)
        if result['finish'] == 'nonfinite':
            raise ValueError('Native generation reported non-finite logits')
        return result
    except (ValueError, OverflowError) as error:
        raise EngineProtocolError(str(error)+'; native DONE: '+line[:512]) from None


class _Stream:
    def __init__(self):
        self.lines = queue.Queue(maxsize=8192)
        self.lock = threading.Lock()
        self.queued_bytes = 0
        self.error = None
        self.stderr = collections.deque()
        self.stderr_bytes = 0

    def put(self, value):
        size = len(value) if isinstance(value, bytes) else 0
        with self.lock:
            if self.error is not None:
                return False
            if self.queued_bytes + size > MAX_QUEUED_BYTES:
                self.error = EngineProtocolError('Native output queue exceeds its byte allocation')
                return False
            try:
                self.lines.put_nowait(value)
            except queue.Full:
                self.error = EngineProtocolError('Native output queue exceeds its frame allocation')
                return False
            self.queued_bytes += size
        return True

    def log(self, raw):
        with self.lock:
            self.stderr.append(raw)
            self.stderr_bytes += len(raw)
            while self.stderr_bytes > MAX_STDERR_BYTES and self.stderr:
                self.stderr_bytes -= len(self.stderr.popleft())


class EngineProcess:
    def __init__(self, profile):
        if not isinstance(profile, dict):
            raise ValueError('Native engine profile must be an object')
        executable = _text(profile.get('executable') or profile.get('exe'), 'engine executable')
        if not Path(executable).is_absolute():
            raise ValueError('Native engine executable must be absolute')
        args = profile.get('args', [])
        if not isinstance(args, (list, tuple)) or len(args) > 256:
            raise ValueError('Invalid native engine arguments')
        self.args = [_text(arg, 'engine argument') for arg in args]
        if any(arg in ('--serve', '--batch', '--slots', '--parallel') for arg in self.args):
            raise ValueError('The Sutra worker owns one serialized --serve stream')
        cwd = profile.get('cwd') or str(Path(executable).parent)
        if not Path(_text(cwd, 'engine working directory')).is_absolute():
            raise ValueError('Native engine working directory must be absolute')
        overrides = profile.get('env', profile.get('environment', {}))
        if not isinstance(overrides, dict) or len(overrides) > 128:
            raise ValueError('Invalid native engine environment')
        self.environment = dict(os.environ)
        for key, value in overrides.items():
            if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,127}', str(key)):
                raise ValueError('Invalid native engine environment name')
            self.environment[key] = _text(value, 'engine environment value')
        self.executable, self.cwd = executable, cwd
        expected = profile.get('context_tokens')
        self.expected_context = None if expected is None else _integer(expected, 'context allocation', 1, 1048576)
        self.probe_tokens = profile.get('probe_tokens')
        if self.probe_tokens is not None:
            self.probe_tokens = _tokens(self.probe_tokens, 'readiness probe')
        self.drain_timeout = _number(profile.get('drain_timeout', 10), 'drain timeout', .01, 300)
        self.shutdown_timeout = _number(profile.get('shutdown_timeout', 3), 'shutdown timeout', .01, 30)
        self._operation = threading.RLock()
        self._shutdown = threading.Event()
        self._state_lock = threading.Lock()
        self._proc = None
        self._stream = None
        self._threads = []
        self._ready = False
        self._generation_ready = False
        self._poisoned = False
        self._context = 0
        self._capabilities = set()
        self._info = {}
        self._last = None
        self._started_at = None
        self._generating = False

    @staticmethod
    def _stdout(proc, stream):
        try:
            while True:
                raw = proc.stdout.readline(MAX_FRAME_BYTES + 1)
                if not raw:
                    stream.put(None)
                    return
                if len(raw) > MAX_FRAME_BYTES or not raw.endswith(b'\n'):
                    stream.error = EngineProtocolError('Native stdout frame is oversized or truncated')
                    return
                if not stream.put(raw):
                    return
        except (OSError, ValueError) as error:
            stream.error = EngineError('Native stdout closed: '+str(error)[:160])

    @staticmethod
    def _stderr(proc, stream):
        try:
            while True:
                raw = proc.stderr.read(4096)
                if not raw:
                    return
                stream.log(raw)
        except (OSError, ValueError):
            return

    def metadata(self):
        proc, stream = self._proc, self._stream
        if stream is not None:
            with stream.lock:
                tail = b''.join(stream.stderr).decode('utf-8', 'replace')
        else:
            tail = ''
        with self._state_lock:
            return {
                'pid': proc.pid if proc is not None else None,
                'running': proc is not None and proc.poll() is None,
                'ready': self._ready and not self._poisoned,
                'generation_ready': self._generation_ready and not self._poisoned,
                'context_tokens': self._context,
                'capabilities': sorted(self._capabilities),
                'info': dict(self._info), 'last_metrics': dict(self._last) if self._last else None,
                'started_at': self._started_at, 'stderr_tail': tail,
                'owned': proc is not None, 'poisoned': self._poisoned,
                'busy': self._generating,
            }

    def _line(self, deadline, cancel_event=None, startup=False):
        stream = self._stream
        if stream is None:
            raise EngineError('The native process has no response stream')
        while True:
            if (cancel_event is not None and cancel_event.is_set()) or (startup and self._shutdown.is_set()):
                raise EngineCancelled('Native startup was cancelled')
            if stream.error is not None:
                raise stream.error
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EngineTimeout('Native engine exchange exceeded its deadline')
            try:
                raw = stream.lines.get(timeout=min(POLL_SECONDS, remaining))
            except queue.Empty:
                continue
            with stream.lock:
                if isinstance(raw, bytes):
                    stream.queued_bytes -= len(raw)
            if raw is None:
                raise EngineError('Native engine exited before completing the exchange')
            try:
                line = raw.decode('utf-8', 'strict').rstrip('\r\n')
            except UnicodeDecodeError:
                raise EngineProtocolError('Native engine emitted invalid UTF-8') from None
            if any(ord(char) < 32 for char in line):
                raise EngineProtocolError('Native engine emitted control characters')
            return line

    def _write(self, line):
        proc = self._proc
        if proc is None or proc.poll() is not None:
            raise EngineError('Native engine is not running')
        try:
            proc.stdin.write(line.encode('ascii') + b'\n')
            proc.stdin.flush()
        except (OSError, ValueError):
            raise EngineError('Native engine input pipe closed') from None

    def _info_line(self, line):
        fields = line.split()
        if fields[0] != 'INFO' or len(fields) > 128:
            raise EngineProtocolError('Invalid native INFO frame')
        with self._state_lock:
            for field in fields[1:]:
                key, separator, value = field.partition('=')
                if not separator or not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9.-]{0,63}', key) or len(value) > 256:
                    raise EngineProtocolError('Invalid native INFO metadata')
                if len(self._info) >= 128 and key not in self._info:
                    raise EngineProtocolError('Native INFO exceeds its allocation')
                self._info[key] = int(value) if re.fullmatch(r'-?\d{1,15}', value) else value

    def start(self, probe_token_ids=None, timeout=120, cancel_event=None):
        timeout = _number(timeout, 'startup timeout', .01, 900)
        probe = self.probe_tokens if probe_token_ids is None else _tokens(probe_token_ids, 'readiness probe')
        if probe is not None and self.expected_context is not None and len(probe)+1 > self.expected_context:
            raise ValueError('Readiness probe exceeds the native context allocation')
        if cancel_event is not None and cancel_event.is_set():
            raise EngineCancelled('Native startup was cancelled before loading')
        if not self._operation.acquire(blocking=False):
            raise EngineBusy('The native engine is handling another operation')
        try:
            if cancel_event is not None and cancel_event.is_set():
                raise EngineCancelled('Native startup was cancelled before loading')
            if self._proc is not None and self._proc.poll() is None:
                if self._ready and not self._poisoned:
                    if probe is not None and not self._generation_ready:
                        result = self.generate(probe, 1, timeout=timeout, cancel_event=cancel_event)
                        if result['cancelled'] or result['metrics']['generated'] != 1:
                            raise EngineCancelled('Native generation readiness probe was cancelled')
                        self._generation_ready = True
                    return self.metadata()
                raise EngineError('Stop the unready native process before loading again')
            self._close_owned()
            self._shutdown.clear()
            with self._state_lock:
                self._ready = self._generation_ready = self._poisoned = False
                self._context = 0
                self._capabilities = set()
                self._info = {}
                self._last = None
            deadline = time.monotonic() + timeout
            options = {'cwd': self.cwd, 'env': self.environment, 'stdin': subprocess.PIPE,
                       'stdout': subprocess.PIPE, 'stderr': subprocess.PIPE, 'bufsize': 0}
            if os.name != 'nt':
                options['start_new_session'] = True
            self._proc = subprocess.Popen([self.executable, '--serve', *self.args], **options)
            self._stream = _Stream()
            self._started_at = time.time()
            self._threads = [threading.Thread(target=self._stdout, args=(self._proc, self._stream), daemon=True),
                             threading.Thread(target=self._stderr, args=(self._proc, self._stream), daemon=True)]
            for thread in self._threads:
                thread.start()
            startup_frames = 0
            while True:
                if self._shutdown.is_set():
                    raise EngineError('Native startup was cancelled')
                line = self._line(deadline, cancel_event, startup=True)
                startup_frames += 1
                if startup_frames > 512:
                    raise EngineProtocolError('Native startup exceeds its frame allocation')
                if line.startswith('INFO '):
                    self._info_line(line)
                    continue
                if line.startswith('ERR'):
                    raise EngineError('Native startup refused: '+line[3:].strip()[:600])
                fields = line.split()
                if len(fields) < 2 or fields[0] != 'READY' or len(fields) > 8:
                    raise EngineProtocolError('Expected native READY handshake')
                try:
                    context = _integer(int(fields[1]), 'native context', 1, 1048576)
                except ValueError as error:
                    raise EngineProtocolError(str(error)) from None
                if len(set(fields[2:])) != len(fields[2:]) or any(not re.fullmatch('[a-z][a-z0-9_]{0,31}', value) for value in fields[2:]):
                    raise EngineProtocolError('Invalid native READY capabilities')
                if self.expected_context is not None and context != self.expected_context:
                    raise EngineProtocolError('Native READY context differs from the selected profile')
                if type(self._info.get('batch_slots')) is int and self._info['batch_slots'] > 0:
                    raise EngineProtocolError('The Sutra worker requires a serialized native engine')
                with self._state_lock:
                    self._context, self._capabilities, self._ready = context, set(fields[2:]), True
                break
            if probe is not None:
                result = self.generate(probe, 1, timeout=max(.01, deadline-time.monotonic()), cancel_event=cancel_event)
                if result['cancelled'] or result['metrics']['generated'] != 1:
                    raise EngineError('Native generation readiness probe did not complete')
                self._generation_ready = True
            return self.metadata()
        except BaseException:
            self._poisoned = True
            self._close_owned()
            raise
        finally:
            self._operation.release()

    def generate(self, token_ids, max_tokens, *, temperature=0, top_p=None, top_k=None,
                 seed=42, on_token=None, on_progress=None, cancel_event=None, timeout=150,
                 stop_tokens=None, hold=None):
        ids = _tokens(token_ids, 'prompt')
        maximum = _integer(max_tokens, 'output allocation', 1, MAX_OUTPUT_TOKENS)
        duration = _number(timeout, 'generation timeout', .01, 3600)
        sampling = []
        temperature = _number(temperature, 'temperature', 0, 100)
        if temperature:
            sampling.append('temperature='+repr(temperature))
        if top_p is not None:
            sampling.append('top_p='+repr(_number(top_p, 'top_p', .000001, 1)))
        if top_k is not None:
            sampling.append('top_k='+str(_integer(top_k, 'top_k', 0, 64)))
        if seed is not None:
            sampling.append('seed='+str(_integer(seed, 'seed', 1, 2**53-1)))
        terminal = set() if stop_tokens is None or stop_tokens == [] or stop_tokens == () else set(_tokens(stop_tokens, 'stop tokens', 64))
        if hold is not None:
            if not isinstance(hold, (list, tuple)) or len(hold) != 2:
                raise ValueError('Invalid reasoning hold')
            count = _integer(hold[0], 'reasoning hold count', 1, MAX_OUTPUT_TOKENS)
            token = _integer(hold[1], 'reasoning hold token')
            sampling.append('hold='+str(count)+':'+str(token))
        if not self._operation.acquire(blocking=False):
            raise EngineBusy('The native engine is handling another operation')
        sent = False
        try:
            if self._generating:
                raise EngineBusy('The native stream cannot be reentered by a callback')
            if not self._ready or self._poisoned or self._proc is None or self._proc.poll() is not None:
                raise EngineError('Load a ready native process before generation')
            if len(ids) + maximum > self._context:
                raise ValueError('Tokenized prompt and output exceed the native context allocation')
            if hold is not None and 'hold' not in self._capabilities:
                raise ValueError('This native engine does not advertise reasoning hold support')
            if cancel_event is not None and cancel_event.is_set():
                return {'tokens': [], 'metrics': {}, 'finish': 'cancelled', 'cancelled': True,
                        'elapsed_seconds': 0., 'prompt_tokens': len(ids), 'reused_tokens': 0,
                        'request_sent': False}
            self._generating = True
            started = time.monotonic()
            deadline = started + duration
            sent = True
            self._write('GEN '+str(maximum)+(' '+' '.join(sampling) if sampling else '')+' '+','.join(map(str, ids)))
            tokens, reused, stopped, interrupted, callback_error = [], 0, False, None, None
            metrics = None
            while metrics is None:
                now = time.monotonic()
                cancel = self._shutdown.is_set() or (cancel_event is not None and cancel_event.is_set())
                if interrupted is None and (cancel or now >= deadline):
                    interrupted = 'cancelled' if cancel else 'timeout'
                if interrupted and not stopped:
                    if 'stop' not in self._capabilities:
                        raise EngineProtocolError('Native engine cannot safely cancel this request')
                    self._write('STOP')
                    stopped = True
                    deadline = now + self.drain_timeout
                if interrupted and now >= deadline:
                    raise EngineProtocolError('Native engine did not drain DONE after STOP')
                # A short read allows cancellation to be noticed during prefill.
                try:
                    line = self._line(min(deadline, now + POLL_SECONDS))
                except EngineTimeout:
                    continue
                if line.startswith('T '):
                    fields = line.split()
                    try:
                        if len(fields) != 2:
                            raise ValueError('Invalid native token frame')
                        token = _integer(int(fields[1]), 'native token')
                    except ValueError as error:
                        raise EngineProtocolError(str(error)) from None
                    tokens.append(token)
                    if len(tokens) > maximum:
                        raise EngineProtocolError('Native generation exceeded its output allocation')
                    if interrupted is None and on_token is not None:
                        try:
                            on_token(token)
                        except BaseException as error:
                            callback_error, interrupted = error, 'callback_error'
                    if token in terminal and interrupted is None:
                        interrupted = 'stop_token'
                elif line.startswith('PP '):
                    fields = line.split()
                    try:
                        if not 3 <= len(fields) <= 5:
                            raise ValueError('Invalid native prompt progress')
                        read = _integer(int(fields[1]), 'prompt progress', upper=len(ids))
                        total = _integer(int(fields[2]), 'prompt progress total', upper=len(ids))
                        if read > total:
                            raise ValueError('Native prompt progress is inconsistent')
                        progress = {'read': read, 'total': total}
                        if len(fields) >= 4:
                            progress['milliseconds'] = _number(float(fields[3]), 'prefill milliseconds')
                        if len(fields) == 5:
                            progress['tokens_per_second'] = _number(float(fields[4]), 'prefill rate')
                    except ValueError as error:
                        raise EngineProtocolError(str(error)) from None
                    if interrupted is None and on_progress is not None:
                        try:
                            on_progress(progress)
                        except BaseException as error:
                            callback_error, interrupted = error, 'callback_error'
                elif line.startswith(('RESUME ', 'REUSED ')):
                    fields = line.split()
                    try:
                        if len(fields) != 2:
                            raise ValueError('Invalid native reuse count')
                        reused = _integer(int(fields[1]), 'native reuse count', upper=len(ids))
                    except ValueError as error:
                        raise EngineProtocolError(str(error)) from None
                elif line.startswith('LP '):
                    if len(line) > 8192 or not tokens:
                        raise EngineProtocolError('Invalid native token probability frame')
                    # Token probabilities are observe-only diagnostics. The host
                    # never treats their content as authority or request data.
                elif line.startswith('INFO '):
                    self._info_line(line)
                elif line.startswith('DONE '):
                    metrics = parse_done(line)
                    if metrics['generated'] != len(tokens) or metrics['prompt_tokens'] > len(ids):
                        raise EngineProtocolError('Native DONE counts differ from the observed stream: '+
                                                  str(metrics['generated'])+' reported / '+str(len(tokens))+
                                                  ' observed output, '+str(metrics['prompt_tokens'])+
                                                  ' reported / '+str(len(ids))+' submitted input')
                    if metrics.get('reused', reused) > len(ids) or metrics.get('prompt_read', 0) > len(ids):
                        raise EngineProtocolError('Native DONE prompt counts exceed the input allocation')
                elif line.startswith('ERR'):
                    # Some engine versions print DONE after ERR and some do not.
                    # Ending our owned child removes that ambiguous tail before
                    # another request can claim this pipe.
                    raise EngineProtocolError('Native engine rejected the request: '+line[3:].strip()[:600])
                else:
                    raise EngineProtocolError('Unexpected native generation frame: '+line[:120])
            result = {'tokens': tokens, 'metrics': metrics,
                      'finish': interrupted or metrics['finish'],
                      'cancelled': interrupted in ('cancelled', 'timeout', 'callback_error'),
                      'elapsed_seconds': time.monotonic()-started,
                      'prompt_tokens': len(ids), 'reused_tokens': metrics.get('reused', reused),
                      'request_sent': True}
            self._last = metrics
            if interrupted is None and metrics['generated'] > 0:
                self._generation_ready = True
            if interrupted == 'timeout':
                error = EngineTimeout('Native generation timed out; STOP and DONE were reconciled')
                error.engine_result = result
                raise error
            if callback_error is not None:
                try:
                    callback_error.engine_result = result
                except (AttributeError, TypeError):
                    pass
                raise callback_error
            return result
        except BaseException as error:
            # A drained timeout/callback error leaves a synchronized stream.
            if sent and getattr(error, 'engine_result', None) is None:
                self._poisoned = True
                self._close_owned()
            raise
        finally:
            if sent:
                self._generating = False
            self._operation.release()

    def _close_owned(self):
        proc = self._proc
        self._ready = self._generation_ready = False
        if proc is None:
            return
        if proc.poll() is None:
            try:
                if os.name != 'nt' and os.getpgid(proc.pid) == proc.pid:
                    os.killpg(proc.pid, signal.SIGTERM)
                else:
                    proc.terminate()
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=self.shutdown_timeout)
            except subprocess.TimeoutExpired:
                try:
                    if os.name != 'nt' and os.getpgid(proc.pid) == proc.pid:
                        os.killpg(proc.pid, signal.SIGKILL)
                    else:
                        proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=self.shutdown_timeout)
                except subprocess.TimeoutExpired:
                    raise EngineError('The owned native child has not confirmed shutdown') from None
        # Once the child has exited, let the readers collect its final error
        # lines before closing their handles. Every reader is tied to this
        # process's own stream object, never to a replacement process.
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=.5)
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=.1)
        self._proc = None
        self._threads = []

    def stop(self):
        self._shutdown.set()
        with self._operation:
            self._close_owned()
            return self.metadata()

    close = stop

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.stop()

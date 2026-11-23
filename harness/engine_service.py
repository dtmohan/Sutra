"""Sutra-owned local engine service. No Strata Control or Ollama imports."""
import argparse
from contextlib import contextmanager
import hmac
import json
import os
from pathlib import Path
import secrets
import select
import signal
import socket
import sys
import threading
import time
import uuid

from engine_client import FRAME_LIMIT, VERSION, runtime_hash, socket_path, signed, verified, progress_frame, read_line
from engine_profiles import load
from platform_support import lock_file


class WorkerError(RuntimeError):
    def __init__(self, message, status='failed', decision=None):
        super().__init__(message)
        self.status, self.decision = status, decision


@contextmanager
def _socket_lifetime(endpoint):
    try:
        yield
    finally:
        endpoint.unlink(missing_ok=True)
        try:
            endpoint.parent.rmdir()
        except OSError:
            pass


class EngineWorker:
    def __init__(self, directory, registry, *, process_factory=None, codec_factory=None, admission=None):
        if process_factory is None:
            from engine_process import EngineProcess
            process_factory = EngineProcess
        if codec_factory is None:
            from engine_codec import EngineCodec
            codec_factory = EngineCodec
        if admission is None:
            from provider_startup import admission
        self.directory, self.registry = Path(directory), Path(registry)
        self.process_factory, self.codec_factory, self.admission = process_factory, codec_factory, admission
        self.lock = threading.Lock()
        self.engine = self.codec = self.profile = self.instance_id = None
        self.loading_engine = None
        self.phase = 'idle'
        self.owner_id = uuid.uuid4().hex
        self.last_generation = None

    def status(self):
        metadata = self.engine.metadata() if self.engine is not None else {}
        return {'ok': True, 'running': True, 'busy': self.lock.locked(), 'phase': self.phase,
                'owner_id': self.owner_id, 'profile': self.profile.get('name', self.profile.get('ref')) if self.profile else None,
                'profile_hash': self.profile.get('profile_hash') if self.profile else None,
                'context_tokens': self.profile.get('context_tokens') if self.profile else None,
                'instance_id': self.instance_id, 'pid': metadata.get('pid'),
                'engine': metadata, 'last_generation': self.last_generation}

    def _current(self, request):
        profile = load(request['ref'], path=self.registry)
        if profile['profile_hash'] != request.get('profile_hash'):
            raise WorkerError('The engine profile changed after host admission', 'changed')
        return profile

    def _admit(self, profile=None):
        cold = profile.get('cold_memory_bytes') if profile else None
        result = self.admission(cold, self.directory, native_fit=cold is None)
        if not result.get('admitted'):
            error = WorkerError(result.get('reason', 'Engine memory admission is unavailable'),
                                'paused', result.get('decision'))
            hardware = result.get('hardware') or {}
            error.blocking_measurement = {key: result.get(key) for key in
                ('at', 'available_bytes', 'cold_memory_bytes', 'system_reserve_bytes', 'native_fit_only')}
            error.blocking_measurement.update(hardware_decision=hardware.get('decision'),
                hardware_reason=hardware.get('reason'), sample=hardware.get('sample'))
            raise error
        return result

    def _stop(self):
        if self.loading_engine is not None:
            self.loading_engine.stop()
        if self.engine is not None:
            self.engine.stop()
        self.engine = self.codec = self.profile = self.instance_id = None

    def _load(self, request, cancel):
        if cancel.is_set():
            raise WorkerError('Engine loading was cancelled before dispatch', 'cancelled')
        profile = self._current(request)
        if self.profile and self.profile['profile_hash'] == profile['profile_hash']:
            state = self.engine.metadata()
            if state.get('running') and state.get('generation_ready'):
                self._admit()
                return dict(self.status(), busy=False)
            if state.get('running') and not state.get('poisoned'):
                raise WorkerError('The current engine lost generation readiness', 'uncertain')
        # Only this worker's idle child can be released. The caller holds the
        # cross-window OS slot and this worker holds its own operation lock.
        self.phase = 'unloading'
        self._stop()
        self.phase = 'idle'
        admission = self._admit(profile)
        self.phase = 'loading'
        done = threading.Event()
        observed = []

        def monitor():
            while not done.wait(1.5):
                try:
                    self._admit()
                except Exception as error:
                    observed.append(error)
                    cancel.set()
                    return

        watcher = threading.Thread(target=monitor, daemon=True)
        watcher.start()
        engine = None
        try:
            codec = self.codec_factory(profile['tokenizer_path'])
            probe = codec.encode_messages([{'role': 'user', 'content': 'Hello'}], thinking=False)
            engine = self.process_factory(dict(profile['engine'], context_tokens=profile['context_tokens']))
            self.loading_engine = engine
            engine.start(probe_token_ids=probe, timeout=120, cancel_event=cancel)
            if observed:
                raise observed[0]
            if cancel.is_set():
                raise WorkerError('Engine loading was cancelled; the owned child was stopped', 'cancelled')
            if self._current(request)['profile_hash'] != profile['profile_hash']:
                raise WorkerError('Engine artifacts changed while loading', 'changed')
            self._admit()
            if not engine.metadata().get('generation_ready'):
                raise WorkerError('The engine did not verify a generation probe', 'uncertain')
            self.engine, self.codec, self.profile = engine, codec, profile
            self.instance_id = uuid.uuid4().hex
            self.phase = 'idle'
            return dict(self.status(), busy=False, admission=admission)
        except Exception:
            if engine is not None:
                engine.stop()
            self.phase = 'idle'
            if observed:
                raise observed[0]
            raise
        finally:
            self.loading_engine = None
            done.set()
            watcher.join(timeout=3)

    def _generate(self, request, emit, cancel):
        profile = self._current(request)
        if not self.profile or self.profile['profile_hash'] != profile['profile_hash']:
            raise WorkerError('Load the admitted engine profile before generation', 'changed')
        if request.get('expected_instance_id') != self.instance_id:
            raise WorkerError('The engine instance changed before generation', 'changed')
        messages, maximum = request.get('messages'), request.get('max_tokens')
        context = request.get('context_tokens')
        if context is None:
            context = profile['context_tokens']
        thinking = request.get('thinking')
        if (not isinstance(messages, list) or not 1 <= len(messages) <= 128 or
                type(maximum) is not int or not 1 <= maximum <= 65536 or
                type(context) is not int or not 1 <= context <= profile['context_tokens'] or
                (thinking is not None and type(thinking) is not bool) or not isinstance(request.get('schema'), dict)):
            raise WorkerError('Invalid engine generation allocation')
        for message in messages:
            if (not isinstance(message, dict) or message.get('role') not in ('system', 'user', 'assistant') or
                    not isinstance(message.get('content'), str)):
                raise WorkerError('Invalid engine message')
        self._admit()
        tokens = self.codec.encode_messages(messages, thinking=thinking, json_schema=request['schema'])
        if len(tokens) + maximum > context:
            raise WorkerError('Actual tokenized prompt and output reserve exceed the admitted context allocation')
        decoder = self.codec.parser(thinking=thinking)
        self.phase = 'generating'
        self.last_generation = None
        started = time.monotonic()
        generated = []

        def token(value):
            generated.append(value)
            output = decoder.feed(value)
            if output.get('thinking'):
                emit('thinking', text=output['thinking'])

        try:
            result = self.engine.generate(tokens, maximum, temperature=0, seed=42,
                on_token=token, on_progress=lambda progress: emit('progress', data=progress),
                cancel_event=cancel, timeout=150, stop_tokens=sorted(self.codec.stop_token_ids))
            final = decoder.finish()
            if final.get('thinking'):
                emit('thinking', text=final['thinking'])
            response = decoder.response()
            if self._current(request)['profile_hash'] != profile['profile_hash']:
                raise WorkerError('Engine artifacts changed during generation', 'changed')
            metrics = result.get('metrics') or {}
            count = metrics.get('generated', len(result.get('tokens', generated)))
            if type(count) is not int or not len(generated) <= count <= maximum:
                raise WorkerError('Native output counts exceed the observed generation allocation')
            native_finish = result.get('finish')
            if result.get('cancelled'):
                finish = 'cancelled'
            elif response.get('stopped') or native_finish in ('stop', 'eos', 'eot', 'stop_token'):
                finish = 'stop'
            elif native_finish in ('length', 'limit', 'max_tokens') or count >= maximum:
                finish = 'length'
            else:
                raise WorkerError('The native engine did not report a known completion reason')
            self.last_generation = {'prompt_tokens': len(tokens), 'completion_tokens': count,
                                    'finish_reason': finish, 'elapsed_s': time.monotonic() - started}
            return dict(self.last_generation, ok=True, content=response['content'], thinking=response['thinking'],
                        instance_id=self.instance_id, profile_hash=profile['profile_hash'], timings=metrics)
        except BaseException as error:
            partial = getattr(error, 'engine_result', None)
            if isinstance(partial, dict):
                metrics = partial.get('metrics') or {}
                self.last_generation = {'prompt_tokens': len(tokens),
                    'completion_tokens': metrics.get('generated', len(partial.get('tokens', []))),
                    'finish_reason': 'cancelled' if partial.get('cancelled') else 'error',
                    'elapsed_s': time.monotonic() - started}
            raise
        finally:
            self.phase = 'idle'

    def operation(self, request, emit, cancel):
        if request.get('op') in ('ping', 'status'):
            return self.status()
        if not self.lock.acquire(blocking=False):
            raise WorkerError('The Sutra engine worker has an active operation', 'busy')
        try:
            if request.get('op') == 'load':
                return self._load(request, cancel)
            if request.get('op') == 'generate':
                return self._generate(request, emit, cancel)
            if request.get('op') in ('unload', 'shutdown'):
                self.phase = 'unloading'
                self._stop()
                self.phase = 'idle'
                return self.status()
            raise WorkerError('Unsupported engine operation')
        finally:
            self.lock.release()


def serve(directory, registry, *, worker=None, ready=None, stop=None):
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    stop = stop or threading.Event()
    owner = secrets.token_hex(32)
    endpoint = socket_path(directory)
    worker = worker or EngineWorker(directory, registry)
    code = runtime_hash()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    record = directory / 'worker.json'
    with _socket_lifetime(endpoint), lock_file(directory / 'owner.lock'):
        if endpoint.exists():
            endpoint.unlink()
        server.bind(str(endpoint))
        os.chmod(endpoint, 0o600)
        server.listen(8)
        server.settimeout(.25)
        document = {'version': VERSION, 'token': owner, 'pid': os.getpid(),
                    'socket': str(endpoint), 'runtime_hash': code, 'registry': str(registry),
                    'python': sys.executable}
        temporary = directory / ('worker-' + uuid.uuid4().hex + '.tmp')
        descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(document, stream)
        temporary.replace(record)

        nonces = {}
        nonce_lock = threading.Lock()

        def connection(peer, cancel):
            ended = threading.Event()
            write_lock = threading.Lock()
            stream = None
            nonce = None
            sequence = 0
            watcher = None
            write_failed = False

            def emit(event, **fields):
                nonlocal sequence, write_failed
                if nonce is None:
                    raise WorkerError('The request was not authenticated')
                if event == 'progress':
                    progress_frame(fields.get('data'))
                with write_lock:
                    if write_failed:
                        raise WorkerError('The engine response connection was interrupted')
                    value = signed(dict(event=event, nonce=nonce, seq=sequence, **fields), owner)
                    frame = json.dumps(value, separators=(',', ':'), ensure_ascii=False,
                                       allow_nan=False).encode() + b'\n'
                    if len(frame) > FRAME_LIMIT:
                        raise WorkerError('Engine result exceeds its transport allocation')
                    try:
                        peer.sendall(frame)
                    except OSError:
                        write_failed = True
                        raise
                    sequence += 1

            def disconnected():
                while not ended.wait(.1):
                    try:
                        readable, _, _ = select.select([peer], [], [], .1)
                        if readable and not peer.recv(1, socket.MSG_PEEK):
                            cancel.set()
                            return
                    except (OSError, ValueError):
                        cancel.set()
                        return

            try:
                peer.settimeout(5)
                raw = read_line(peer, bytearray(), time.monotonic() + 5)
                if len(raw) > FRAME_LIMIT or not raw.endswith(b'\n'):
                    raise WorkerError('Invalid bounded engine request')
                envelope = verified(json.loads(raw), owner)
                if (set(envelope) != {'version', 'nonce', 'sent_at', 'request'} or type(envelope.get('version')) is not int or envelope.get('version') != VERSION or
                        not isinstance(envelope.get('nonce'), str) or len(envelope['nonce']) != 32 or
                        any(char not in '0123456789abcdef' for char in envelope['nonce']) or
                        type(envelope.get('sent_at')) is not int or abs(time.time() - envelope['sent_at']) > 60 or
                        not isinstance(envelope.get('request'), dict)):
                    raise WorkerError('Engine worker authentication failed', 'denied')
                nonce = envelope['nonce']
                with nonce_lock:
                    now = time.monotonic()
                    for key in [key for key, expiry in nonces.items() if expiry < now]:
                        del nonces[key]
                    if nonce in nonces or len(nonces) >= 2048:
                        raise WorkerError('Engine request nonce was replayed or the admission window is full', 'denied')
                    nonces[nonce] = now + 125
                request = envelope['request']
                watcher = threading.Thread(target=disconnected, daemon=True)
                watcher.start()
                result = worker.operation(request, emit, cancel)
                if request.get('op') == 'ping':
                    result = dict(result, runtime_hash=code)
                emit('result', data=result)
                if request.get('op') == 'shutdown':
                    stop.set()
            except Exception as error:
                cancel.set()
                try:
                    emit('result', data={'ok': False,
                        'status': 'timeout' if isinstance(error, TimeoutError) else getattr(error, 'status', 'failed'),
                        'decision': getattr(error, 'decision', None), 'error': str(error)[:1000],
                        'blocking_measurement': getattr(error, 'blocking_measurement', None),
                        'usage': worker.last_generation if isinstance(error, TimeoutError) else None})
                except (OSError, WorkerError):
                    pass
            finally:
                ended.set()
                if watcher and watcher.ident is not None:
                    watcher.join(.25)
                if stream is not None:
                    stream.close()
                peer.close()

        if ready:
            ready.set()
        threads = []
        try:
            while not stop.is_set():
                try:
                    peer, _ = server.accept()
                except socket.timeout:
                    continue
                threads = [(thread, cancel) for thread, cancel in threads if thread.is_alive()]
                if len(threads) >= 8:
                    peer.close()
                    continue
                cancel = threading.Event()
                thread = threading.Thread(target=connection, args=(peer, cancel), daemon=True)
                thread.start()
                threads.append((thread, cancel))
        finally:
            server.close()
            for thread, cancel in threads:
                cancel.set()
            until = time.monotonic() + 15
            for thread, cancel in threads:
                thread.join(timeout=max(0, until - time.monotonic()))
            worker._stop()
            endpoint.unlink(missing_ok=True)
            try:
                if json.loads(record.read_text()).get('token') == owner:
                    record.unlink()
            except (OSError, ValueError):
                pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--registry', type=Path, required=True)
    args = parser.parse_args()
    if not args.directory.is_absolute() or not args.registry.is_absolute():
        parser.error('Engine service paths must be absolute')
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    serve(args.directory, args.registry, stop=stop)


if __name__ == '__main__':
    main()

"""Controlled transport fixture. Never shipped as an inference provider."""
import argparse
import json
import os
from pathlib import Path
import signal
import sys
import time


class Cancelled(BaseException):
    pass


def cancelled(signum, frame):
    raise Cancelled('Controlled worker cancellation')


def emit(kind, identity, seq, **fields):
    value = dict(id=identity, role='conversation', model='fixture-model',
                 node_id='fixture-task', kind=kind, seq=seq, **fields)
    print('SUTRA_EVENT ' + json.dumps(value), file=sys.stderr, flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--storage', required=True)
    parser.add_argument('--request-stream', action='store_true')
    args = parser.parse_args()
    root = Path(args.storage)
    root.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, cancelled)
    signal.signal(signal.SIGINT, cancelled)
    for raw in sys.stdin:
        envelope = json.loads(raw)
        request = envelope['request']
        with (root / 'worker-dispatches.ndjson').open('a') as stream:
            stream.write(json.dumps(dict(pid=os.getpid(), **envelope)) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        fatal = False
        try:
            time.sleep(min(2, max(0, request.get('delay_ms', 0)) / 1000))
            if request.get('crash_worker'):
                os._exit(7)
            for call in range(min(request.get('event_calls', 1), 150)):
                identity = format(call + 1, '032x')
                emit('start', identity, 1)
                for part in range(4):
                    emit('thinking', identity, part + 2, text='x' * 2048)
                emit('finish', identity, 6, status='accepted',
                     usage={'input_tokens': 8, 'output_tokens': 4})
            data = dict(request=request, environment=envelope['environment'],
                        worker_pid=os.getpid(), workspace=args.workspace)
            if request.get('response_bytes'):
                data['large'] = 'x' * min(request['response_bytes'], 5_000_000)
            answer = {'ok': True, 'data': data}
        except Cancelled as exc:
            answer = {'ok': False, 'fatal': True, 'error': str(exc)}
            fatal = True
        print(json.dumps(answer), flush=True)
        print('SUTRA_BRIDGE_REQUEST_END', file=sys.stderr, flush=True)
        if fatal:
            return


if __name__ == '__main__':
    main()

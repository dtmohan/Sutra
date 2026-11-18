"""Measure cold provider admission without changing task or provider state."""
import argparse
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time

from karma_adapter import KarmaAdapter
from monitoring import resource_state
from runtime_memory import HEADROOM


def admission(cold_memory_bytes, storage, *, native_fit=False):
    if not (native_fit and cold_memory_bytes is None) and (
            type(cold_memory_bytes) is not int or not 0 < cold_memory_bytes <= 2**53 - 1):
        raise ValueError('Cold memory estimate must be a positive safe integer in bytes')
    storage = Path(storage)
    if not storage.is_absolute():
        raise ValueError('Startup measurement storage must be an absolute path')
    at = time.time()
    hardware = resource_state(storage, refresh=True, newer_than=at)
    if hardware.get('admitted') and hardware['sample']['wall_time'] < at:
        hardware = {
            'available': False, 'admitted': False, 'decision': None,
            'reason': 'No fresh hardware observation for provider startup',
        }
    result = {
        'admitted': False,
        'decision': hardware.get('decision'),
        'reason': hardware['reason'],
        'at': at,
        'cold_memory_bytes': cold_memory_bytes,
        'system_reserve_bytes': HEADROOM,
        'hardware': hardware,
        'native_fit_only': native_fit and cold_memory_bytes is None,
        'peak_memory_verified': False,
    }
    if not hardware.get('available'):
        return result
    sample = hardware['sample']
    available = (sample['available_bytes'] if sample.get('profile') == 'portable-memory-v1'
                 else sample['free_bytes'] + sample['inactive_bytes'])
    # An explicit first native load has no measured footprint. This check
    # admits only the current machine reserve. Strata sizes its own runtime;
    # the manager refreshes pressure while waiting for generation readiness.
    allocation = KarmaAdapter().memory_allocation(cold_memory_bytes or 0, available, HEADROOM)
    result.update(available_bytes=available, memory_allocation=allocation)
    if not hardware['admitted']:
        return result
    result.update(admitted=allocation['decision'] == 'ALLOW', decision=allocation['decision'])
    result['reason'] = ('Measured hardware and cold provider memory allocation allow startup'
                        if result['admitted'] else
                        'Cold provider memory estimate and system reserve exceed available memory')
    if cold_memory_bytes is None:
        result['reason'] = ('Current hardware and system reserve allow a monitored native first load; '
                            'the model peak footprint is unknown' if result['admitted'] else
                            'Current hardware or system reserve blocks a native first load')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--cold-memory-bytes', type=int)
    mode.add_argument('--native-fit', action='store_true')
    parser.add_argument('--storage', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.storage is None:
            with TemporaryDirectory(prefix='sutra-provider-startup-') as directory:
                result = admission(args.cold_memory_bytes, Path(directory), native_fit=args.native_fit)
        else:
            result = admission(args.cold_memory_bytes, args.storage, native_fit=args.native_fit)
    except ValueError as error:
        print(json.dumps({'admitted': False, 'decision': None, 'reason': str(error),
                          'at': time.time()}, allow_nan=False))
        return 2
    print(json.dumps(result, allow_nan=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())

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


def admission(cold_memory_bytes, storage):
    if type(cold_memory_bytes) is not int or not 0 < cold_memory_bytes <= 2**53 - 1:
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
    }
    if not hardware.get('available'):
        return result
    sample = hardware['sample']
    available = (sample['available_bytes'] if sample.get('profile') == 'portable-memory-v1'
                 else sample['free_bytes'] + sample['inactive_bytes'])
    allocation = KarmaAdapter().memory_allocation(cold_memory_bytes, available, HEADROOM)
    result.update(available_bytes=available, memory_allocation=allocation)
    if not hardware['admitted']:
        return result
    result.update(admitted=allocation['decision'] == 'ALLOW', decision=allocation['decision'])
    result['reason'] = ('Measured hardware and cold provider memory allocation allow startup'
                        if result['admitted'] else
                        'Cold provider memory estimate and system reserve exceed available memory')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--cold-memory-bytes', type=int, required=True)
    parser.add_argument('--storage', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.storage is None:
            with TemporaryDirectory(prefix='sutra-provider-startup-') as directory:
                result = admission(args.cold_memory_bytes, Path(directory))
        else:
            result = admission(args.cold_memory_bytes, args.storage)
    except ValueError as error:
        print(json.dumps({'admitted': False, 'decision': None, 'reason': str(error),
                          'at': time.time()}, allow_nan=False))
        return 2
    print(json.dumps(result, allow_nan=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())

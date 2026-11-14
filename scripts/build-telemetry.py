"""Rebuild the Apple Silicon helper and smoke-test a real sensor sample on macOS."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import subprocess

root = Path(__file__).resolve().parent.parent
parser = argparse.ArgumentParser()
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--record', type=Path)
args = parser.parse_args()
if platform.system() != 'Darwin':
    raise SystemExit('The native helper requires macOS and Xcode Command Line Tools')
args.output.parent.mkdir(parents=True, exist_ok=True)
command = ['xcrun', 'clang', '-arch', 'arm64', '-mmacosx-version-min=12.0',
           '-fobjc-arc', '-framework', 'Foundation', '-framework', 'IOKit',
           str(root / 'harness/telemetry.m'), '-o', str(args.output.resolve())]
subprocess.run(command, check=True, timeout=90)
sample_checked = False
if platform.machine() == 'arm64':
    sample = subprocess.run([str(args.output.resolve()), '--once'], check=True,
                            capture_output=True, text=True, timeout=15)
    assert isinstance(json.loads(sample.stdout)['wall_time'], (int, float))
    sample_checked = True
record = {
    'compiler': subprocess.check_output(['xcrun', 'clang', '--version'], text=True).splitlines()[0],
    'sdk': subprocess.check_output(['xcrun', '--show-sdk-version'], text=True).strip(),
    'architecture': 'arm64',
    'sample_checked': sample_checked,
    'sha256': {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
               for name in ('harness/telemetry.m', 'harness/smc_temperature.h')},
}
record['sha256']['bin/telemetry'] = hashlib.sha256(args.output.read_bytes()).hexdigest()
if args.record:
    args.record.write_text(json.dumps(record, indent=2) + '\n')
print(json.dumps(record, indent=2))

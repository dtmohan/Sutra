"""Explicitly regenerate or verify the runtime manifest after reviewed changes."""
import argparse
import json
from pathlib import Path
import sys

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
from package import integrity_hashes, verify_integrity

parser = argparse.ArgumentParser()
parser.add_argument('--write', action='store_true')
args = parser.parse_args()
if args.write:
    manifest = json.loads((root / 'runtime-integrity.json').read_text())
    manifest.update(format=2, sha256=integrity_hashes(root))
    (root / 'runtime-integrity.json').write_text(json.dumps(manifest, indent=2) + '\n')
verify_integrity(root)
print('Runtime integrity: complete coverage and matching hashes')

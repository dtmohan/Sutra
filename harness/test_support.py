"""Portable test discovery; explicit environment paths take precedence."""
import os
from pathlib import Path
import shutil
import sys
import unittest

NODE=os.environ.get('KARMA_NODE') or shutil.which('node')
COMPILER=os.environ.get('KARMA_TYPESCRIPT') or str(Path(__file__).resolve().parent.parent/'vendor/typescript/typescript.js')
ENV={k:v for k,v in {'KARMA_NODE':NODE,'KARMA_TYPESCRIPT':COMPILER}.items() if v}
HAS_TYPESCRIPT=bool(NODE and Path(NODE).is_file() and Path(COMPILER).is_file())
HAS_SANDBOX=sys.platform=='darwin' and Path('/usr/bin/sandbox-exec').exists()
requires_typescript=unittest.skipUnless(HAS_TYPESCRIPT,'Node and bundled/configured TypeScript required')
requires_sandbox=unittest.skipUnless(HAS_SANDBOX,'macOS sandbox-exec required')

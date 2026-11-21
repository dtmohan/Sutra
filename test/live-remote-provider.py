"""Real saved-task continuity: RTX -> local native High -> RTX, fresh caller per phase.

Run --phase low-first, high, low-last, verify against one new absolute --output.
Uses the real gateway and device telemetry. Downloads no model, performs no
workspace actions and never changes saved User tier selections. Runtime startup
and trusted RTX profile registration must already be complete.
"""
import importlib.util
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('sutra_remote_live_helpers', ROOT / 'test/live-strata-management.py')
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
helpers.MODELS = {'low': 'sutra:remote/rtx3090', 'mid': 'qwen3:4b', 'high': 'strata:high'}
os.environ.update(SUTRA_REMOTE_MODELS=json.dumps(['sutra:remote/rtx3090']),
                  SUTRA_REMOTE_ONLY='0', SUTRA_STRATA_PORT='19483',
                  SUTRA_STRATA_MODEL='qwen3.8-flash-next-q2_0',
                  SUTRA_STRATA_MODELS=json.dumps(['strata:high']))
if __name__ == '__main__':
    helpers.main()

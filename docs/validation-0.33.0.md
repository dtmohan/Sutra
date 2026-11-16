# Sutra 0.33.0 validation — 2026-11-16

The canonical STP kernel is unchanged, SHA-256 `9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`.

## Release checks

`bash scripts/qa.sh --browser --with-package` passed all 14 gates. The Python suite passed 678 tests. Node suites include capability approvals, native chat, runtime integrity and existing host controls. Browser tests cover the monitor and other webviews. Package checks verified every bundled file and confirmed that runtime tests/experiment scripts are excluded.

New regressions exercise missing/forged/expired approval, source changes, scope escapes, registry changes, installed-file tampering, budget checks at dispatch, disabled policy, resource refusal, failed-operation charges, ledger reopening and interrupted work without refund. Package checks cover integrity failure, traversal, links, expansion bounds and redirects. Native flow tests separate install/check approvals and recover a completed operation after a lost reply without another grant, replay or charge. Orphan tool folders are quarantined only on a newly authorized reinstall. UI tests cover workspace-policy confirmation, reload/model-switch persistence, changed settings, cancellation/disarming and escaped diagnostics.

## Real tools

`test/live-capabilities.py` downloaded the official pinned Ruff 0.17.5 and Pyright 1.1.414 artifacts into isolated Sutra storage, verified their digests and ran them through the macOS sandbox. The fixture contained an unused `os` import and a string assigned to an `int` annotation.

Ruff produced F401 (and an import-order diagnostic); Pyright produced `reportAssignmentType`. Both returned diagnostic exit code 1. The second live run retained the original source and all task constraints, passed audit verification, and charged five operations (initial catalog plus two installs and two checks), 64 source bytes and 2.330 observed seconds. No assertion-success claim was inferred from process output.

The first live run exposed a Pyright integration problem: invoking its internal bundle directly omitted the launcher-defined type-stub root. Using `package/index.js`, relative snapshot includes and explicit empty stub/venv folders corrected the test. Regression/live assertions now require the expected type diagnostic and reject undefined-builtin errors.

## Live local-model switching

`test/live-capability-switch.py` continued from those saved tool receipts through fresh bridge processes:

| Tier/model | Wall time | Observation |
| --- | ---: | --- |
| Low, qwen3:4b | 9.39 s | Produced a structured Pyright capability proposal; no tool authority was granted by that proposal. |
| Mid, qwen3.6:35b | 8.01 s | Explained the saved string/int mismatch and recalled Cedar CED724 plus no edits, tests or dependency changes. |
| Low, qwen3:4b | 6.52 s | Recalled the identifier, finding and unchanged-source/test/dependency constraints. |

All three responses were admitted. Their conversation ledger recorded three calls and 7,407 charged tokens. The workspace's existing tool receipt, task snapshot and consumed usage stayed identical throughout; proposing/explaining a tool operation did not execute it. Audit and invocation verification passed. Models used the normal runtime resource checks in shared-server mode; no other client's models were unloaded.

## Boundaries

The live sequence tests real model requests and evidence recall. Separate deterministic and native-provider tests exercise the approval/dispatch boundary and interruption recovery. These tests do not claim end-to-end manual clicking of the installed VS Code dialogs or prove model semantic correctness.

Tool execution is validated on this Apple Silicon Mac. Linux/Windows artifacts have registry mappings, but execution is deliberately blocked until platform sandbox adapters are implemented and tested. Static diagnostics use a bounded Python snapshot and isolated configuration; they can differ from the project's full CI. Pyright currently targets Python 3.9. Historical evidence is explicitly labelled and must be refreshed for current-source claims.

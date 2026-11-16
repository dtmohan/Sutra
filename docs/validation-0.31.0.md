# Sutra 0.31.0 validation

- 655 Python tests passed on macOS with Python 3.9, including 10 allocation-recovery tests.
- All eight Node contract gates passed: model activity, native provider, hardening, panel entry, model selection, branding, host tokens and runtime integrity.
- Dashboard, diagram and Monitor browser suites passed in headless Chrome. The new recovery control is visible only for eligible pauses, disabled during work, and emits a command without proposal/permission data. Spent/total amounts render at narrow width without page overflow.
- Release metadata, VSIX contents and runtime integrity checks passed. Runtime tests and experiment scripts remain excluded from the VSIX.
- Canonical kernel SHA-256 remains `9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`.

## Reported saved task

The user's existing ledger was read through a separate SQLite backup. Its live allocation was not changed. The copied packet reproduced the reported capacity pause: 228,975 of 250,000 model-budget units had been consumed, leaving a 10,512-unit per-call share including a 2,628-unit output reserve. Mandatory pinned context could not fit.

The explicit amendment increased the model-budget total to 478,975 and retained exactly 228,975 consumed units. Corresponding additions for calls, repository operations, source bytes and time were displayed and recorded. Packet sizing then admitted 32,384 units with an 8,192-unit output reserve; it retained four pinned statements, the latest request and two recent turns, with nine excerpt files. Older turns remained in retained memory.

After reopening the copied ledger, the existing policy-upgrade restart read source afresh, mapped the workspace, ran two child reviews on a smaller deterministic model transport, and synthesized on a larger deterministic transport. The workflow reached DONE with an admitted result. Contract, permissions, conversation binding, pins, allocation amendment and receipt validation passed. Spending increased to 343,958 units, 16 calls and 465 repository operations; no prior use was refunded.

This is a state-machine/packet/accounting regression, not a fresh local-LLM answer-quality benchmark. Source compilation and scoped reads were real; model responses were deterministic fixtures. Actual inference still depends on current memory pressure, model capability, remaining grants and output/reviewer checks.

## Recovery checks

Tests cover preview without mutation; declined/missing confirmation; additive totals; reopening after approval; atomic rollback on interruption; stale/tampered/replayed proposals; changed policy or spending; rejected invalid dimensions and ceilings; active and unsettled work; preserved child limits and permissions; updated repository admission; task-budget pauses; and context retention through model switches and policy restart.

The initial outer-sandbox run could not launch nested macOS test sandboxes or Chrome. The host-permitted run passed all Python checks. Browser checks were rerun with an absolute output path after correcting a test invocation path error; all three browser suites passed. No production admission check was weakened to run these tests.

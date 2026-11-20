# Sutra 0.50.3 — workspace consent and saved-review recovery

Validated on 2026-11-19. The canonical STP kernel remains
`9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`.

## Confirmed failure

The reported saved review committed `app.py`, `config.py` and `tools.py`, although
the user's request targeted the whole workspace and those files do not exist in
Sutra. A failed catalog then raised again during status rendering, masking its
original cause as “Latest catalog refresh failed.” The consent journey also ended
the response at a link and required a separate continuation.

## Resulting behavior

- Current-prompt file/directory targets remain narrow. A broad workspace read
  targets the workspace; model-suggested starting files do not define user scope.
- Catalog diagnostics retain the original failure and receipt, distinguish
  missing roots, preserve saved pages/spending, and never fall back to stale
  source authority or claim structural completion for an empty source scope.
- Native chat uses separate read-access, agent-access and edit/test tools. A
  host-issued, single-use consent ticket binds workspace, grant generation,
  request, cursor and chat. Approval persists access and automatically continues
  the exact request through its host receipt, including on another model.
- Revoked or changed scope requires fresh explicit consent, even when VS Code
  has cached “Allow in this Session.” Read approval cannot approve agent access
  or edits/tests. A denied permission result produces saved-request guidance.
- A saved router-invented scope has an explicitly confirmed correction path.
  It versions only that read scope in the same task, settles children, preserves
  exact spending and the effective allocation amendment prefix, and records
  intent/completion. Interrupted correction can retry without a budget reset.
- The package-verified 0.50.2 policy is accepted for historical conversation
  data only. Old executable results still require recovery under current policy.

## Saved-request reproduction

A SQLite read-only WAL-consistent backup of the reported ledger was used; the
live task was not edited. The original flow and `review-b912fb313efd` session,
exact prompt and conversation were retained. Confirmed correction created
contract epoch 2 with only `allowed_paths` changed to `.`. Its two spent
operations, elapsed charge and all allocation limits remained exact. A fresh
governed catalog found 353 files and charged one additional operation. Audit and
invocation verification passed. This test did not invoke a model or produce a
new semantic architecture report.

The retained report is
`/var/folders/fy/x8qdt8yx3mn57b2pmjj6c0zc0000gn/T/sutra-saved-scope-final-lpx6h192/scope-recovery-result.json`.

## Real native-chat lifecycle

`test/live-permission-tool.py` uses an isolated VS Code profile, real native chat
tool confirmations and real authorization storage. Its backend is deterministic;
it performs no production source reads or inference. Final run h passed native
Skip, saved retry, approval-to-report automatic continuation, Mid-tier grant
reuse, and revoke/decline/regrant with a cached host session approval.
Evidence: `/private/tmp/sutra-permission-tool-20261119-h/result.json` and adjacent
screenshots. These tests found and prompted fixes for a legacy conversation-axis
loss and cached host permission overriding revocation.

The default full Chat Agent attempt in a separate logged-out profile was blocked
by host GitHub sign-in before Sutra was called, including with MCP disabled
(run g). It is retained as `host_blocked`, not counted as a successful full-Agent
test. The successful participant test establishes the narrower native-chat
provider/tool lifecycle.

Native chat needs its contributed access tools enabled. New Sutra chats include
them. Existing chats restricted to the old action-only tool set retain the
link/Monitor fallback; no undeclared tool is emitted or other chat opened.

## Automated validation

Targeted coverage includes explicit named scope, missing/failed catalog and
fresh recovery, denied/forged/stale/cancelled consent, tool and profile isolation,
workspace changes, reloads, model switches, interrupted scope correction, exact
spent-use carry, existing allocation amendments and later amendment continuity.
The final frozen-source `scripts/qa.sh --with-package` run passed **24/24 gates**:
**1,180 Python tests with eight environmental skips**, all Node contract suites,
runtime integrity, metadata, packaging and exact packaged contents. The earlier
full run overlapped implementation changes and is not used as the release result.
Its isolated checked-runtime and startup-frame failures also passed on a focused
rerun before the final full suite. No NPU/GPU benchmark was rerun for this change.

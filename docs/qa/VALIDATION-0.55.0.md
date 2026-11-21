# Sutra 0.55.0 validation

Date: 2026-11-20. Scope: durable whole-task continuation, kernel-bound permits,
Stop, UI reload, model changes and source-frontier recovery. The canonical STP
kernel and configured user model tiers remain unchanged.

## Implementation

- The coordinator advances authoritative backend stages outside the UI. It keeps
  model/reasoning settings immutable for an accepted request, checks the captured
  grant before each effect, and never carries one-time consent into a generated
  next-step request.
- The Karma continuation adapter evaluates each automatic intent through the
  canonical kernel. `RUNNER_CONTINUATION_DECISION` records in the existing
  `page_audit` chain bind the UID, original submission digest, task contract,
  checkpoint, kernel frame and next event. Stale, foreign or forged permits fail
  before source/model effects or reconciliation.
- Resource waits perform fresh observations without inference. An admitted
  resume rechecks readiness. Unchanged observations reuse a bounded diagnostic
  head/permit rather than growing an audit stream with clock noise.
- Stop cuts off queued work immediately. A bounded administrative FIN audit
  revokes the runner UID without closing or refunding its saved task. Pending
  Stop audit intent survives restart and cannot be pruned after transport ACK.
  A failed audit stops admission and has a visible retry control.
- A Stop observed during reattachment ends that response. It cannot fall through
  recovery and submit replacement work. A new explicit Continue may recover a
  ticket that was already cancelled before that attachment.
- The daemon owns independent bounded telemetry. Monitor displays the task
  continuation kernel frame and distinguishes a local Stop from its pending
  chained audit receipt.

## A live failure found during validation

The initial native Low trial reviewed three tiny Python files. The first child
received function bodies, then asked for imports and globals above them. The old
recipe advanced only after the last delivered declaration and returned no source
for those earlier lines. The pause was classified as source exhaustion despite
82,099 remaining allocation units and a 65,536 context allowance.

New child recipe 5 fills unseen whole-line gaps from the union of authenticated
source spans. After all authorized nonblank lines have been delivered, a
source-frontier adapter can request a bounded BRIDGE handoff of findings and
unresolved checks to the parent. Delivery is not semantic completion. Earlier
recipes retain their original selection and receipts. The exact three-file
regression verifies reload/model switching, unchanged paid source usage and no
permission or spending reset.

## Regression coverage

Nine actual coordinator/TCP/process scenarios use the production bridge,
orchestration, gateway and temporary ledgers. Only model transport and hardware
observations are deterministic fixtures. They verify:

- Detached multi-stage completion, replay protection and complete usage.
- Queued Stop before dispatch, then administrative FIN after the queue head.
- Fresh readiness polling with zero inference until the blocker clears.
- Stop during a wait, safe daemon restart and exact checkpoint recovery.
- Invalid continuation bindings and unknown-outcome stops without stale success.
- Grant revocation before the next effect, including Stop audit without a grant.
- In-flight interruption followed by a new High runner on the same saved task,
  retaining checkpoints, pinned context, permissions, limits and prior charges.
- Chained kernel receipts and exact automatic permit echoes; raw stale, forged,
  cross-UID and changed-digest permits fail without new model/ledger effects.

Additional focused tests cover permission-approval binding in read-only summary,
historical source replay, exact delivered-line coverage, kernel threshold
scaling, diagnostic deduplication, Stop revocation and receipt corruption.

## Actual VS Code reload and Stop

A temporary startup helper exercised the native model API and real
`workbench.action.reloadWindow`/`karma.stopRunner` commands in an isolated VS Code
profile. It used the installed `qwen3:4b`, a stable chat ID and a private read-only
grant. These were actual local-model calls, not the deterministic transport.

Reload during CHILDREN/running replaced extension host 69123 with 69353 while
retaining the same coordinator process and runner ticket. Reattachment advanced
the same job through 19 stages. At its measured reviewer-memory wait, Monitor
Stop produced the stopped notice with no replacement request.

The contract, grant and allocation version stayed unchanged. The task retained
13 model receipts, 41,811 consumed model units, 12 repository operations and 1,461
source bytes. The administrative FIN was committed at audit sequence 311 after
BRIDGE sequence 310; the full chain verified, and an old-UID authorization probe
was refused. The pending Stop receipt cleared before cleanup. Only the completed
private test processes were closed.

An earlier trial caught the reattachment Stop/restart bug; its failure evidence
was retained and the corrected final-policy trial reran independently.

## Actual native architecture review

The final-policy build also ran the three-file architecture request through
native VS Code using `strata:high`, with `qwen3:4b` reviewing claims. It completed
in 173.945 seconds with 10 recorded calls, 28,799 input and 2,763 output tokens.
It delivered three components, four request-flow steps, source-linked claims,
two child reports and both preview diagram views. Sixteen chained continuation
decisions included ALLOW, BRIDGE and terminal FIN. Model judgments remain
unverified; unsupported claim groups were omitted and disclosed.

The native Low greeting completed in 6.371 seconds. A Low architecture trial
completed both children and produced its draft, then correctly waited because
the automatically selected independent reviewer needed an estimated 26.1 GiB
plus 4 GiB reserve while about 20 GiB was available. That trial was explicitly
stopped. Reviewer selection/resource policy was not relaxed to claim success.

## Performance scope

Ten thousand evaluations of `KarmaAdapter.task_continuation(observation)` measured
3.417 microseconds median and 4.25 microseconds p95 on this host. This includes the
adapter and canonical kernel, and excludes evidence collection, hash-chain
verification, SQLite, transport and device work. It is not an end-to-end overhead
or cross-platform performance claim.

## Release gates

The final frozen build passed all **35 release gates** in a single run of
`scripts/qa.sh --with-package --browser --vscode`. The Python suite ran **1,838
tests**, with eight optional dependency/platform skips. Node contracts, all nine
real-process runner scenarios, browser controls, runtime integrity, exact VSIX
contents and the packaged VS Code host passed. Earlier development runs were
interrupted or failed integrity checks when fixes landed during validation; they
are not counted as this clean release pass.

The packaged VSIX contains 300 runtime files and is 5,188,600 bytes. Its SHA-256 is
`a0e9c5c762b27929160c7e0e69e0de6d71aafef4e5ec4dcb37d286a1967e909a`.
The VS Code CLI installed 0.55.0 successfully. All 300 installed runtime files
matched the tested VSIX byte-for-byte (ignoring only VS Code’s added package
metadata); the extension registry and CLI reported 0.55.0. The saved model tiers,
reasoning, provider configuration and background utility-model preferences were
unchanged.

The canonical kernel SHA-256 is
`9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`.

## Limits

The controlled suite validates invariants and recovery. The real-model smoke is
a small project, not a full semantic audit of Sutra or a model benchmark. The
existing independent-reviewer selection can require more memory than a Low
writer; a healthy system-pressure badge alone does not establish that it fits.
Unknown effects are never replayed automatically. Broader operation coverage is
scoped separately in [the common boundary proposal](../kernel-operation-boundary.md).

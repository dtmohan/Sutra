# Bounded accounting and reservation recovery

Orchestration uses cumulative spending, active-child reservations and versioned
accounting seals. The canonical STP kernel and its decision choices are unchanged.
Rotating a batch never grants a new task allocation, drops context, changes scope
or unloads a model.

## Cadence and pending work

The default accounting batch contains at most 64 newly dispatched charges. A seal
is also emitted after 128 charge/settlement changes. A quiet tail can be sealed
after 30 seconds of inactivity, or after a 300-second maximum batch age when the
host polls. Persistent bridge workers poll while waiting for the next request.
Their input reader is bounded and SQLite remains on the controller thread.
The timer does not interrupt a model call. A busy workspace writer lock defers
idle sealing; queued input and task permissions remain intact.

A pending charge stays fully counted even if its owner is terminal. A later
settlement replaces the declared reservation exactly once. Unknown cancelled or
interrupted usage keeps its conservative charge. Duplicate settlement is an
idempotent no-op. New settlements include a host timestamp so a late adjustment
starts a fresh inactivity interval; old records remain readable.

A failed seal rolls back its record, compact header and audit anchor together.
Rollback also invalidates provisional in-memory cursors before another admission.

## Version 2 seals

A V2 seal contains cumulative root totals, source watermarks and rolling hashes,
root scope digests, explicit pending-reservation entries, and a recovery snapshot.
Each pending entry binds the charge, declared/settled cost, owner, dispatch
receipt, contract/calibration binding and context/permission digests. The snapshot
retains roots, active/pending ancestry, state/event heads, exact Decimal totals,
and a bounded recent-charge window. It does not replace the authoritative task
context or permission records in the ledger.

The seal digest commits a compact header, which includes the recovery payload's
hash. A separate append-only header table makes chain checks independent of large
snapshot payloads. Body/header parity is part of verification. Each seal links
the previous seal and is anchored in the existing audit chain. Source ranges are
recorded by adjacent watermarks; pending entries are carried rather than renewed.
V1 seals remain deep-verifiable. Legacy import preserves original records and
spending; a valid V2 unsealed tail is not mistaken for a legacy migration.

## Strict and externally anchored recovery

Strict verification is the default. A new connection checks the audit chain and
replays authoritative source records against the seals. This establishes source
consistency but remains proportional to retained history.

A trusted host caller can explicitly supply an out-of-ledger `{seq, digest}` to
`recover`. Only that pinned V2 seal is trusted as a starting snapshot. Its body,
header, scope records and pending entries are checked, then canonical records
newer than the pinned source watermarks are replayed. An older trusted head does
not authenticate later seals just because they link back to it: their source
suffix is re-derived before accepting their totals. Cost depends on unseen work
and the compact header chain; it is not constant if the supplied anchor lags.

This package does not configure a NAS, external signing service or independent
head writer. No model tool can enable trusted recovery. The caller must establish
the anchor's authenticity outside the SQLite database. Reading a digest from the
same file and calling it trusted provides no independent assurance.

Warm anchored readers verify new canonical rows on a competing writer's commit.
Schema/file changes, rollback, malformed tails, missing headers or inconsistent
seals reject verification. Historical state queries outside the retained window
use explicit deep verification rather than inventing a retired result.

## Detailed audit and admission stops

`BackgroundAudit.start(store)` runs a detailed read-only source audit on a
separate connection. The store's next `checked` admission polls its result. A
failure is recorded as a finding, never corrected totals. Findings block later
admissions and survive close/reopen. If an effect transaction must roll back,
quarantine is persisted after rollback; partial effects are not committed merely
to keep the finding. There is no automatic finding-clear or budget-reset API.

Hashes provide tamper evidence under the trusted-host writer model. They cannot
authenticate a privileged process that rewrites the entire database and history.
An external trusted head narrows that trust boundary. Model observations and
numerical receipts do not grant file, edit, test or network permissions.

## Timings and API

`accounting_metrics` retains at most 256 recent samples per stage plus aggregate
counts and extrema. Observers are optional diagnostics. Ordinary observer errors
do not alter admission. Kernel timing surrounds only the unchanged canonical
`evaluate` call, excluding measurement validation and recording overhead.
Separate stages cover budget gates, seals, strict/anchored recovery, refresh and
detailed audit. Total queue latency still includes source checks, IPC and worker
execution; it must not be presented as pure kernel or encoder time.

- `configure(store, batch_size=64)` selects a pre-effect cadence without allocating
  capacity. It cannot reset an existing allocation.
- `status`, `checkpoints` and `flush` expose verified state and explicit sealing.
- `seal_if_idle` applies host-observed idle/max-age boundaries.
- `set_trusted_anchor` and `recover` are explicit trusted-host interfaces.
- `verify(deep=True)` re-derives sealed history; failure blocks admission.
- `BackgroundAudit` provides optional read-only detailed auditing.

Indexes cover parent lookup, session/kind lookup, charge ownership and audit
events. Active-child discovery excludes terminal history; FIFO callers still
order active records by their committed submission cursor.

Native `kind='task'` spending retains its distinct `review_budget.usage()` semantics
for repository, document, capability, invocation and permission carry-over, plus
projected edit/test seconds. Replacing it with generic charge totals would
miscount work; its remaining session scans are not hidden by worker benchmarks.

Warm gains require reuse of the verified connection. The current VS Code bridge
opens a ledger per operation, so default strict recovery still applies there.
Projection size follows retained roots, active/pending ancestry and recent work,
not merely batch size; many simultaneously open tasks can increase it. Compact
seal-chain verification still scales with the number of seals.

Regression tests are in `harness/test_accounting_batches.py` and
`harness/test_accounting_recovery.py`. Measured host growth, cold/anchored recovery
and actual NPU/GPU co-runs are retained in the adjacent `Sutra Reservation Recovery
Evaluation` output directory. Host-only fixtures use declared cost units and do
not claim device inference or source I/O that they did not execute.

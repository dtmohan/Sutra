# Sutra 0.50.0 — reservation recovery and idle seals

Validation on 2026-11-18, Apple M5 Pro / 64 GiB, macOS 27.2.1.
The canonical STP kernel is unchanged:
`9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`.
Saved model settings and the pre-existing GPU runtime were preserved.

## Shipped behavior

- Version 2 cumulative seals carry explicit pending charges, their owners,
  dispatch receipts, context/permission bindings and source-chain positions.
  Recovery does not refund interrupted work or charge a duplicate settlement.
- Default cadence is 64 dispatches or 128 charge/settlement changes. The
  persistent bridge seals after 30 seconds idle; a 300-second batch-age bound
  applies when the host polls. It never interrupts generation to run the timer.
- Kernel evaluation, budget admission, seal construction, refresh, recovery and
  detailed audit have separate bounded diagnostic timing buffers.
- Strict source verification remains the default. A trusted host may explicitly
  supply an independently authenticated seal head for snapshot-plus-tail
  recovery. No model tool can enable that trust or reset a budget.
- Optional background audits inspect a private online-backup snapshot. Failed
  verification durably stops admission; unavailable snapshots pause admission
  until a successful retry. Neither case silently repairs spending.

The backup source uses an existing-file read/write descriptor with SQLite
`query_only=ON`; the private snapshot verifier uses an OS read-only connection.
This avoids an Apple system SQLite 3.54 advisory-lock race reproduced during
concurrent receipts. Both tested runtimes passed 48 race rounds / 240 writes
after the fix. No model, source or permission action runs in the audit worker.

## Validation coverage

The complete standard QA suite passes 24/24 gates: 1,109 Python tests (eight
environment-dependent skips), Node contract gates, runtime integrity, release
metadata and packaged-content checks.
Final focused accounting tests: 71 passed on Python 3.12; Python 3.9 ran 71 with
one defensive-SQLite configuration skip. Additional cases cover:

- Pending work across seals, process recovery, model-identity changes and late
  settlement; exact context, permissions, allocation and cumulative spending.
- Unknown cancelled usage retaining its conservative charge; duplicate
  settlement as an idempotent no-op; terminal owners with unsettled work.
- Idle, maximum-age, empty-tail and metadata-only-tail seals; busy workspace
  locks; failed seal rollback; partial UTF-8 input and ordered request frames.
- Rewritten context/permissions, malformed event tails, missing compact headers,
  warm-cache integrity failures and durable quarantine after rollback/reopen.
- Background snapshot contention, transient failure/retry and a detailed audit
  during settlement of already-admitted reservations in a 100,000-request ledger.

A real persistent bridge was also left idle for the default 30-second interval
with one pending reservation, then closed and reopened. It emitted an idle seal
and retained the exact parent, permission and spending records.

## Host history growth

Actual public host accounting APIs, default cadence, synthetic declared costs;
these rows do not represent 100,000 device inference calls. Current verified
connection reused; raw samples retained outside the source package.

| Settled history | Request median | Pure kernel median | Seal median |
| ---: | ---: | ---: | ---: |
| 1,000 | 2.25 ms | 0.98 us | 2.20 ms |
| 10,000 | 2.32 ms | 0.94 us | 2.13 ms |
| 100,000 | 2.53 ms | 1.04 us | 2.34 ms |

The controller's current physical footprint was 23.9 MiB at 100,000. This is
near-flat warm performance with modest drift, not a proof of constant time.
The retained projection includes roots, active/pending ancestry and recent work;
many open tasks can enlarge it. Compact-header verification grows with seals.

At 100,000 requests, opening through the first verified spending query took
18.68 s in strict mode and 21.11 ms with a trusted test-controller anchor.
Competing-writer refresh took 17.72 s strict and 0.396 ms anchored. The OS file
cache was not evicted. The anchor is an explicit controlled test input; this
release does not deploy an independently protected NAS/head witness. Strict
production startup has not been relabelled as the fast anchored measurement.

The host growth/recovery fixtures preceded a platform-only change to the optional
background audit helper. The central accounting implementation is byte-identical
across those fixtures and the final live run. The original policy bindings were
retained, with no admission bypass; a later pressure trial finalized only
reservations admitted before that policy change. Full provenance is in the
evaluation directory.

## Actual NPU/GPU co-run

Three matched GPU-alone/concurrent pairs used the already-loaded native
`qwen3.8-flash-next-q2_0`, identical prompt/answer hashes, 2,710 input and 505
output tokens, prompt cache disabled. All work shared one parent with 10,000
prior settled host requests. Retrieval used the pinned MiniLM sentence encoder
on ANE with fresh source checks over 220 passages from 33 files.

- GPU end-to-end means: 28.88 s alone / 29.40 s concurrent, 1.8% longer.
- 1,193 retrievals ran during generation. Per-phase end-to-end medians were
  8.59–8.99 ms and p95 9.37–10.22 ms. Worker retrieval medians were 3.35–3.52 ms.
- Pure kernel median was 0.75 us; seal median 2.47 ms, measured separately.
  Twelve real idle seals used accelerated experiment intervals of 2/10 seconds;
  production defaults remain 30/300 seconds, separately exercised by the bridge.
- A separate paced NPU-only control, after an explicitly charged identical-worker
  restart, had median 10.01 ms / p95 12.28 ms over 100 arrivals at 40 ms intervals.
  The earlier burst test (61 admitted, 39 capacity BRIDGEs) is retained as a
  backpressure test and is not a matched latency baseline.
- Queue cancellation, cancellation during an actual native call, GPU interruption
  and subsequent context continuation passed. A read-only independent audit
  passed 35,581 checks: 1,369 NPU jobs, eight GPU children, zero pending charges.
  Context, permissions, original allocation and all consumed budgets remained.

Each concurrent phase still had one 1.7–2.1 s final-result tail coinciding with
GPU receipt/final verification; worker compute for those results was about
3.5 ms. Invocation/protocol audit replay and strict connection reopening remain
history-dependent. Lock waits were not individually timed, so the experiment
does not attribute the entire delay to one function. Uniformly bounded queue
latency remains a follow-up, distinct from the measured warm admission gains.

Peak sampled physical footprints: GPU engine 18.83 GiB, NPU worker 266.7 MiB,
controller 195.8 MiB. These exclude external ANE daemons and are not summed into
a purported machine peak. There is no hardware energy/cycle trace. This is a
host/device accounting experiment, not an architecture-answer quality benchmark.

## Artifacts and limits

The adjacent `Sutra Reservation Recovery Evaluation` directory retains scripts,
source/model fingerprints, raw timing streams, ledgers, cancellation receipts,
independent audit results and the comparison report. The VSIX contains the host
accounting/recovery/idle changes. The private-interface NPU retrieval lane is
still a host experiment; the release does not silently enable it in VS Code.

Native `kind='task'` spending still uses its distinct `review_budget.usage()`
semantics. The VS Code bridge opens a ledger per operation, so strict cold
verification and historical audit paths remain visible costs. No budget is
replenished by sealing, reloading, retrying or switching a model.

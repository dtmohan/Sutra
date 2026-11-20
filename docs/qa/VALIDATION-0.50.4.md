# Sutra 0.50.4 — Monitor, context admission and rejected children

Validated on 2026-11-19. STP kernel SHA-256 remains
`9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`.
User model tiers, ownership settings and live task data were not changed.

## Three separate failures

The native telemetry collector was active. Monitor's task-budget facade lacked
`_contract_history`, which allocation-prefix verification now requires; one task
diagnostic exception aborted the entire dashboard. The fix uses a query-only
ledger facade with contract/audit verification and returns independent hardware,
runtime and usage sections alongside explicit partial-diagnostic errors.

The first pause did observe real macOS warning pressure: about 27.3 GB compressed,
239 MB free and 8.58 GiB available estimate. Thermal pressure was nominal. The
configured Low/Mid providers are shared Ollama; High is legacy Strata. Observing
the shared server without forcibly unloading it was consistent with those
settings. Later OS pressure returned to normal.

The subsequent conversation had zero pinned statements and two short turns.
The old pinned-overflow message did not establish its stated cause. Its exact
failing context cap was not recorded, so a hardware-derived cap is a retrospective
inference. A read-only saved-conversation clone with a forced 4K cap reproduces
the mandatory-format shortage: 12,069 conservative input units plus 512 output
reserve, requiring 12,581 context units. The same history fits 65,536 with its
constraints and exact prior spending retained. No inference occurred in this
reproduction. New code records context plans before dispatch, reports measured
shortages, and permits one fresh mandatory-context preflight only when current
hardware, model/configuration/calibration limits and the unchanged kernel admit
the larger allocation. Otherwise it returns a typed model-capacity wait.

The failed child was a separate output-validation problem: invalid structured
output, a missing branch report, then invalid structured output. Quarantine was
correct; remaining in CHILDREN and attempting to join it was incorrect. The fix
persists a receipt-backed validation stop. Resume may admit one replacement from
the failed branch's unspent share. Original quarantine and spending remain;
accepted siblings and checkpoints survive. Auto can try another configured
model, and a later admitted resource fallback takes precedence. Only the
accepted current replacement can join. A child error mentioning cancellation no
longer mislabels the parent user request as cancelled.

## Reproduction and verification

- Actual live ledger: query-only dashboard returned fresh CPU/GPU temperatures,
  memory, runtime allocation, task budget and 34 recorded calls with no diagnostic
  errors in about 102 ms. It used the host's shared runtime directory, not a
  disabled-runtime fixture. No model, task or user setting was mutated.
- Monitor tests include a corrected scope, retained allocation amendment and
  quarantined child in one ledger; corrupted proofs cannot display ALLOW.
  Runtime/task/SQLite failures retain independently measured hardware. The real
  browser renderer retains usage and temperatures while displaying a partial
  diagnostic warning.
- Conversation/runtime tests cover base-format versus true-pin shortages,
  successful and denied refits, current OS pressure, architecture/calibration
  limits, plan persistence and exact retained history/constraints/spending.
- Child recovery tests reach a final report after reload and a model switch,
  preserve completed siblings and accepted slices, reject invalid joins, retain
  exact old spending, cap repeated failure, recover an interrupted replacement
  idempotently and respect later resource fallback.
- The installed 0.50.3 policy files were verified against its VSIX. Only that
  historical conversation-data digest was added for compatibility. Old execution
  bindings still require the established policy restart; invalid worker results
  are never made current by a compatibility entry.

Saved-ledger reproductions are retained separately:

- `/var/folders/fy/x8qdt8yx3mn57b2pmjj6c0zc0000gn/T/sutra054-memory-audit-1pp9hbxh/report.json`
- `/var/folders/fy/x8qdt8yx3mn57b2pmjj6c0zc0000gn/T/sutra-child-quarantine-bgmqtffr/result.json`

The child clone reproduces its diagnostic state only. Successful replacement
and final-report tests use current-policy deterministic model fixtures. This
release validation does not claim that a new full High-model architecture review
has succeeded or that generated architecture claims are semantically certified.

The final frozen-source `scripts/qa.sh --with-package` run passed **24/24 gates**:
**1,196 Python tests with eight environmental skips**, Node contract suites,
runtime integrity, metadata, packaging and exact packaged contents. The Monitor
browser suite passed separately. An earlier aggregate run was intentionally
interrupted when a final child-route precedence correction arrived; it is not
the release result. No NPU/GPU benchmark or full live-model architecture review
is part of this update.

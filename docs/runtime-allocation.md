# Runtime allocation and fallback

Sutra 0.29.0 estimates the **additional** memory needed by the next call. It
retains the canonical STP kernel; adapters provide normalized measurements and
the central workflow records routing decisions.

## Loaded-model estimates

The cold estimate is model weights plus known conventional KV requirements, or
the calibrated MLX footprint envelope including its full uncertainty margin.
The reserve remains 4 GiB. A warm estimate subtracts only reusable weights:

`additional = cold estimate − matched resident weight credit`

Credit requires one current inventory entry with the exact name and installed
manifest digest, matching backend, a future expiry and context capacity at least
as large as the next request. It is capped by model-file size and reported
resident size, and by reported VRAM on MLX. KV, backend workspace and calibration
margins are not credited. Unknown or mismatched entries receive zero credit.

Context selection uses this estimate before optional Smriti paging. Dispatch
checks fresh system pressure, residency and installed model identity under the
Sutra inference lease. A model that disappeared, changed or cannot accommodate
the selected context cannot inherit its earlier admission.

These are estimates, not a measured peak-memory guarantee. Shared Ollama clients
can change residency after observation. Credit never increases the OS available
memory measurement and does not authorize unloading another client's model.
The inventory fields come from [Ollama's running-model API](https://docs.ollama.com/api/ps).

## Governed Auto fallback

For workspace children, synthesis and coding, a capacity refusal **before
inference** can propose the next smaller configured tier. The central state
machine records `RESOURCE_MODEL_FALLBACK` after kernel and remaining-allocation
checks. There are at most two selections per workflow, including across reload
and Resume. Equal-size or larger candidates, repeated names and known aliases of
the same weights are skipped. Explicit low/mid/high selections remain fixed.

The next workflow step selects fresh source and Smriti pages for that model and
passes through the ordinary invocation gateway. The original task, pinned
constraints, permissions and completed child results remain authoritative. Each
read and invocation consumes its normal allocation; fallback refunds nothing.
If mandatory context does not fit, the task stops rather than dropping pins.

Fallback does not handle OS thermal/memory pressure, busy slots, identity changes
or interrupted calls. Those retain the existing pause/recovery path. Report
reviewer independence remains enforced; an unavailable independent reviewer can
still withhold a report. A smaller model is an opportunity to continue within
budget, not a promise of equal answer quality.

Sutra Monitor shows cold estimate, resident weight credit, additional reservation
and recorded fallback. These historical plans are checked again before dispatch.

## Validation

`test_memory_profiles`, `test_runtime_memory` and `test_resource_routing` cover
identity/expiry/context rejection, disappearing residency, pressure stops,
bounded fallback, child preservation, re-paging, reload, permissions and charged
budgets. The browser suite checks visible accounting and safe plain-text output.

The opt-in `test/live-resource-fallback.py --output /path/to/new/artifacts`
uses isolated state and installed local models. It preloads only after a cold
plan fits, exercises low → high → low memory continuity, and injects one capacity
refusal to exercise real-model fallback without deliberately exhausting RAM.
Its results distinguish live inference from the injected admission condition.

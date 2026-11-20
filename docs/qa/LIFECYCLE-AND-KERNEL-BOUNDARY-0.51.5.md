# Sutra 0.51.5: lifecycle and kernel boundary audit

Date: 2026-11-19. This is a source audit, not a new inference or cancellation experiment. The implementation described below is current; the recommendations are proposed work and are not included in this build.

## Current verified implementation

### Kernel admission and host timing have separate responsibilities

The domain adapters supply normalized measurements to the canonical STP kernel. Before model dispatch, the host admits the call's token and seconds reservation against the task and orchestration owners, then commits its envelope and charge. The kernel does not operate the HTTP connection, cancel a backend request or manage the VS Code response slot. See [gateway admission](../../harness/invocation_gateway.py#L199) and [orchestration admission](../../harness/orchestration.py#L275).

The canonical kernel does **not** timestamp itself. The host's `KernelBoundary.evaluate` constructs the kernel, calls `time.perf_counter_ns()` around `kernel.evaluate(omega, sigma)`, and records that measured interval as an observe-only `kernel` sample. Validation, kernel construction and later recording are outside that measured interval. Other host measurements, including the enclosing budget gate, are separate. These samples do not modify the decision or certify backend performance. See [kernel boundary](../../harness/kernel_boundary.py#L25), [budget gate measurement](../../harness/orchestration.py#L171) and [bounded host metrics](../../harness/accounting_metrics.py#L25).

Canonical kernel SHA-256, rechecked for this audit: `9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`. No kernel source was changed.

### Current limits are explicit host policy

| Boundary | Current behavior |
| --- | --- |
| Ordinary/legacy model transport | 150 seconds. |
| Opted-in report/claim transport | 360 seconds when output reserve exceeds 2,048 and the provider supports an enforceable deadline; otherwise the existing 150-second path. Owned-engine calls remain on their existing 150-second protocol. |
| New timed-call bridge headroom | Requires the call limit plus 30 seconds before registering a model invocation. Insufficient headroom yields a fresh bridge step without dispatch. |
| Python bridge operation | 450-second lifecycle deadline, with SIGTERM/SIGINT cancellation handlers. |
| JavaScript worker watchdog | 480 seconds; SIGTERM followed by a 10-second SIGKILL fallback. |

The 150/360/30 values are versioned host timing policy, not measurements calculated by the STP kernel. Eligible calls commit the same seconds value used for admission and transport; offline interruption reconciliation retains that committed reservation. See [timing policy](../../harness/invocation_timing.py#L4), [provider support](../../harness/invocation_timing.py#L28), [headroom](../../harness/invocation_timing.py#L79), [bridge deadline](../../harness/lifecycle.py#L53) and [worker watchdog](../../bridge_worker.js#L141).

Marked timing envelopes also receive a hard call alarm. The legacy path instead checks remaining time between transport reads and sets socket timeouts. A buffered read receiving a slow trickle can therefore outlast its intended per-call deadline before the next check; the outer bridge deadline still applies. See [gateway alarm selection](../../harness/invocation_gateway.py#L599) and [local stream reads](../../harness/local_model.py#L72).

### Local serialization is stronger than remote-stop confirmation

The worker allows one pending request, the native provider rejects overlapping operations, and workspace/runtime locks serialize local owners. On timeout, the HTTP connection is closed and the gateway quarantines and settles the invocation. Native `running` is cleared during response cleanup. These are local lifecycle events; they do not prove that the remote backend has stopped computing. See [worker serialization](../../bridge_worker.js#L128), [native request guard](../../native_provider.js#L656), [transport cleanup](../../harness/local_model.py#L105) and [native cleanup](../../native_provider.js#L1240).

There are existing conservative safeguards. Enabled Ollama runtime management retains an uncertain active marker when an incomplete call's model remains resident, and blocks subsequent dispatch while that model is still observed. Strata requires `activity.in_flight == 0` before dispatch. These observations are useful, but neither provides a shared request-specific cancellation acknowledgment. In particular, model absence is not proof that a previously queued request cannot later begin. See [Ollama interruption guard](../../harness/runtime_memory.py#L355), [release observation](../../harness/runtime_memory.py#L395) and [Strata activity admission](../../harness/strata_model.py#L518).

One concrete early-release edge is `BridgeWorker.fail`: it sends SIGTERM, calls `onClose` and rejects the request before receiving the child's actual close event. A replacement worker can then be created; local locks still constrain its execution. Normal cancellation may also finish after a worker response/end marker without a remote-stop acknowledgment. See [worker failure](../../bridge_worker.js#L117), [response completion](../../bridge_worker.js#L68) and [replacement worker creation](../../extension.js#L239).

### Causal records exist, but are not one complete attempt trace

The invocation envelope binds invocation ID, call ID, run, delivery, request hash, model identity, resource observation and admission frames. Receipts retain the envelope hash, measured elapsed seconds, usage and selected failure kinds. Model activity events carry invocation/run/session/owner IDs. However, receipt elapsed time starts after registration, so it excludes preceding admission and preparation. Host gate timings are bounded in-memory aggregates; native routing uses a separate audit ID, and backend HTTP requests do not carry the gateway call ID. See [envelope](../../harness/invocation_gateway.py#L78), [receipt](../../harness/invocation_gateway.py#L255), [transport clock](../../harness/invocation_gateway.py#L599) and [activity identity](../../harness/model_activity.py#L13).

Provider construction, reasoning checks, runtime admission, identity observation, context counting, storage checks and kernel denial can fail before the provider-call exception handler. They may correctly create no model invocation or charge. `Interrupted` derives from `BaseException`, bypasses that handler and is reconciled later using the committed reservation. Finalization itself can also fail after backend work has completed. These outcomes must not all be described as a failed model generation. See [pre-dispatch boundary](../../harness/invocation_gateway.py#L540), [call exception handler](../../harness/invocation_gateway.py#L608) and [interruption reconciliation](../../harness/lifecycle.py#L100).

## Proposed work, ranked

1. **Track backend drain separately from local response completion.** Record cancellation requested, transport closed, worker exited and backend stopped as distinct states. A saved UI pause can return promptly while new inference remains blocked when backend ownership is uncertain. Use a request-specific acknowledgment where supported; otherwise retain an explicit unknown state rather than treating a closed socket or absent model as acknowledgment.
2. **Use enforceable absolute deadlines consistently.** Separate queue/load, prefill, generation and cleanup intervals. Any future performance-derived limit should use observed backend phase measurements, bounded margins and an explicit versioned policy. Admit its exact seconds before dispatch; preserve historical receipts and retain a separate bounded cleanup period. Kernel evaluation time cannot predict model generation time.
3. **Create one attempt trace before preflight.** Carry a single attempt ID through the native request, bridge, resource/context/kernel gates, invocation, transport and cancellation/reconciliation records. Record monotonic phase durations and terminal reasons without source, prompts or response bodies. Failed preflight should retain a diagnostic attempt record without inventing a model call or charge.
4. **Keep failure categories distinct.** Identify pre-dispatch denial, transport timeout, explicit cancellation, unknown backend completion, invalid output and receipt/finalization failure separately. Measure kernel-call duration and surrounding host overhead separately; compare repeated observations with disclosed model, hardware and workload conditions before changing policy.

None of these recommendations has been implemented by this audit. No inference, UI action, live-ledger access, preference change or source modification was performed to produce this document.

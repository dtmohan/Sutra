# Sutra 0.53.3 validation

Date: 2026-11-20. Scope: shared-Ollama recovery from a retained interrupted-call
marker. The canonical STP kernel, configured model tiers, backend deadlines,
workspace authorization and prior ledger spending remain unchanged.

## Confirmed defect

The shared runtime contained an uncertain active marker from the earlier timed-out
greeting. `/api/ps` showed `qwen3:4b` loaded. The dispatch guard interpreted matching
model residency as evidence that the old request was still running. Another client
can keep this model warm indefinitely, so the guard could refuse every explicit
retry even after the Sutra OS lease was free. The ownership stop was also labelled
as missing telemetry rather than uncertain runtime ownership.

The fix separates uncertain backend completion from current host dispatch
ownership. Shared-mode explicit requests still require the OS lease, fresh
hardware admission, matching model metadata and admitted memory allocation.
Uncertainty is retained in a bounded diagnostic and never grants model-unload
authority. Neither empty inventory nor a later successful request clears it.
Exclusive-mode uncertainty remains a refusal and has the correct category.

## Live reproduction

Two private trial stores copied the actual shared-runtime state. The original
runtime file was byte-for-byte unchanged by both probes. Each used the public
`vscode_bridge.handle` orchestration path, prompt `hello`, Adaptive reasoning,
Auto routing and the existing Low/Mid/High selections. Fresh AppleSMC/OS telemetry
ran during the call. No injected provider, hardware bypass, manual unload, server
restart, settings rewrite or user-ledger reset was used.

Both trials observed the same loaded `qwen3:4b` manifest digest
`359d7dd4bcdab3d86b87d73ac27966f4dbb9f5efdfcc75d34a8764a09474fae7`
and 32,768-token resident context before dispatch.

| Runtime | Result | Elapsed | Model calls |
| --- | --- | ---: | ---: |
| Installed 0.53.2 | PAUSED; interrupted-model guard; mislabelled missing measurement | 0.240 s | 0 |
| Patched 0.53.3 | DONE; accepted greeting | 2.997 s | 1 |

The accepted call reported 1,618 input and 77 output tokens; settlement charged
1,695 measured tokens. This was a new private trial, not a refund of the earlier
failed request. The previous interrupted-call identity remained in the diagnostic
summary with `backend_completion_verified=false`; current active ownership was
cleared after completion. No workspace tool was used.

These are real local-provider API trials, not a claim that the user's existing
VS Code chat was driven end to end. Shared-provider queueing can still delay or
time out a request; this patch removes the false residency barrier.

## Automated validation

- 209 focused Python tests passed in 32.063 seconds: runtime memory, provider
  waits/transport, saved-request recovery, conversations and memory/routing,
  orchestration, gateway, activity, kernel, context/allocation recovery and claim
  timeout recovery.
- The saved-request regression wraps fake inference in real runtime dispatch,
  then times out, reloads the ledger and resumes the same workflow on another
  model. It checks the original contract, pinned statement, parent, immutable
  receipt prefix, consumed calls/tokens, unchanged source/operation spending and
  unchanged allocation. HTTP is forbidden in this deterministic regression.
- Runtime tests cover a held cross-process lease, crash release, resident legacy
  markers, hardware/memory/identity refusals, incomplete calls with empty
  inventory, shared-to-exclusive switches and independent unload suppression.
- 78 native-provider checks passed. Bridge-worker, runtime-integrity and branding
  Node suites passed.
- 21 packaged VS Code host checks passed in an isolated profile, including real
  thermal telemetry, governed source mapping, task preservation and revocation.
- Package contents/release metadata and `git diff --check` passed.

## Artifact

- VSIX: `dist/Sutra-0.53.3.vsix`, 5,140,232 bytes, 295 runtime files.
- VSIX SHA-256: `a69cfe947c67b61787c0f707fd96cf6ebf4c2f53396d638af4f2d6288e37f2e1`.
- Unchanged canonical kernel SHA-256:
  `9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`.
- Released 0.53.2 conversation policy is registered for data-only continuity;
  executable checkpoints and tool permissions are still subject to current checks.

## Runner and NPU scope

This patch does not turn the experimental NPU retrieval queue into a chat router.
The shipped native engine has a separate service, while the persistent Python
bridge still belongs to the extension host. A future coordinator can own durable
request identity, cancellation acknowledgement and completion receipts across UI
reconnection. NPU embeddings/retrieval can be another bounded worker behind the
existing gateway; they do not establish intent, evidence truth or permissions.

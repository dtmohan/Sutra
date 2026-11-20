# Sutra 0.54.0 validation

Date: 2026-11-20. Scope: persistent request coordination independent of the VS Code
UI. Includes the 0.53.3 shared-model interruption fix. The canonical STP kernel,
configured model tiers, provider deadlines and task budget rules are unchanged.

## Delivered behavior

- A detached, authenticated local coordinator owns the persistent Python bridge.
  Chat disconnects retain accepted execution, result identity and bounded activity.
- Native chat saves a service-issued ticket before submitting work. Same-chat
  `continue` reconnects by identity; it never resubmits an ambiguous operation.
- Reconnection checks the current core workflow before using cached output. Stale
  policies use existing recovery, and changed checkpoints supersede cached bodies.
- Stop runner is distinct from stopping display. Cancellation remains pending
  until the host worker exits; shared-provider completion is not thereby verified.
- Queued native work checks the exact caller grant generation, scope/profile and
  private record bytes before dispatch. Revocation cancels queued work first.
- Coordinator restart, missing delivery records and failed journal writes recover
  through durable workflow state without refunding spending or replaying actions.
- Monitor displays fresh/stale runner status and scoped Stop/Dismiss controls.
  Routine refresh preserves keyboard focus on an unchanged Dismiss control.

The coordinator retains one accepted bridge step. The existing task state machine
still governs subsequent steps and approvals. The NPU retrieval prototype is not
enabled or shipped as an incoming-message reasoning model by this release.

## Validation

The complete `scripts/qa.sh --with-package --browser --vscode` run covered 33
gates. It passed 32; its sole failure was an older hardening VM fixture resolving
the new coordinator import relative to the test directory. The fixture was updated
to model the new client boundary while retaining its existing assertions. That
gate then passed independently; runtime files did not change for this fixture fix.
All 33 gate areas are therefore covered by passing results.

- Full Python harness: **1,741 tests**, 8 skipped, 509.399 seconds, passed.
- Native-provider suite: **90 checks**, including same-chat reconnect, per-turn
  reasoning, current-policy inspection, missing-record recovery, revocation and
  grant replacement/downgrade during asynchronous operations.
- Coordinator backend: **16 actual TCP/process scenarios**, including immutable
  IDs/model environments, token/version/workspace/chat binding, ordered bounded
  event replay, cancellation after child exit, byte-reserved queue bounds,
  acknowledged-cache eviction without replay, daemon crashes, missing records,
  completion-write failures and altered-cache digest rejection.
- Coordinator client: **8 checks**, including actual Python bridge continuity,
  UI client disposal/recreation, subscription cancellation versus runner Stop,
  concurrent bootstrap, empty startup-lock recovery, startup-file cleanup and
  authority-only requests without managed transport IDs.
- Real Python bridge fixtures retain an existing charged invocation, task
  contract, source checkpoint, permissions and cumulative spending across
  disconnects and per-request configuration changes. Deterministic model-switch
  recovery also checks pinned facts, receipt prefixes and remaining allocations.
- Hardening VM: **22 existing assertion groups** passed after fixture migration.
  It continues exercising actual BridgeWorker framing with fake Python streams.
- Browser fixtures passed, including runner freshness, Stop/Dismiss identities,
  stale-action disabling, escaped labels, focused-control retention and narrow
  layouts. Browser values are fixture data, not actual hardware measurements.
- Packaged VS Code host: **23 checks** passed in an isolated profile. These include
  actual source tools through the independent coordinator and Python bridge,
  current runtime authentication, AppleSMC telemetry, task preservation and
  permission revocation. Test-owned coordinators are shut down by the fixture.
- Packaged contents, runtime integrity, branding and whitespace checks passed.

The first packaged test exposed macOS `/var` versus `/private/var` aliases in
permission-record paths. Host evidence now canonicalizes the parent directory
while retaining the exact record bytes and expected authority epoch. The packaged
mapping and revocation checks passed after that correction.

## Live local-model trial

A private workspace/store copied the existing uncertain shared-Ollama marker.
The original user runtime file remained byte-for-byte unchanged. The real
CoordinatorClient submitted `hello` through the normal orchestration gateway,
observed `running`, then detached. A new client reattached to the same identity.

- Model: existing Low, `qwen3:4b`; Adaptive settings preserved.
- Final state: **DONE**, accepted greeting.
- End-to-end elapsed: **2.977 seconds** (earlier trial: 3.063 seconds).
- Exactly **one** accepted model call: 1,618 input and 77 output tokens.
- Start/control/finish activity retained the same invocation identity.
- No workspace tools, model escalation, explicit shared-model unload, server
  restart, user settings rewrite, budget reset or automatic retry was used.

This is a real coordinator/client/provider trial, not a claim that the user's
existing chat was manually driven end to end. The packaged VS Code test separately
exercises the production UI integration with model-free source operations.

## Artifact and integrity

- `dist/Sutra-0.54.0.vsix`: **5,157,517 bytes**, **297 runtime files**.
- VSIX SHA-256:
  `16d50495f3cb0215b8c3c498eaf2679273170dd557c82669644bd3a1251b2643`.
- Policy SHA-256:
  `000d1b280d5635767280cf60d553378145cf34d772cf812ef8f4709ebb7503a7`.
- Unchanged canonical kernel SHA-256:
  `9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`.

Runtime files in the VSIX match the checkout. The source archive includes the
updated tests, coordinator design and this validation record. Transport caches
are bounded disposable data; authoritative evidence and spending remain in the
existing ledger. Journal hashes detect accidental or naive rewrites, not a
privileged replacement of the cache and its digest.

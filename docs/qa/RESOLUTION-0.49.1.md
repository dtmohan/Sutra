# Resolution register — Sutra 0.49.1

The original [0.49.0 findings](FINDINGS-0.49.0.md),
[live session](LIVE-0.49.0-native-session.md) and QA charter are retained as supplied.
This register tracks fixes separately. A code change is not a live sign-off.
Consent was tested with real dialogs in a normal, isolated VS Code window; its
backend was deterministic. Native inference and continuity were tested separately
through the real gateway. These are distinct validation scopes.
The STP kernel retains SHA-256
`9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`.

| ID | Resolution and evidence | Validation status |
| --- | --- | --- |
| L-01 | Host-owned access link, notification action and Monitor button; permission work occurs after the model response ends | Real VS Code provider/dialog journey and native-provider/Monitor tests pass |
| L-02 | Dismissal preserves the request and cannot interrupt the already-finished model response | Real modal dismissed, then reopened and approved; no source operation or interruption error before consent |
| L-03 | Never use generic Open Chat to resume; show the request and offer a copied instruction bound to its original chat/cursor | Real clipboard/dialog journey passes; foreign chat, workspace, stale cursor and replay tests pass. Direct session targeting remains unavailable |
| L-04 | Explain VS Code hosting/Copilot labels and actual Sutra routing in Settings | In-product copy added; host-owned chat chrome remains outside Sutra's control |
| L-05 | Distinct runtime busy/timeout/uncertain, missing measurement, allocation and hardware stops with saved observations | Typed wait tests pass; a real cold-memory pause recovered on the same native task |
| L-06 | Keep metadata-only calls, progress and observe-only commitments in Monitor; actual reasoning still appears inline | Activity, native-provider and browser tests pass; persisted per-call diagnostics retained |
| L-07 | Workspace access wording across command, modal, status, chat and Monitor | Native-provider and browser tests pass; active UI locators updated |
| L-08 | Mark Current/Parked requests and show state plus short identity | Open-task/reload/focus/model-switch tests pass; independent tasks remain independent |
| E-01 | User-only approval binds executable content, tokenizer/shaders and profile; sanitized child/bootstrap environments | Engine, settings and host approval regressions pass; large weights remain metadata-bound |
| E-02 | Verify ownership/identity before replacement; no competing worker while an unresponsive owner holds the lease | Lifecycle and restart tests pass; absolute startup and nonblocking pipe-write deadlines tested |
| E-03 | Startup uses the selected profile and no longer fires on unrelated settings changes or silently replaces the selection | Startup and manager regressions pass |
| E-04 | Verify native CLI/server identity; owned-worker Loaded also requires a live, generation-ready child | Manager tests cover unverified, failed and uncertain states |
| E-05 | Preserve typed provider failure causes without inappropriate memory release or model fallback | Provider-wait and orchestration regressions pass |
| E-06 | Retain pending loading feedback while state pushes arrive | Unit and real browser suites pass |
| E-07 | Compare every expected `runtime_files()` member with archive entries; reject omissions, duplicates and unexpected members | Five release-gate tests pass, including omitted worker, codec, license and notices |
| E-08 | Single top-level Changelog heading, current release first and nonempty, matching README and package versions | Positive and negative metadata regressions pass |
| E-09 | Periodic grant reconciliation backs up file notifications | Real two-window revocation/cancellation and deterministic silent-watcher regression passed, including five repeated runs |
| E-10 | Nonce, sequence and HMAC on v2 IPC; bounded replay window | Authenticated framing and replay regressions pass |
| E-11 | Random private socket directory with owner/mode validation and cleanup | Lifecycle tests pass; real shutdown removes the fixture worker endpoint |
| E-12 | Reject unsafe registry ownership, modes and link types | Profile regressions pass |
| E-13 | Per-profile state, verified readiness and refreshed discovery after management actions | Manager and browser regressions pass |
| E-14 | Preserve unsaved model drafts through refresh and failed save | Browser regression passes |
| E-15 | Explain shared model weights/capabilities instead of promising smaller/larger models | Updated tier copy and browser tests pass |
| E-16 | Disable unsupported native controls before interaction | Platform-gated manager tests pass |
| E-17 | Validate progress and usage types/ranges; withhold invalid output and settle observations conservatively | Engine tests pass, including malformed frames and partial timeout usage |
| E-18 | Hash the engine's executable/admission dependency closure, not unrelated experiments | Fingerprint tests pass; live model switching/restart succeeds |
| E-19 | Mark native-tier validation explicitly as the historical 0.48.0 run; retain its original timings/count | Documentation updated; current aggregate counts intentionally not substituted |
| E-20 | Installed tokenizer tests require an explicit pack path; ordinary discovery reads no user pack | Portable discovery skips the real pack; all 15 codec tests pass with the explicitly selected owned Engine tokenizer/venv. Catalog isolation remains assigned to engine tests |
| QF-24 | Follow editor diagram theme, including SVG export | Browser regression passed light/dark/high-contrast changes, theme-preserving SVG export and hostile-label screening; screenshots in `/private/tmp/sutra-0491-ui` |
| QF-33 | Accurate `/new` command help | Native help/command regression passes |

No result in this register authorizes bypassing permissions, resetting consumed
spending or changing the canonical kernel. Engine lifecycle and model-quality
claims require separate measurements; model profiles sharing weights are not
independent reviewer models.

## Live validation — 2026-11-18

`test/live-permission-journey.py` runs a normal isolated VS Code development
window, the real `native_provider` and `vscode.lm` API, real permission dialogs,
and a deterministic backend. The first response finishes before consent is
requested. Dismissing the dialog leaves the saved request intact and performs
no source operation. Resume copies an instruction without opening another chat;
approval then resumes the same flow on Mid with the original read-only, no
network/dependency constraints and cumulative fixture spending (one to two
calls). Five screenshots and the transcript are retained with its result.

This is not a full Agent-mode inference test. A direct logged-out Agent UI
attempt encountered VS Code's Copilot/MCP preprocessing before Sutra's provider
was invoked. Sutra neither initiated sign-in on Resume nor started that MCP
server. Host chat labels/checkpoint controls remain owned by VS Code. The
packaged-host smoke separately verifies Local Chat selection and fresh source
mapping through the actual bridge.

`test/live-engine-worker.py` used the real owned engine, real gateway and a new
caller process for every phase:

| Phase | Seconds | Input tokens | Output tokens |
| --- | ---: | ---: | ---: |
| Low / 16K | 17.594 | 2,483 | 148 |
| Mid / 24K | 15.271 | 2,762 | 136 |
| Worker restart into High / 32K | 15.293 | 2,969 | 125 |
| Fresh caller, warm High / 32K | 12.521 | 3,126 | 125 |

The first Mid activation hit a genuine cold-memory allocation stop while a
separate managed runtime was resident. It generated no model output. The same
saved task recovered after releasing that verified idle competing instance;
no ledger was recreated or allocation refunded. Final spending was four model
calls and 11,874 settled tokens. All responses retained Cedar-724, CED724, target
18, actual 21, difference 3 and the prohibited operations. The verifier checked
unchanged contracts/limits, permission flags, monotonic spending, receipt
prefixes, valid audit chains and fresh worker/model identities. The profiles
share weights; these results establish continuity rather than capability gains.

## Remaining limits

- The compatibility Resume action requires returning to the original Local
  Sutra chat. It does not automatically navigate to an unsupported session URI.
- Exact engine binary/tokenizer/shader fingerprints are user-approved. Large
  weights retain file metadata bindings; this is not model-weight attestation
  or protection against unrestricted same-user compromise.
- An unresponsive lock-holding owner pauses replacement; Sutra does not kill an
  arbitrary process. V1 compatibility allows authenticated idle status/shutdown
  only, never model execution.
- Older QF-23's duplicate token presentation code and QF-27's formatting/lint
  debt remain maintenance work. The two views share token aggregation helpers;
  this release does not claim a renderer consolidation.
- Apple Neural Engine/NPU support is deferred, with no NPU runtime changes.

## Release gates

The final `scripts/qa.sh --browser --engine-codec --vscode` run passed all
27 gates. The Python suite ran 1,038 tests with seven optional installed-pack
skips; the explicit installed-pack codec gate ran all 15 tests successfully.
The Node contract suites, four real Chrome webview suites, runtime integrity,
complete packaged contents and isolated packaged VS Code smoke test passed.
The shipped runtime contains 278 files and excludes test/experiment scripts.
The user's VS Code settings hash is unchanged; validation used separate profiles
and environment-local model assignments. The owned validation worker was shut
down and the pre-existing managed Strata profile restored to generation-ready.

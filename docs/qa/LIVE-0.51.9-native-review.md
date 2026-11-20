# Sutra 0.51.9 native review: continuity passes, architecture acceptance fails

Date: 2026-11-19. This was the existing signed-in VS Code chat, using its native model picker, chat input, cancellation and Sutra commands. Inference was not substituted with a programmatic benchmark. Runtime source remained unchanged throughout the trial.

## Task and release

The original request was: “can you review the workspace project and go through the tools logic and help me understand the architecture in detail? a few block diagrams would be great”, preceded by “Start a fresh read-only review and keep earlier tasks saved.”

- Task: `review-25447a34c017`.
- Current successor flow: `1401791faff04132a0854ecdf628e4b8`.
- Installed VSIX: `Sutra-0.51.9.vsix`, SHA-256 `51463e41a986cddf510db0bb15d84f4194ef3b8d36a6a379abf3a57e0abdc760`.
- Release validation: 26 gates passed; 1,501 Python tests, eight skipped; Node, browser and packaging checks passed.
- Canonical STP SHA-256 remains `9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`.

## Reasoning controls

New discovery packets honor explicit On, Off and Model default. The next-request On override applies to orchestration planning and answering; delegated children retain the configured base preference. Existing permissions and budgets do not become child-controlled.

The actual High selectors requested the supported named `low` control under Adaptive. A screenshot verified completed thinking blocks and an expanded live architect stream in the native chat. The accessibility tree omitted those widgets; its omission was not evidence of a rendering failure. The diagnostic archive also contained actual reasoning text.

On selects the lowest advertised positive control. On this High model, Adaptive already selected that same control, so the toggle would not establish a stronger-effort comparison.

## Allocation and safe recovery

The trial began with 400,167 previously consumed units. It paused at 421,591 because the next three-call planning forecast exceeded the remainder of the 500,000 automatic ceiling. The user explicitly approved the displayed one-task amendment:

| Dimension | Approved total |
| --- | ---: |
| Model budget units | 921,591 |
| Calls | 92 |
| Repository operations | 1,094 |
| Source bytes | 94,387,070 |
| Execution seconds | 3,204 |

The native confirmation created amendment version six with authority `host_user_confirmation`. The workspace automatic ceiling was unchanged. Previously consumed units were not reset.

Nineteen accepted High selectors retained the same discovery chain and actually received source-body floors. Discovery nevertheless remained slow and sometimes repeated navigation. In particular, three identical bridge searches returned the same five source positions. The actual handler body lay beyond the partial read, so this did not prove that the model ignored a supplied implementation.

## Actual switch to Mid

Native cancellation at a settled boundary preserved exactly 773,640 units and zero pending operations. The same chat was switched to Sutra Mid and sent `continue`.

The first Mid attempt stopped before inference: its cold estimate of 26.1 GiB plus a 4 GiB system reserve exceeded the 24.6 GiB available while High was resident. No inference charge was added. After confirming the owned High runtime was idle, its normal Stop command released memory; available memory rose to approximately 48 GiB. Shared Ollama was not stopped or explicitly unloaded.

Mid then dispatched with the same flow, discovery key, previous High checkpoint and 19-note root. No earlier selectors were replayed. Its packet retained scoped source and the required source floor. Mid used the existing conservative context profile, not High's exact physical-token proof.

Three Mid proposals were quarantined for an unknown read cursor. The hash was present in the delivered memory directory: the model copied a directory reference into `cursor.sha256`. Random fabrication was therefore not established. No repository operation executed for these rejected proposals. The second and third requests were byte-identical, with identical repair feedback, temperature zero, seed 42 and requested `low` control. The final retry supplied no new input or control.

The existing three-rejection cap correctly completed discovery with `proposal_rejected`; it did not issue a fourth selector. A deliberate settled-boundary pause and resume preserved that result and advanced to the parent writer.

## Parent and publication

Parent candidate `257b135d4feb49059be484d89f5ed033` was accepted structurally. Its measured call took 208.62 seconds and reported 4,515 input plus 2,998 output tokens. Its real source input comprised 11,967 bytes across four partial windows:

- `extension.js:955–987`;
- `harness/architecture_runtime.py:113–177`;
- `harness/repository_tools.py:290–348`;
- `native_provider.js:1282–1309`.

Independent inspection found none of seven central mechanisms fully explained: native chat to orchestration, task permissions and fork/join, invocation admission, adapters to canonical STP, repository effects to receipts, durable memory/checkpoint recovery, and claim publication. Local fragments of authorization and budgeting were present. Four citation strings validated, but function/registration headers did not support several detailed guarantees. Some component paths were misassigned, and the draft treated the legacy architecture dispatch path as the review core without establishing the native Auto route.

The proposal contained no diagram specification. A host-derived ordered flow would inherit that unproved route; the separate local-import graph is not a second verified execution diagram. No successful publication or diagram-rendering acceptance is claimed.

Claim review never dispatched. Three consecutive fresh bridge requests recorded `MODEL_SLICE_DEFERRED`, approximately 78 seconds apart. Preparation consumed enough of the 450-second bridge window that the required 360-second model allowance plus 30-second headroom no longer fit. A new bridge repeated the same preparation. The guard depends on the reserved output size, so Thinking Off alone is not a demonstrated remedy. No reasoning setting was changed for this proposed diagnostic.

The task was safely paused with its accepted candidate retained, rather than continuing this zero-model-charge host loop.

## Final ledger and latency evidence

Final settled units: **807,425**, with zero pending model or repository operations. New trial spending was 407,258: 373,473 for 19 accepted High selectors, 26,272 for three quarantined Mid selectors, and 7,513 for the accepted Mid parent. The original 400,167-unit prefix and every earlier checked model/source prefix remained unchanged. Remaining task capacity was 114,166 units, 30 calls and approximately 454.97 charged seconds.

High model calls often spent approximately 44–49 seconds on prefill and 9–19 seconds on decode; later prefill also slowed. Unattributed gaps between calls grew from approximately 31 to 75 seconds. These residuals are not all kernel time or all model time.

One two-second native sample of the correct bridge process caught JSON processing, SHA256 and SQLite external-sort spill/merge, not a main-thread model-transport wait. A read-only query-plan check confirmed that repository receipt loading sorts rows containing large JSON. The sample lacks Python function names, so the precise share attributable to that reader remains unproven.

An isolated, unapplied late-materialization candidate sorts small identities before fetching payloads under the same snapshot. Eleven small functional cases pass, including unrelated concurrent commits, subsequent corruption detection, pending rows and local mutation fences. A synthetic reader benchmark before the final narrow-fence revision reduced approximately 46 MB of sort spill to zero and reader time from approximately 0.13 to 0.06 seconds. This is not an end-to-end speedup claim. Its 0.623-second run overlapped live inference around epoch 1795153895; that overlap is recorded rather than treating the run as an unperturbed performance benchmark.

## Remaining work

The release's reasoning override, inline rendering and model-switch continuity passed the observed checks. The detailed architecture-review acceptance test failed. Required follow-up concerns source selection and convergence, informative repair across cursor namespaces, and preventing repeated headroom deferrals. The receipt-reader optimization remains isolated and has not been applied or packaged. No kernel modification, budget reset or successful full-review claim is implied by this report.

Compact audit: `/private/tmp/sutra0519-paused-audit.json`. Cursor provenance: `/private/tmp/sutra0519-mid-cursor-provenance.json`. Retry comparison: `/private/tmp/sutra0519-mid-repair-comparison.json`. Visual inline evidence: `/private/tmp/sutra0519-live-screen.png`.

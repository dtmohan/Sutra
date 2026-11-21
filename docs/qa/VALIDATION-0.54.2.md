# Sutra 0.54.2 validation

Date: 2026-11-20. Scope: project-review output intent, source selection, reviewer
reference representation and native chat contention. The canonical STP kernel,
configured model tiers, task limits and permission boundaries are unchanged.

## Observed failure

The caller asked for thoughts on the workspace codebase, what was strong or weak,
and an overall control-flow diagram. The released classifier matched only
`architectur|overview|strengths|shortcomings`. That wording therefore received a
generic answer schema instead of a structured report. The wire instructions still
described how to fill an architecture report, although no report field existed.

The actual saved synthesis emitted 441 output tokens with an 8,192-token allowance
and substantial task capacity remaining. Its answer was a short verdict; a richer
report was not hidden by rendering. The classifier miss also skipped report
discovery, independent claim-review calls, synthesis reasoning headroom and the
request-flow diagram. An earlier Mid-model memory refusal pinned the governed Low
fallback. No stronger model produced that answer.

Two separate UI failures could strand local busy state: a diagnostic callback
throwing before tracking cleanup, and a response/footer stream throwing before
local cancellation/display state was released. Genuine overlap was also surfaced
as a generic VS Code exception, including transient host setup contention.

## Changes

- New deliveries bind a caller-derived presentation profile. The same profile
  selects report schema, source-query priorities, governed follow-up discovery,
  synthesis controls, claim review and diagrams. Source and prior model notes
  cannot choose that profile. Flag-absent historical schema and wire hashes remain
  identical to the released recipe.
- Auto's new report signal makes stronger synthesis eligible only after fresh
  source. Explicit tiers and resource fallback remain authoritative. No semantic
  difficulty measurement or resource admission is inferred from the genre.
- Report prompts request concrete supplied function names, inputs, helper calls
  and results. Static analyzer gaps alone do not establish runtime defects.
- Repeated valid evidence IDs in new claim-review responses are normalized as
  stable sets only after validating the raw response against each claim's ID
  choices and bounds. Verdicts, reasons and source scope are unchanged. Raw order
  and multiplicity are retained in a versioned receipt record and reconstructed
  before verifying the original model-response hash. Historical duplicate-ID
  rejection remains unchanged.
- New reviewer wire packets select supporting evidence, conflicting evidence or
  neither. The host derives supported, contradicted or insufficient-evidence from
  that choice. Both channels populated is an explicit rejection. There is no
  separate model-authored verdict that can contradict an empty evidence list.
  Raw scope validation, the host judgment schema and full raw-hash recovery stay
  intact; no missing source reference is invented.
- Busy requests receive host-owned chat guidance without duplicate submission or
  consuming a pending thinking override. A short cancellable readiness wait
  handles transient host setup. Diagnostic and footer failures cannot retain a
  false local execution owner; a real coordinator ticket remains reconnectable.
- Completed reviews disclose a committed Low resource fallback.

## Controlled local-model experiments

Private stores and frozen runtime copies were used. The original task and VS Code
settings were untouched. Both arms used the same copied 12-file checkout, caller
prompt, `qwen3:4b`, Thinking Off, temperature 0, seed 42, 32K context and 8,192
output allowance. Calls used the existing invocation gateway, shared cross-window
OS lease and actual fresh hardware checks. No gate was bypassed.

| Run | Total time | Model calls | Input / output tokens | Result |
| --- | ---: | ---: | ---: | --- |
| Released 0.54.1 | 13.27 s | 1 | 5,653 / 323 | Short generic answer; no structured report or flow view |
| Initial structured candidate | 76.16 s | 3 | 14,819 / 3,485 | Structured draft; publication correctly withheld after repeated evidence-ID failures |
| Corrected candidate | 62.77 s | 3 | 11,318 / 2,892 | Published reviewed report and both diagram views |
| Auto High before grounded-judgment wire | 201.18 s | 4 | 34,700 / 5,659 | Deeper draft; publication withheld after invalid verdict/evidence pairings |
| Auto High with grounded-judgment wire | 219.56 s | 3 | 31,773 / 6,373 | Published function-level report and both diagram views; no retry |

The first two runs delivered exactly the same eight excerpts: 8,733 bytes with
identical per-path hashes. The corrected candidate also applied architectural
source priorities and delivered a different eight-path set, 6,988 bytes. The final
comparison therefore measures the combined pipeline change, not a pure prompt-only
effect. All recorded wire hashes and invocation receipts verified.

The final Low draft had four components, four flow steps, two strengths, two risks
and three next checks. Claim review retained 11 of 16 groups and omitted both
diagnostic-derived risks and three next checks. It retained 36 source-evidence
mappings. A reviewer repeated one ID; its receipt preserves the raw references,
while the accepted proposal contains the normalized set. The task contract stayed
unchanged and all 14,210 spent model units remain charged.

This is an improvement in structure, source selection, publication and readable
flow. It is still a high-level Low-model report, not an expert function walkthrough.
The published Markdown has 332 words including headings and links; the retained
overview, component descriptions and flow contain approximately 100 prose words,
plus about 45 words of strengths. No function-call syntax appears in that prose.
The reviewers use the same weights as the writer: separate invocations do not
establish independent capability or semantic truth. Reviewer omission of a next
check also does not prove that the suggested check would be unhelpful.

### Configured Auto High follow-up

The existing opt-in High service was already loaded and idle. No server startup
or configuration change was performed. Fresh Auto selection chose the configured
`strata:high` with the new source-bound routing profile, passed actual resource and
slot checks, and used the exact 32,768-token provider window. Its physical-token
profile admitted 23,712 source bytes from all 12 files; the conservative Low
profile admitted the smaller eight-file selection above. Model capability and
evidence capacity are therefore confounded in a Low-versus-High comparison.

The two High runs have exactly identical writer packet hashes, excerpt hashes
and structured drafts. The first failed because a Low reviewer twice selected a
contradiction without an evidence reference. The new channel-based representation
then completed that same draft with three accepted calls and no repair, timeout
or resource failure. The writer took 98.88 seconds; the Low reviewer pages took
75.05 and 39.08 seconds. All 38,146 spent units remain charged, the contract is
unchanged and the invocation chain verifies.

The final report has six components, five flow steps, three strengths, three
risks, five next checks, both diagrams and 78 evidence mappings. Its prose is
502 words (839 including headings and links). It names concrete functions and
methods, their inputs and intermediate results. This is materially deeper than
the Low result. These are single trials with shared runtime/cache conditions,
not statistical latency benchmarks or proof of correctness.

Semantic limitations remain material: the reviewer retained all 23 claim groups,
including a hypothesis connecting unresolved static calls to possible integration
or runtime problems. Static resolution gaps alone do not establish those defects.
The input-type risk is also hypothetical, while the hardcoded fallback risk is
tied to supplied implementation. Reviewer reasons often restate the claims.
High-versus-Low weight independence is **unknown**, because the High server did
not provide a content-addressed weight identity; model labels do not settle it.
Do not interpret publication or the 23/23 model judgment as verified truth.

## Validation

- 127 focused intent, routing, publication, context, model-switch and recovery
  tests passed before the live reference-normalization discovery.
- 100 native-provider checks passed, including genuine overlap, transient and
  persistent host contention, cancellation, failing diagnostics and recovery of
  the same retained ticket across model switches after a footer-stream failure.
- Seven reference-set tests cover raw validation, strict claim scope, idempotence,
  unchanged verdicts/reasons, legacy rejection, charged gateway execution, reload
  verification and tampered normalization-proof detection.
- Eight intent tests cover the exact failed wording, historical schema/wire
  compatibility, source-bound publication, follow-up permission boundaries and
  retained contracts/spending across model changes.
- Thirteen evidence-grounded judgment tests cover both channels, uncertainty,
  ambiguous/unknown choices, candidate binding, accepted and quarantined gateway
  responses, raw multiplicity proofs, reload verification and legacy wire hashes.

The complete 33-gate run passed 31 gates. Its Python run exercised **1,796 tests**
in 544.602 seconds (8 skipped), with six fixture failures; the authorization
integration fixture also failed its older context-size assumption. After updating
only test fixtures, all affected modules and integration journeys passed:

- Allocation and execution sessions: **20 tests passed**. Assertions now drive
  through the separately charged review stage and distinguish writer memory from
  the independent reviewer's evidence-only packet.
- Task observations and claim pages: **23 tests passed**. Commitments stay with
  writer/children; reviewers do not inherit that commentary as evidence. Wire
  assertions check the new channels rather than an older prompt phrase.
- Saved discovery exposure recovery: **1 test passed**, retaining the exact
  400,167-unit paid prefix, 500,000-unit contract, source receipts and grants across
  reload and model switches. Future writer/reviewer calls now use the real test
  transport interface and settle their reported 400/80 counts as 480 measured
  units; older unmeasured charges were not refunded or rewritten.
- Authorization integration: **all seven journeys passed**. Measured probes prove
  the small context fails before reading source, the middle context fits the
  mandatory packet but fails on evidence, and the larger one resumes unchanged
  receipts. The middle test capacity is 7,500 units, above its measured mandatory
  floor; no production capacity or permission was changed.

All 33 gate areas therefore have passing coverage, assembled from the full run
and these targeted fixture reruns; this is **not** a single clean 33-gate run.
Production files did not change after the full run. The tested VSIX still matches
all 297 runtime files byte-for-byte. The full run also passed:

- Native provider: **100 checks**.
- Persistent coordinator: **16 scenarios**; client: **8 checks**.
- Browser webviews, runtime integrity, metadata and packaged contents.
- Isolated packaged VS Code host: **23 checks**, including real source operations,
  actual AppleSMC telemetry, task/allocation preservation and permission revocation.

Artifact: `dist/Sutra-0.54.2.vsix`, 5,168,906 bytes, 297 runtime files.
VSIX SHA-256:
`f3d8ab8ca1cb58fcdb9b4de75e2da23a6bf941a69281b3176b37e91c1d757989`.

Earlier full-suite attempts were deliberately stopped before production-policy
changes when live models exposed the duplicate-reference and verdict/evidence
blockers. The final release run uses the frozen corrected implementation.

Canonical kernel SHA-256:
`9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`.

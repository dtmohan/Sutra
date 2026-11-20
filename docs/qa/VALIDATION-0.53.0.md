# Sutra 0.53.0 validation — discovery convergence

Date: 2026-11-20. Baseline: `bc74250ef8079d20183c5a80df13ae3f6bf866b8`
(0.52.0). Canonical STP SHA-256:
`9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`.

## Scope and initial policy

The host observes existing call boundaries. Green preserves the model-facing
wire; yellow supplies a compact factual acquisition delta. Two consecutive
non-advancing repository operations enter yellow, four request partial
synthesis, and two advancing operations clear a warning. Receipt IDs are not
progress. Positioned source ranges, unlocated search snippets and complete empty
searches have separate accounting. Recall is not new acquisition: the live
governor receives repository receipts; existing recall tracking remains separate.

This does not measure semantic understanding, establish a contradiction or
close the task. The existing Karma adapter translates measurements into STP
frames. A persistent identical acquisition request can end that acquisition
stage; the host retains gaps and attempts a partial writer through fresh gates.
It does not turn partial evidence into a completed review.

Discovery also has a finite, audit-anchored allowance of twice its initial
writer plus declared reviewer financial allowance. The writer probe measures a
projection of the current packet with the configured budget, mandatory-only
memory and a repair probe; the reviewer is a conservative same-budget
proxy. The holdback targets one review page, not all pages or repairs, and is not
a durable reservation or guarantee. Every later action still needs fresh admission. These
thresholds and the multiplier are initial heuristics, not universal optima.

The first allowance is preserved across reloads, model changes and later task
grants. Actual settled spending includes quarantined calls. Financial selector
shortage yields to the writer without silently funding more discovery. An
unavailable downstream measurement requests a fresh writer fit rather than
allowing unbounded acquisition or claiming a physical context failure.

## Saved-review replay

The original retained task, `review-25447a34c017`, was inspected read-only. Its
807,425 consumed units and original allocation were not changed. The replay
checks scoped invocation/delivery/repository receipt hashes in one SQLite read
snapshot; it is not a complete external-anchor audit.

| Observation | Result |
| --- | ---: |
| Discovery model attempts | 22: 19 accepted High, 3 quarantined Mid |
| Repository effects | 19 |
| Effects without new source/snippet observations | 4 |
| Discovery spending | 399,745 units |
| Returned positioned read bytes | 79,572 |
| Unique positioned read bytes | 64,547 |
| Reread positioned bytes | 15,025 (18.88%) |
| Unlocated search snippets | 27; 1,572 returned bytes |

Returned source is not proven model-delivered source. Unlocated search snippets
are excluded from the positioned-byte union because overlap is unknown.

Default 2/4 progress thresholds warned at accepted operation 15 and recovered at
17; they never requested synthesis. Alternative 2/3 and 3/6 thresholds also did
not stop this review. Most acquisition was new: novelty alone does not solve
this failure. That result motivated the separate finite phase allowance.

A retrospective writer envelope was 32,046 units, with actual use 7,513 units
and 208.62 seconds. No claim reviewer ran on that saved failed task. A distinct
historical Low reviewer supplies only a labelled proxy: 21,410 reserved units,
2,252 used and 15.08 seconds. Their 53,456-unit pair and twice that pair would
cross historical cumulative selector spending after attempts 5 and 7. These
are sensitivity calculations, not observed live savings or quality gains.

## Real-provider experiment design

Private temporary ledgers preserve the user's existing task and settings. Both
runtimes see an identical, exact-byte product projection of the baseline commit:
161 selected text files including README.md, 2,276,766 bytes (2.28 decimal MB), excluding
vendored implementation, tests and binaries. This supports a product-runtime
review, not a claim of complete repository coverage. The source scope digest is
`e831e055a33316ff0265ed8a22966bd850d9be2d44aca9c179b6943aed840998`.

Prompt:

> Review the Sutra workspace and explain its architecture in detail for a new
> intern. Trace a user request through model and tool dispatch, STP enforcement,
> memory and recovery, child checkpoints, and claim publication. Include a
> high-level overview, strengths and shortcomings, source citations and a
> simplified flow diagram.

Models: Low `qwen3:4b`, Mid `local-assistant:latest`, High `strata:high`, serving
`qwen3.8-flash-next-q2_0`. Fixed High exercises architecture stages; ordinary
distinct review routing remains enabled. Adaptive reasoning is unchanged.
Limits: 12 calls, 250,000 model units, 256 repository operations, 64 MiB source,
1,800 execution seconds. No fake provider, resource bypass, automatic grant,
forced report or identity bypass. Real telemetry is refreshed by the runner.
The run enters the architecture stage directly; it does not test chat intent
classification or VS Code user interaction.

### Baseline child run

The real baseline paused before discovery: three child responses failed JSON
parsing at line 1, column 1. They ended with `stop`, not an output-limit stop.

| Child attempt | Input tokens | Output tokens | Model seconds |
| --- | ---: | ---: | ---: |
| 1 | 8,672 | 361 | 29.88 |
| 2 | 8,712 | 994 | 52.39 |
| 3 | 8,712 | 994 | 52.45 |

28,445 units remain charged, with no pending work. Attempts 2 and 3 had identical
payload, model-packet, wire, schema and settings hashes. Per-call request hashes
differ because they include unique call IDs. The bridge step took 169.63
seconds, of which model calls took 134.72; the residual includes preparation,
validation and teardown, and is not attributed to the kernel.

The retained baseline trace did not include response bodies or output-shape
metadata. Positive content-stream timing proves content existed; it does not
show whether the prefix was prose, markup or another invalid form. An apparent
summary-instruction contradiction was not reproduced in the final built wire;
the existing replacement already removes it. No prompt-version change was made.

The candidate adds syntax-specific repair feedback and a BRIDGE checkpoint
before a proven identical third repair. The original remaining attempt is
retained. Changing only financial allocation does not make the generation new;
a material wire, model identity, reasoning or physical/output-control change
can resume it. Parser acceptance, evidence checks and the kernel are unchanged.
New output-shape diagnostics record bounded categories and numeric positions,
with no generated prefix or response body in compact traces.

### Supplemental parent path

A separately declared 1,200-second contract uses normal branch-affordability
admission to select direct parent discovery. All other models, source, prompt
and limits remain the same. This isolates convergence without forcing a state
transition; it does not establish successful child-workflow execution.

The baseline reached the declared external observation boundary of eight
discovery calls with its workflow still in SYNTHESIS. All eight selector/tool
steps were accepted: 114,455 units and 413.87 seconds of summed model time.
No writer, reviewer or report ran before that boundary. There were no pending
model calls; the SYNTHESIS state and its receipts were retained. No automatic
grant, replacement or expanded baseline was introduced.

The baseline returned 31,013 unique positioned bytes from four files, with zero
positioned rereads, plus 765 bytes of unlocated search snippets. All eight
requests were distinct and the novelty governor would remain green. Useful new
acquisition can continue without producing a report: novelty and convergence
are different measurements. This external experiment boundary is not a claim
that the baseline runtime would have stopped itself at eight calls.

Both trials start a fresh Python interpreter for each bridge action. They do
not measure the persistent-worker RAM-cache user experience. The product scope,
prompt, actual provider map and 1,200-second contract are identical.

### Measured headroom calibration

The first candidate (frozen candidate-2 runtime) handed off after three accepted
selectors and 44,148 consumed units. The recorded reason was
`governor_downstream_holdback`, with a canonical BRIDGE. Novelty remained green
and the 327,428-unit discovery ceiling was not exhausted.

| Handoff observation | Budget units |
| --- | ---: |
| Projected writer | 39,739 (35,643 input + 4,096 output) |
| Representative reviewer probe | 8,433 (4,337 input + 4,096 output) |
| Declared reviewer financial proxy | 102,926 |
| Combined v1 holdback | 142,665 |
| Projected next selector | 67,416 |
| Task remaining | 205,852 |

The selector plus holdback exceeded remaining capacity by 4,229 units. The
reviewer proxy was over twelve times its representative probe. Conversely, the
actual next writer reserved 83,698 units (75,506 input + 8,192 output), larger
than the planning projection. Neither projection is an established bound for
an unknown final packet; the old `cost_floor` name describes measured planning
packets rather than a lower bound on future execution.

Version 2 estimates reviewer headroom as
`min(declared financial allowance, 2 * representative probe)`. At the recorded
handoff, that is 16,866 units and a combined 56,605-unit estimate. It adds no
provider, identity or inference calls. Actual writing/review still passes fresh
admission; this heuristic cannot promise publication or that every page fits.

The stage-ceiling basis remains writer plus DECLARED reviewer allowance, so
tuning headroom does not change the initial 327,428-unit ceiling in the matched
trial. Existing allowance anchors are reused, and captured v1 plans retain
their original validation and gate behavior. New allowances link to a bounded,
source-free full initial plan audit, preserving the measurements behind the
digest rather than only its aggregate number. The multiplier of two is initial
empirical headroom, not a universal model-capability result.

### Observe-only metadata placement failure

The candidate-2 writer used its normal three attempts. First it reached the
actual provider output limit (172.03 seconds, 21,127 consumed units). The
financial output reserve was 8,192 units, while the transport requested 3,072
total generation tokens with a 768-token reasoning budget under Adaptive.
Capacity reservations and actual generation controls are distinct.

The next two replies completed JSON but failed `response.report: unexpected
fields` (70.72 and 81.33 seconds; 18,228 and 17,746 units). Both contained every
required report field. The sole extra report key was a string-valued
`task_observation`; no top-level observation was present. In-memory copies pass
normal model-packet binding and shape validation when only that nested optional
diagnostic is removed. This does not establish semantic correctness, citation
support or publication admission.

The immutable run ended with its response withheld: 101,249 units across six
calls, with no pending work and no reviewer or published report. Its receipts
were not rewritten, refunded or retroactively accepted.

The corrected path extracts only `report.task_observation` for the architecture
writer when a top-level observation is absent. It retains the raw diagnostic
and placement for response-hash reconstruction, records a malformed observation
as non-authoritative, and validates the remaining answer normally. Other unknown
report fields, ambiguous duplicate placement, invalid citations, permissions
and claim-publication checks remain enforced. This restores the existing
observe-only invariant; it does not salvage arbitrary malformed JSON or accept
unsupported claims. The final tuned trial uses a fresh ledger rather than
replaying changed rules over the old run.

### Final matched trial

The final frozen candidate-4 runtime uses version 2 headroom, an unchanged stage
basis, the linked initial plan audit and the optional-observation placement fix.
It ran only after release validation; no further fresh trial was started.

Its initial audit records writer 38,714 units, representative reviewer 8,418,
declared reviewer financial ceiling 125,000 and estimated reviewer headroom
16,836. The combined estimate is 55,550 units, while the initial discovery
ceiling remains 327,428. These are planning values, not paid spending or a
future-publication guarantee.

| Observed outcome | Baseline | Final candidate |
| --- | ---: | ---: |
| Accepted discovery calls | 8 | 8 |
| Consumed model units | 114,455 | 105,329 |
| Summed model seconds | 413.87 | 352.70 |
| Positioned returned source bytes | 31,013 | 18,401 |
| Positioned reread bytes | 0 | 0 |
| Unlocated search-snippet bytes | 765 | 1,358 |
| Writer/reviewer/published report | None | None |

Both trials reached the external eight-discovery observation boundary with
runtime SYNTHESIS retained and zero pending model calls. Every final-candidate
operation added some positioned source or search-snippet evidence, so the
observer stayed green throughout. The runtime did not force a synthesis
handoff before the external boundary. Neither run establishes improved
convergence, answer quality, STP/memory/checkpoint/publication explanation or the
requested flow diagram. No output exists on which to score those criteria.

Trajectories, source returns and transient identifiers differ. These single,
incomplete trials are descriptive observations, not a causal latency, token
savings or quality comparison. The headroom change demonstrably permits more
acquisition than candidate-2's three-call handoff; it does not establish when
the model has understood enough. Semantic convergence remains unverified in
this live case. The optional-observation fix is verified by the gateway and
publication regressions, not by a final live report.

The original retained review was checked again read-only after all experiments:
62 calls, 807,425 consumed units, identical model and repository receipt heads.
That includes the 65,668-unit original reservation retained by a quarantined
call without a settlement. An intermediate one-off evidence summary omitted
that fallback; its corrected, superseding artifact uses the runtime accounting
rule. No original task receipt or spending was rewritten.

## Recovery and authority regressions

- Actual gateway tests cover changing receipt IDs for the same read, sparse
  yellow feedback, partial synthesis, immutable allowance anchors and rejected
  attempts staying charged.
- Reload/model-switch tests retain pinned statements, task binding, permission
  constraints, receipt prefixes and consumed spending. Later grants do not
  replenish discovery's phase allowance.
- A deterministic saved-task fixture with 245,904/250,000 model units consumed
  and a 500-token counting-service response verifies financial selector shortage
  can yield to a writer without a new selector dispatch, hidden grant or dropped
  pins. This checks the control path, not actual tokenizer accuracy or hardware
  fit. Genuine physical overflow remains a context fault.
- An unavailable 4,096-unit reviewer probe yields a recorded BRIDGE and fresh
  writer admission, with no discovery allowance, grant or selector call.
- Forged feedback, changed profile, foreign task bindings, removed/downgraded
  control and rehashed allowance substitutions fail before a model/tool effect.
- Historical fifteen-call recall/eviction, twenty-read DAG batching and fixed
  pager/automatic-funding fixtures explicitly use their original pre-governor
  profile; all original source, transaction and spending assertions remain.
  New governance tests exercise the default profile.
- A default-governor conservative-counting fixture preserves an already-earned
  6,762-byte page and a 225,000-unit paid prefix. Physical ALLOW plus financial
  refusal yields partial synthesis without funding, rereading, amendments,
  changed pins, receipt changes or spending changes.

## Reproduction

Run the offline and normal packaging gates from the repository root:

```sh
KARMA_NODE=/absolute/path/to/node scripts/qa.sh --with-package
python3 -B scripts/evaluate-discovery-governor.py --help
python3 -B scripts/experiment-discovery-convergence.py --help
```

The replay command requires a local retained ledger/session; reports omit model
notes and source bodies. `--steps-output` additionally contains local tool paths
and queries and is optional. Live `prepare` freezes hashes without inference;
`step` executes one normal bridge action and preserves each pause. Tests cover
the runner itself. Neither script installs an extension or changes user tiers.

The component timings and release gates below describe the implementation;
they do not replace the incomplete live-quality result above.

## Component overhead

The reproducible `--benchmark` mode uses fixed advancing, repeated and mixed
8/32/128-receipt workloads. Each component has 10 warmups and 100 measured
iterations; the run occurred between live inference actions.

| Receipt count | Approximate assessment median |
| --- | ---: |
| 8 | 0.12 ms |
| 32 | 0.45 ms |
| 128 | 1.76 ms |

The adapter plus unchanged canonical kernel took approximately 0.004–0.005 ms
median, with p95 at most 0.011 ms. Worst combined p95 at 128 receipts was
approximately 1.85 ms. These are component timings, excluding SQLite, receipt
verification, prompt packing and inference. They do not establish end-to-end
request latency or constant cost for unrelated storage paths.

## Release validation history

The first complete QA run passed 29 of 30 gates, including browser checks,
packaging, package-content verification and the isolated packaged VS Code host.
Its Python gate ran 1,670 tests with eight skips and found ten fixture failures:
the deliberately long ancestor chain and old pager/funding recipes were using
the new convergence default. Those fixtures now explicitly select the original
profile. Their assertions were retained, and the additional conservative
saved-page default-governor regression was added. Production code and the
already-verified VSIX payload were unchanged by these fixture corrections.

The corrected complete Python gate passed **1,671 tests in 417.910 seconds,
with eight skips**. Together with the 29 unchanged release gates, all 30 required
gates passed. The new replay evaluator ran 18 tests; the live experiment runner
ran 12. The isolated VS Code gate passed 21 packaged-host checks, including real
AppleSMC temperatures, governed source mapping, saved authorization and
revocation with task/spending retention.

The isolated VS Code gate exercises packaged extension APIs and webviews; the
real-model trials use the bridge API, not a manual chat acceptance test.

Packaged artifact: `dist/Sutra-0.53.0.vsix`, 5,124,007 bytes, 294 runtime files.
SHA-256: `7ad8a6f5627a76870944406f5792fb1979abe375438fca1c6a7aed982a8f70b6`.
Package-content verification matches the reviewed source, and canonical STP
remains at the digest above. Existing user model tiers were preserved. No
installation or Git push was performed by this validation task.

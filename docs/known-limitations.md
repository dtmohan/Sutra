# Known limitations

- MLX calibration measures sampled process-tree physical footprint during cold
  synthetic text prefill, not a proven worst-case GPU allocation. Larger output,
  multimodal work, unseen shapes and external clients can change memory use.
  Estimates use the next larger measured workload plus a 25%/1 GiB margin.
  Exact, unexpired resident identity and sufficient context permit a credit
  capped by model-file and reported resident weight size (also VRAM for MLX).
  This estimates weight reuse, not newly available RAM; the full calibrated
  margin and workspace envelope remain. Unmatched inventory uses cold estimates.
  Context beyond measured coverage requires paging or a
  new calibration; the runtime never extrapolates from smaller observations.
- Profiles are bound to exact model/metadata, backend binary/libraries, hardware,
  OS build and server epoch. Restarting Ollama invalidates them because server
  settings may change. Missing or invalid profiles revert visibly to file-size
  and known conventional GGUF KV estimates; MLX backend overhead is then unknown.
  Other same-model clients cannot be distinguished by the running-model API:
  calibration requires other clients to be idle. This is not a RAM guarantee.
- The inference slot coordinates Sutra windows in the same extension profile.
  Other Ollama clients and separate profiles are not coordinated. Shared mode
  never sends a separate unload request. Exclusive ownership is a user assertion
  about the server, not something the running-model API can verify.
- Auto capacity fallback applies to workspace children, synthesis and coding,
  after a typed refusal before inference. It can try only strictly smaller
  configured tiers, at most twice per saved workflow. It does not override
  explicit tier choices, OS pressure, stale identity or interrupted inference.
  A smaller worker may still lack context capacity or fail quality checks.
  Independent report reviewers still require their own resource admission;
  fallback does not relax reviewer identity or turn withheld work into success.
- Resource recovery releases at most one confirmed idle owned model and observes
  stability for up to three seconds. It does not automatically retry inference.
  Interrupted model activity stays uncertain until observed absent; no caller is
  assumed idle just because a local lease was released after process death.
- Model explanations can contain unsupported factual claims. Structural diagram
  validation does not establish the truth of device specifications or connections.
- A generated diagram can merge distinct devices unless identity constraints are
  preserved and checked. Future work: verify user-stated node identities and edges
  independently before publication.
- Conversation-role calls currently have no web tools. Permission in a prompt
  alone cannot supply a missing search capability. Future work: explicit governed
  web access with source receipts and a visible network state.
- Native chat compaction relies on signed pointers and, when supplied by VS Code,
  a per-chat binding hint. Missing authenticated context must be reported rather
  than filled with another chat's most recent context.
- Conversation working memory is sized from the selected model/context allocation, with recent turns and selected Smriti pages. The complete transcript stays in the ledger; selected context is not a semantic completeness guarantee.
  A valid checkpoint authenticates this memory; it does not prove it retained all
  facts. Workspace contracts are stored separately and checked on each invocation.
- VS Code host token sizing uses cl100k_base plus 15% headroom as a local-model
  approximation. It is not the model's exact tokenizer or the harness budget.
  Large instructions, tools or history can still legitimately trigger compaction.
- A zero test-process exit code is an observation, not independent proof that
  assertions are correct or even ran. Source-controlled test output is untrusted.
- Editing/test execution is limited to supported scoped Python operations.
  The Windows adapter is currently read-only.
- Test snapshots contain only the task's explicitly granted files: at most eight
  files, each at most 64,000 bytes. Required project modules must be included in
  that grant, otherwise imports fail. A broader read scope allows inspection and
  dependency recognition; it does not add files to an executable test snapshot.
  Test confirmation lists the snapshot contents. This is not yet a whole-project
  test runner.
- Audit verification still scans the full chain on reopening a ledger or after
  database changes. Repeated checks on the same unchanged connection reuse the
  verified result. There is no persisted trusted-prefix checkpoint.
- Runtime hashes detect changes against the bundled manifest, including application
  code and the canonical kernel. They are not a signature or protection against an
  actor who can replace the extension entry point and manifest together. VS Code's
  mutable package metadata and the manifest itself are excluded from hashing.
- Temperature sensor mapping is verified for the original Apple Silicon test
  machine. Celsius values are advisory; resource admission uses OS pressure and
  memory measurements.
- A resource pause needs explicit Resume after resources recover; Sutra does not
  automatically retry it or refund consumed task allocation. If pressure blocks
  the independent reviewer after a draft was generated, that report is withheld;
  the unreviewed draft is not a resumable or publishable checkpoint.
- Private repository access and extension installation do not require or imply
  publication to the VS Code Marketplace.

- Architecture claim reviews are fallible model opinions. Exact-quote checking
  establishes source provenance, not entailment. The host omits disputed claim
  groups but cannot guarantee that every unsupported statement is detected.
  The extra reviewer consumes task allocation and can withhold publication when
  a valid review does not fit the remaining context, call or time budget.
- Weight identity uses verified manifest bytes and content-addressed model,
  named-tensor and adapter references in the local Ollama blob store. It does
  not re-hash all weight files or attest the server. Template/parameter changes
  and registry provenance do not establish different weights. A nonstandard
  server model store may leave weight separation unknown; strict separation
  refuses dispatch in that case. Family is an architecture heuristic, not a
  verified training lineage. Different weights do not imply independent errors.
- Test provenance covers declared test paths and observed runtime/build identity.
  It cannot prove isolation from every environmental difference or the quality
  of the tests. A red-to-green observation is not a verified correctness claim.
- The optional sensor is experimental and off by default. Token likelihood is
  not a correctness/confidence metric. Top-k conditional entropy is truncated;
  a sampled token below an alternative does not prove a grammar forced it.
  Unknown or unaligned probabilities remain unavailable/unattributed. Bootstrap
  thresholds are uncalibrated, never kernel inputs. Backend request errors still
  obey normal transport and repair rules. Only numeric mode omits token text
  from this diagnostic log; existing call traces have their own retention policy.

- A long-lived workspace task shares its conservative allocation across requests, model switches and retries. Model context capacity and remaining task budget are different limits. A saved context pause does not replenish either limit; optional repair shares yield first, but a report can still require a larger explicitly authorized task allocation.

## Small-model routing

- Escalation is a model proposal, not a verified measurement of reasoning quality. A small model may fail to request help or request it unnecessarily. Contract, budget, context and resource checks remain deterministic; they do not prove semantic correctness.
- Auto starts the conversation/routing stage small. Workspace workers still use a source-informed breadth heuristic after fresh scoped retrieval; this release does not add a small-first retry cascade to every worker or reviewer role.
- Repeated configured model names are skipped during escalation. Different aliases with the same weights are not deduplicated here; reviewer identity checks remain separate.
- Requested pages and pinned context must fit the chosen model. Otherwise the request pauses for a larger context/model choice; no mandatory context is discarded. Missing facts require user clarification.
- Routing drafts consume allocation and remain receipt dependencies, but are not published or saved as ordinary conversation turns. Resume never refunds completed calls.

## Adaptive reasoning

- Adaptive currently applies to conversation and intent routing. Workspace workers and independent reviewers use their model-default reasoning; source-informed worker tier selection is unchanged.
- The escalation signal is a fallible model assessment. More deliberation is not a correctness guarantee, and a request can stop at an incorrect direct answer without asking for help.
- Ollama's `think=false` request may fall back to a model's lowest available level. If thinking-on is advertised, the generic false request can be followed by true, but backend fallback may make them computationally equivalent. Metadata does not prove the backend's actual computation. Unknown level names are not ordered automatically.
- Each supported retry consumes the existing call, time and output allocation. Increasing `num_predict` alone is not used as a reasoning control. A long thought stream can still exhaust the combined reasoning/answer output reserve and be withheld.
- Existing explicit thinking preferences remain in effect. Select Adaptive to opt in when upgrading an installation with an existing preference. Model capabilities or preference changes during a saved retry cannot silently reset its progression.

- In the 0.25.0 live test, `local-assistant:latest` at `low` effort omitted three requested constraints from its visible answer even though every constraint remained in pinned input; it reported no need for escalation. The switch back to `qwen3:4b` recalled them. Host context preservation and permission enforcement do not prove answer completeness.

## Prompt and decoder boundary (0.26.0)

- The structured-decoder workaround is deliberately limited to Ollama 0.40.16 MLX. Other runtimes need their own validation; host shape and length checks remain strict everywhere.
- Adaptive initial effort uses current-request keywords and structure, not a semantic classifier. Some easy prompts can receive extra effort and some difficult prompts can stay at the lowest effort. More reasoning can cost more without improving the answer.
- Blank answers and incomplete code fences are detectable, but an apparently complete and well-formed wrong answer can still be admitted. The kernel governs allocation and permissions; it does not certify semantic correctness.
- Paging controls must be explicit. Bounded repair can fix malformed requests, but cannot guarantee that a model selects every relevant page or recognizes missing evidence.

## 0.26.1 live routing validation

The reported capability-to-architecture sequence now reaches the governed source workflow. This does not guarantee that a subsequent architecture report finishes. In the isolated one-file test, the MLX worker exhausted a roughly 2K-token output share; a MoE replacement produced a draft after one formatting repair, but its smaller independent reviewer also exhausted its combined reasoning/output share. Both publication failures remained withheld. Context, read-only permissions and consumed budget survived the model switch. Adaptive still applies to the conversation router; workspace workers/reviewers retain model-default reasoning. Their output allocation and reasoning policy need separate tuning.

# Known limitations

- MLX calibration measures sampled process-tree physical footprint during cold
  synthetic text prefill, not a proven worst-case GPU allocation. Larger output,
  multimodal work, unseen shapes and external clients can change memory use.
  Estimates use the next larger measured workload plus a 25%/1 GiB margin and
  no residency credit. Context beyond measured coverage requires paging or a
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

Sutra 0.29.0 — warm-model memory estimates and governed fallback

Loaded models receive a conservative weight-reuse credit only when name, manifest, backend, unexpired residency and context capacity match. The full working-memory envelope, calibration uncertainty margin and 4 GiB reserve remain. Sutra Monitor shows the cold estimate, resident weight credit and remaining memory reservation. Fresh inventory and installed identity are checked again before dispatch.

Auto workspace workflows may select a strictly smaller configured tier after a capacity refusal before inference. The central state machine records at most two switches under STP and remaining-budget checks. The replacement receives fresh source and paged context with the original request and constraints. Completed children, permissions and spent budgets survive. Explicit model selections are preserved; OS pressure, interrupted inference and identity failures still pause. Independent report review requirements are unchanged. Conversation routing continues to start with the low tier.

The canonical STP kernel is unchanged. Compatible conversation memory from 0.28.x is retained; old policy-bound workspace tasks require the existing explicit restart flow, which preserves consumed allocation.

CALLS AND REASONING
Native thinking blocks open only when a model returns separate reasoning text. Calls without that stream show call details, including the requested reasoning control. Source mapping and resource pauses are labelled as host status. A paused model allocation is not a generated model answer. Parent and child streams remain independently inspectable in Sutra Monitor. Diagnostic text is bounded and unverified; the STP kernel and all admission rules are unchanged.

CONTEXT MEMORY
Open Sutra Monitor to see confirmed working sets, page-ins, evictions, refaults and recovery. Exact originals remain in durable storage when optional pages leave a prompt. Common memory faults pause the workflow with its context, permissions and consumed allocation retained. Resume rechecks the saved stage; it never refunds budget or replays completed children. Missing conversation pages can be rebuilt from verified receipts. Corrupt or stale evidence is withheld for trusted recovery. Diagnostic history is bounded and never supplies authority. The STP kernel is unchanged.

TASK COMMITMENTS — OBSERVE ONLY
Workspace routing can record the model's interpretation and completion criteria before source work. Workers inherit the saved commitment and can report what was delivered and what remains. Native chat shows a concise summary; Sutra Monitor shows the last 12 observations. Model claims are unverified and cannot grant permissions, authorize completion, trigger escalation or refund budgets. Missing/malformed observations never request a repair. Existing calls carry the observations; generated tokens still consume allocation. Greetings and direct calls without a routing commitment do not manufacture precommitments.

Architecture and onboarding requests about the current project request a same-chat source review. Capability-only questions stay in conversation. The model receives a compact response guide; the decoder and host retain the full strict schema, permissions, context binding and budgets.

Sutra Monitor now shows the effective reasoning setting and links to its VS Code setting. Existing choices remain unchanged by installation. Choose Adaptive to use the request-based reasoning policy. Protocol checks passing does not verify task completion or answer correctness.

Conversation prompts now focus on the current ask, relevant memory and the required answer format. The host binds context identity and retains the full contract, permissions and consumed budget in the ledger. Missing-context replies need actionable top-level page requests or a specific question. For the tested Ollama 0.40.16 MLX decoder only, a transport schema omits string length bounds that truncated escaped text; the original strict limits remain enforced by the host.

Auto starts new requests with the configured low-tier local model. Greetings and routine questions can finish in that first call without arming workspace access or running review tools.

Choose Model reasoning → Adaptive in Sutra → Settings → Reasoning & runtime, then Save runtime settings. Adaptive is the default for new configurations. Existing Off, On and Model default choices are preserved; this upgrade does not silently override them. The native VS Code setting is karma.thinking.

Adaptive keeps routine chat at the lowest supported effort. A small deterministic heuristic selects the first supported positive effort for explicit reasoning requests, numeric planning/corrections, code analysis and fenced structured results. It does not prove task difficulty. If a model requests missing Smriti context, Sutra pages it in first at the same effort. If the model reports that the evidence is sufficient but it needs stronger reasoning, the host checks the next supported effort on that same model. Only after that model's supported choices are exhausted may Auto select the next configured, untried tier. Explicit Low/Mid/High selections can increase reasoning but never silently switch models. A workspace request hands off immediately for fresh scoped reads; workspace workers and reviewers retain their model-default reasoning in Adaptive mode. This is not a retry cascade for every tool role.

Controls come from Ollama /api/show, not model names. Named levels must be advertised. Ollama documents think=false as a generic lowest-effort request; models that cannot disable thinking may still reason. If true is advertised, Sutra can explicitly enable thinking on a retry, even when false is absent from the metadata. The backend may map both requests to the same effective effort; this is not a guarantee of more computation. Unfamiliar or missing metadata uses the model default without inventing levels. Increasing reasoning can cost more time and tokens without improving correctness; a model's request for help remains unverified.

Reasoning choices and supported-control metadata are bound to receipts and rechecked before dispatch. Each retry uses the invocation gateway, existing task allocation and unchanged STP kernel. Reasoning and final text share the existing output reserve; no extra token or time allocation is created. Resource waits, exhausted budgets and mandatory context that does not fit produce a saved pause. Resume preserves spent budget and the pending effort level. Changing the saved request's reasoning setting or its model capabilities requires restoring the setting or starting a separate request, rather than replaying from the cheapest level.

Recent turns, pinned user statements and requested pages stay intact across model switches. Optional recent turns respect their allocated share so they cannot take all space for retrieval and the table of contents. The host never drops mandatory context to force a model to fit. Missing facts that cannot be paged require clarification, not a larger-model guess.

Chat distinguishes loading context, increasing reasoning on the same model, and escalating models. All routing stages and repairs contribute to token totals. Only the final admitted answer becomes conversation memory; quarantining an intermediate routing receipt invalidates dependent answers. Source freshness and workspace/edit/test permission checks remain independent of model and reasoning selection.

Conversation beside saved workspace work

New messages are classified through the invocation gateway before any saved-review recovery. A greeting stays conversational, with no workspace arming, source mapping or review restart. A saved review keeps its context, constraints and consumed allocation. Send continue/resume explicitly to resume it; new workspace work asks before replacing it. Policy-upgrade restarts remain an explicit choice.

Sutra Monitor separates System pressure from Last model allocation. Healthy system pressure does not prove that the selected model fits. The last allocation is labelled historical and is checked again on dispatch. All existing memory thresholds and the STP kernel remain unchanged.

For light conversation, choose a small installed chat model for the low tier. Existing user model choices are preserved; configuring all tiers to the same large model still uses that model for greetings.

Prior memory calibration features:

MLX memory profiles now feed measured process-footprint estimates into Karma's resource adapter. Each profile is bound to the exact model manifest and metadata, Ollama executable/version, MLX libraries, macOS build, hardware and server epoch. Profiles are checked before optional Smriti paging and again before dispatch. A smaller context can lower the estimated memory requirement while the kernel and 4 GiB system reserve stay unchanged.

The calibration uses repeated, cold, synthetic text workloads through the normal invocation gateway. It samples macOS process-tree physical footprint, records actual prompt/output tokens and requested context separately, and rejects interrupted, pressure-affected or poorly sampled measurements. The estimate is the monotonic upper envelope at the next larger measured workload, with 25% or 1 GiB margin (whichever is greater). Context is capped at measured coverage; no extrapolation is applied to calibrated MLX plans. Version 0.29.0 deducts only matched resident weights while retaining the full calibration margin and working-memory envelope. These are empirical estimates, not a guaranteed worst-case RAM bound. OS memory/thermal gates remain authoritative.

Sutra Monitor → Runtime memory shows the selected profile fingerprint, measured workload ceiling and margin. Profiles from another model, backend build, hardware or server restart are not reused; missing/mismatched profiles visibly fall back to the previous uncalibrated estimate. The bundled profiles only apply to the measured machine and server epoch. To recalibrate after restarting Ollama, use scripts/calibrate-mlx-memory.py in the source archive with --output pointing to a new artifact directory, then copy its profile-*.json files to the extension's globalStorage runtime-memory/memory-profiles directory. Run only while other local model clients are idle. No measurement uses workspace source, and no model response supplies its own memory costs.

Policy upgrades preserve data-only conversations. Existing workspace reviews use the existing Restart review flow, retaining their task constraints and consumed budget; old executable results are not silently revalidated.

Runtime memory management from 0.22.0:

Sutra now coordinates one local inference slot across its VS Code windows. Each dispatch requests the context needed by its already-admitted packet and output reserve, with a small margin inside the same allocation. Fresh resource checks and a versioned estimate of additional model memory precede invocation registration. Where conventional GGUF attention metadata is known, optional context selection is capped before Smriti paging; MLX/hybrid KV and backend workspace sizes remain explicitly unknown when no matching empirical profile is available. Model-file size and /api/ps residency are planning observations, not a peak-RAM guarantee. The existing 4 GiB system reserve remains in force.

Sutra Monitor → Runtime memory shows compressed memory, swap, resident models, requested context and the last recovery event. Settings → Reasoning & runtime → Local Ollama ownership defaults to Shared. This mode never sends a separate unload request to the shared server. Dedicated to Sutra is an explicit application-level grant for a server used only by Sutra: a completed, identity-matched idle model may be released once during a recovery attempt. Failed or interrupted release is not replayed. All Sutra model calls request keep_alive=0 and observe whether the model remains resident; other clients are outside Sutra's coordination.

Recovery takes at most one release action and a three-second stability observation per attempt, records its result, and leaves the request paused for explicit Resume. Two distinct fresh healthy telemetry samples are required. An interrupted call whose model remains resident cannot be assumed idle. After a model unload, dispatch waits briefly for telemetry sampled after that release so a pre-unload reading cannot cause a false allocation stop. The original prompt, mandatory memory, contract, permissions and consumed budget remain in the ledger; unloading or switching models does not recreate or refund them. Runtime context sizing does not add source caches or replay a full conversation.

Resource admission stops now pause native chat normally instead of causing Autopilot error retries. The saved stop includes the blocking OS pressure/memory measurement or missing/stale telemetry reason. Wait for Resources: ALLOW in Sutra Monitor, choose Resume and send continue in the same chat. Resume checks fresh telemetry before dispatch, retains completed child work, and keeps the original request, constraints and consumed budget across model switches. Conversation routing can resume without arming files. Resource recovery does not replenish task allocation; the STP kernel and resource thresholds are unchanged.

Architecture reports now receive a separate claim-versus-source review before publication. Sutra prefers a smaller installed reviewer with different recorded weights, preserves the task contract, and charges both stages and repairs against the same allocation. Unsupported or contradicted claim groups are omitted; Review details records the verdicts, omitted claims and identities. A malformed or unavailable review withholds publication. Different weights and matching quotations do not prove correctness or statistical independence. API maps use the same identity-aware reviewer selection.

Test receipts compare outcomes only when the complete declared test inventory, runtime/runner identity, limits, policy and task epoch match. Changed tests are labelled separately; missing or incompatible historical provenance is inconclusive. These remain process observations, never proof that assertions ran or that a change is correct.

The experimental token-choice sensor is off by default. Enable numeric diagnostics in Sutra → Settings → Reasoning & runtime, or choose flagged token text explicitly. It measures available token likelihoods, not correctness; it never feeds STP, alters permissions or chooses a model. Local summaries are capped at 1 MiB, with separate cohorts for model identity, stage, schema and decoding settings. Some backends do not return log probabilities. Enabling them can add transport/compute overhead. Numeric mode omits token text from the sensor log; ordinary model traces retain their existing storage policy.

Architecture reviews now have consistently formatted components, request flow, strengths, risks and next checks. Sutra selects relevant declaration bodies and records their line ranges. Workspace calls prioritize fresh source over optional older dialogue, while retaining the latest turn, pinned facts/constraints and explicitly requested memory pages. Optional repair shares and expanded output reserves yield when required context cannot fit. Structured reports retain a separate reviewer share; every actual attempt still requires admission within the remaining shared allocation. Components, findings and flow references must name files whose excerpts were delivered; this checks source provenance, not the truth of a model conclusion. The host opens a compact dependency diagram beside native chat, built from literal import/require references to existing files. These are local import candidates; runtime loading is not verified. Same-file recursion is omitted from that overview and retained in the full static map. Arrows do not imply execution order. Use Review details and source citations for protocol identity, checked quotations, outstanding checks and the full map. Open Last Diagram reopens the saved review snapshot; it is not a fresh read.

Architecture reviewers receive a fresh cross-page file and module inventory, budgeted source excerpts, and available child reports. The evidence-delivery line distinguishes mapped files from sampled implementation coverage. Workspace context uses installed model metadata within remaining allocations; model changes do not reset budgets or permissions.

The conversation stage can request a workspace review in the same chat. Its lack of direct file tools does not disable the harness. Known false file-access refusals are withheld and sent through bounded repair; arming, task scope and remaining budgets still govern execution. Existing conversations keep their saved memory and contracts.


Use Sutra → Settings → Model reasoning to enable reasoning at the lowest supported level or use model defaults. Adaptive is the default for new configurations; existing choices are preserved. Calls & reasoning in Sutra Monitor shows live expandable details for parent and child calls, protocol outcome and final input/output tokens. Compatible VS Code hosts also show thinking inline; older hosts use the Monitor. Reasoning is unverified model commentary and is never saved as task memory or authority. It shares the existing output budget with the answer. Open each call’s reasoning details link in chat, or Sutra Monitor → Calls & reasoning → Open reasoning details. Plain-text diagnostic history survives reloads, bounded to 12 calls, 7 days, 1 MiB per workspace and 16,000 characters per call. It may contain source references. Older vanished streams cannot be recovered because previous versions did not save reasoning.


Workspace access waits for the Arm Sutra dialog, then explicitly reports granted or declined. Already armed requests say they reuse the current grant. Sutra Monitor and routing logs include the permission outcome. File operations cannot proceed while confirmation is pending or declined.

Saved mapping failures are bound to their parser environment, contract and allocation. After a parser/policy upgrade, legacy failures rejoin the fresh-read queue; unchanged failures are not retried indefinitely. Existing receipts, permissions and consumed budget remain intact.

Child reviews read fresh, byte-bounded source pages within their existing allocations. An interrupted review from an older policy offers Restart review in the same chat; the original request, constraints and consumed budget stay intact. Compatible conversation memory is validated and retained on restart. Old worker results are not reused.

Compiled Python stdlib imports and readable project imports are recognized without widening action permissions. Long source lines continue with version-bound cursors. Test confirmation lists the exact snapshot files; required project modules must be explicitly granted.

Runtime checks now cover application code and the unchanged STP kernel before local modules load. Repeated audit checks reuse only an unchanged verified database connection.

Whole-codebase Python mapping uses the same source-page work allocation as TypeScript, instead of the small API-review packet limit. Pauses show the underlying cause and mapped-file counts in chat; file failures also appear in Sutra Monitor.

Resume or Discard a saved request from Sutra Monitor. Resume selects it for your next message in the existing Sutra chat; it does not submit a prompt or overwrite a draft. Pauses report model capacity separately from the task allocation. Context allocation shortages are saved stops, not retriable provider errors. Resume returns to the stopped stage under the same contract and remaining budget. Interrupted actions are reconciled without replay.

Task form drafts and expanded source lists survive refreshes. Start Ollama, then use Retry model discovery in Settings if setup is unavailable. File permissions persist with the task; disarm or reload prevents execution until rearming, and every edit/test still needs action confirmation.


Previously Karma. Your installed extension ID and saved settings are preserved.

GET STARTED
Run Sutra: Configure Local Models to select installed Ollama models.
Run Sutra: Open Governed Chat (Sutra Auto). This saves the compatible Local chat
and model for new chats. Existing chats retain their own selected model.
Chat normally. Workspace requests offer one Arm Sutra confirmation and resume
in the same chat after approval. Sutra: Arm Workspace Access also arms in place.
Your workspace prompt creates or resumes a task. Edits/tests ask for concrete file permissions at the point of need, then
confirm the exact action. There are no goal or workflow pickers during arming.
The review participant is @sutra. Model tiers are Sutra Auto / Low / Mid / High.

ROUTING AND CONTEXT
A ready or armed badge does not prove a prompt used Sutra. Check the selected
model and response invocation details. Other providers can bypass this harness.
Reloading preserves settings and conversation checkpoints, but disarms workspace
actions. Legacy Karma receipt pointers remain valid under the same host checks.

WORKFLOW
Native chat now sends events to one persisted host state machine. It classifies
the request, waits for user grants, maps fresh source, allocates child reviews,
joins accepted results and prepares actions. Children run sequentially in the
same chat. Their model calls inherit the full contract, Smriti context and a
narrower source scope. Parent and child allocations are checked by the unchanged
STP kernel. Switching models never refreshes spent budgets or grants permissions.
After an interrupted review, select another Sutra tier and send "continue" to
resume its saved workflow. A different request cancels unused child reservations
while retaining consumed budget and evidence. Arming may be required after reload.

DASHBOARD
Click Sutra Monitor in the status bar, or run Sutra: Open Monitor.
The combined dashboard opens beside Terminal and Output. Workflow & child
reviews shows stages, model assignments and reserved/consumed child allocations. It shows CPU/GPU
temperatures, CPU usage, memory, individual kernel decisions and input/output
totals, recent-call charts and per-stage counts, with model, stage, outcome and
time filters. @sutra /usage opens it too. Counts arrive after each model call;
missing counts remain unknown. Filters cover the latest 250 workspace calls.
Live readings are separate from token filters and clear when stale. Context
diagnostics distinguish a retained checkpoint from missing context.
Run Sutra: Open Local Harness. Overview shows resources and actual routing;
Usage filters measured input/output tokens by stage and model; Settings provides
installed-model dropdowns. Temperature readings are advisory.

COMPATIBILITY
Internal karma.* settings, command IDs and model IDs are retained intentionally.
The canonical STP kernel is unchanged. Harness adapters/policies are updated.
Conversation data from 0.16–0.19 is validated and restored; historical executable
proposals remain subject to stale-policy checks.
VS Code now receives an offline tokenizer estimate instead of a byte count.
Its compaction threshold remains under VS Code's control. Harness budget charges
and measured Ollama token counts remain distinct. Smriti now preserves full turns
in receipt-backed pages, includes exact recent turns and pinned user quotations,
and retrieves older pages by relevance or model request. The working summary
uses the output allocation instead of a fixed 1,000-character cap. Model context
comes from installed metadata/configuration, capped at the tested 65,536-token
runtime allocation. Pinned statements that cannot fit cause a visible stop.
This preserves provenance, not guaranteed semantic comprehension or recall.
Conversation has no web tools. Diagrams are conceptual, not factual verification.
Python edits/tests are limited to supported operating-system sandbox adapters.

SOURCE AND TRACKING
https://github.com/dtmohan/Sutra
Build: python3 -B package.py --output dist
See README.md and docs/development.md in the source repository.

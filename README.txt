Sutra 0.45.3 — Strata High startup

On macOS, enable Karma › Strata: Start On Startup in User Settings to start
the registered io.sutra.strata-high LaunchAgent when High is set to Strata.
The service profile must match the configured local port and model. Run
Sutra: Start Strata High in the Command Palette to recover after a stop.

Sutra 0.45.2 — Model availability

An unavailable High provider leaves Low and Mid usable. Auto reports
which available configured model it can use for work routed to High.
Fixed High reports how to recover Strata and retains the configured model.

Sutra 0.45.1 — Strata review reliability

Strata selects citation IDs, which Sutra checks against exact source.
Adaptive caps Strata's reasoning and generation within the existing
transport deadline. Architecture requests can name functions to prioritize
their source in the review packet.

Sutra 0.45.0 — Opt-in Strata High

Ollama supplies the default model choices. To opt in, start Strata on
127.0.0.1 with its model loaded. Set karma.strata.port and karma.strata.model
in VS Code User Settings to the server port and the exact model ID reported
by /v1/status. Run Sutra: Configure Local Models, refresh, select strata:high
in the High tier, and save. Use Sutra High for a fixed chat or read-only
review. Use an Ollama tier for edits and tests.

Choose and save an Ollama model in High to switch back. Set the Strata port
to 0 to disable discovery. If your server requires authentication, set
SUTRA_STRATA_API_KEY in the VS Code extension host's environment.

Sutra 0.44.1 — Recover child allocations

Read-only workspace reviews save child findings between slices. After a timeout
or output-limit pause, select a model tier and send continue in the same chat.
Sutra can use one admitted attempt when a repair reserve would prevent the
saved packet from fitting. When the Monitor offers Increase allocation, a
confirmed increase adds measured capacity to the existing child execution.
The saved checkpoint, read scope and recorded spending carry forward.

Long document reviews

Approve the read of a named workspace DOCX in Sutra Auto. Extracted text
above 24 KiB and up to 96 KiB is reviewed in sections. Auto starts synthesis
on Low, checks the answer against the extracted quotations and can try a
higher configured tier within the same task allocation. A fixed tier runs
both stages with the selected model.

Sutra 0.42.1 — Isolate chat and document context

A native chat restores receipts that belong to its workspace and conversation.
Approving a new named DOCX starts a document-specific working set. Reviewing
the same approved snapshot can carry forward its earlier review turns.

Sutra 0.42.0 — Resolve implicit document follow-ups

Follow up in the chat that reviewed a document. The classifier can request
one eligible approved snapshot using an identifier. Sutra verifies the
selection, then supplies the snapshot to the answering model.

Sutra 0.41.3 — Resolve absolute DOCX paths in the workspace

An opinion request with an absolute or relative DOCX path inside the workspace
can open a one-time read approval. Continue a saved request in the same chat to
recover from an earlier request for pasted text.

Sutra 0.41.2 — Continue an approved document review

Follow up about the same document in its original Sutra chat to reuse the
approved snapshot. Name a new file to request a separate read.

Sutra 0.41.1 — Review named workspace documents

Name a DOCX in Sutra Auto. Sutra locates it in the active workspace folder
and requests a one-time read. Choose the intended path when names match
more than one file. Continue a saved review in its original chat.

Sutra 0.41.0 — Resume when evidence arrives

When a review needs a document or source target, Sutra pauses the request.
Reply in the same chat with Document text: and the relevant résumé section,
Use this workspace, or Workspace files: followed by one relative path per line.
The saved request keeps context and spending across reloads and model changes.
Workspace access follows the workspace's authorization settings.

Sutra 0.40.1 — Route document reviews in chat

If a résumé, CV, or cover letter is referenced without its content, Sutra
asks for the text. Paste the relevant section below a separate Document text:
heading in Sutra Auto to review it in chat.

Sutra 0.40.0 — Open tasks in native chat

A new workspace request parks the focused task and starts with its own goal
and scope. Use Open tasks in Sutra Monitor to focus, resume, or close tasks
from the last active Sutra chat. Closing the focused task restores focus to
the previous open task. Send continue in the originating chat to resume it.
You can choose another Sutra model tier before continuing.

Workflow checkpoints, memory references, permissions and consumed allocation
stay with each task across reloads. Resuming rechecks scope and source
freshness. A saved edit or test still needs action confirmation.

Sutra 0.39.0 — Adaptive thinking and fixed-tier settings

Adaptive requests the lowest positive reasoning level supported by the model
for substantive chat. Sutra checks the wording for debugging supplied code,
numerical planning, comparison of approaches, discussion of trade-offs, or
an explicit request to reason. Routine chat starts at the lowest known effort.
The host checks these cues without another model call.

Multi-file report synthesis can use thinking when the existing allocation has
its full output reserve and a spare draft attempt. If thinking exhausts the
reserve, Sutra can retry at the lowest supported level. For Adaptive, evidence
selection, child reviews and claim checks start at the lowest known level.

Run Sutra: Configure Reasoning. Set the shared mode for Sutra Auto. For each
fixed tier, choose Inherit, Adaptive, On, Off, or Model default. A fixed tier
continues to use its configured model. The Chat lightbulb requests thinking for
one turn. Call details show the requested reasoning level.

Sutra 0.38.5 — start in chat and recover reviews where they stop

Run Sutra: Open Chat (Sutra Auto) to start in VS Code's native Local Chat.
Ask for a codebase review there. Sutra asks for workspace access when needed,
then discovers and reads relevant files.

The advanced architecture review proceeds from inspection to report. Read
another file and retry recovers a review that needs more evidence using its
current scope and remaining budget. Sutra Monitor shows the stop and available
actions. Dashboard tabs, form descriptions and control contrast are improved.

Choose Resume this request in the original chat or Resume in Sutra Monitor to
continue through Sutra Auto. For a different tier, select it and send continue
in the original chat.

Sutra 0.38.4 — verify protocol events and explain review stops

Sutra checks protocol events against their hash-chained audit copies. New events include host timestamps. Architecture preflight stops appear in the review panel and Sutra Monitor.

The review panel shows catalog exclusions and the remaining repository operations. Catalog scans use one operation each. Sutra verifies the current budget and contract when it commits an operation and checks replay identity before returning a receipt. When source evidence is short and the repository budget is exhausted, the review reports a terminal stop.

Headless admission can refresh missing or expired telemetry with the bundled sampler on supported hosts.

Sutra 0.38.3 — recover an architecture review step

The Sutra: Review Codebase workflow checks for enough current source evidence before requesting a report. It can reuse settled reads even if earlier model notes omitted them. If an architect call reaches its output limit, Sutra can spend one existing retry with thinking off, keeping the prior checkpoint and read receipts. A read continuation must match a cursor issued for that file.

Sutra 0.38.2 — resume an interrupted claim review

If the architecture claim reviewer reaches its output limit with thinking enabled and a retry available, Sutra pauses with the draft, completed reviews, source evidence and consumed budget saved. Choose Resume in Sutra Monitor, then send continue in chat. Sutra requests thinking off for the unfinished page and checks that the saved source evidence is current.

Sutra 0.38.1 — thinking for one chat request

Click the lightbulb in the VS Code Chat title bar, or run Sutra: Think on Next Request from the Command Palette. This requests thinking for one Sutra turn even with Adaptive selected. Click the lit bulb or run Sutra: Cancel Thinking for Next Request before sending to cancel. Later requests use your saved reasoning setting. Sutra shows thinking inline when the local model streams it.

Sutra 0.38.0 — Git provenance and targeted architecture evidence

Architecture reviews record the open checkout's commit, branch and working tree status before reviewing source. Upstream status uses local tracking data and is labeled accordingly. A request for the latest upstream version asks you to confirm the open snapshot or open the intended checkout. Sutra checks the snapshot again before it publishes the report.

The review can request up to four precise source excerpts in one follow-up round. The host checks each requested path and line against the workspace scope and admitted plan, charges the active allocation, and verifies source against the read receipts before publication. Evidence selection uses the lowest advertised reasoning setting. The final review uses your configured setting.

Sutra 0.37.0 — automatic allocation within workspace limits

Read-only native reviews can draw from a workspace reserve when the host measures a shortfall. The reserve uses cumulative ceilings per review. The defaults are 128 MiB of source, 2,000 repository operations, 80 model calls, 500,000 model budget units and 3,600 seconds. An approved draw increases the working allowance, preserves previous spending and creates a linked bounded execution. Sutra revalidates any source receipts it carries forward.

Use the User setting **Sutra: Workspace Allocation** to override these ceilings or disable automatic draws. The setting accepts `enabled`, `source_bytes`, `operations`, `calls`, `tokens`, `seconds` and a `byFolder` map keyed by the local workspace root. Each folder override accepts those same six policy fields. Workspace files cannot grant reserve authority. Explicit overrides outside the automatic ceiling still use **Sutra: Increase Task Allocation**.

Sutra 0.36.0 — source budget recovery on saved reviews

Sutra checks the pinned task, constraints and conversation against the selected model's context before opening source for a review call. After a pause, it can reuse a settled source snapshot for the same saved execution after checking the current file bytes. Changed files need fresh analysis within the remaining allocation.

Work started under an older policy uses the explicit review restart. If the old review has already spent its source allocation and has no eligible snapshot, increase the task's allocation in Sutra Monitor before restarting. Previously consumed work remains recorded.

Sutra 0.35.0 — persistent workspace permissions

Choose **Sutra: Workspace Permissions…** from the Command Palette or pick a profile the first time native chat needs project files. **Read only** enables scoped reads. **Workspace agent** also enables scoped edits and sandboxed tests with the existing action approval flow. Optional tools have their own approval policy.

Sutra restores the profile when you reload a trusted workspace or switch models. Use **Sutra: Revoke Workspace Access** to remove it. Paused requests retain their context, task constraints and consumed budget. Send **continue** to resume a saved request.

Sutra 0.34.0 — bounded execution sessions and fresh source paging

Workspace requests now have a fixed execution allowance inside the cumulative task budget. Retries, reloads and model switches preserve that allowance, memory and permissions. A later execution uses only the task capacity that remains; an explicit increase creates a linked successor without erasing spending.

Before asking for more capacity, mapping and synthesis select smaller fresh source pages and disclose deferred files. The monitor shows execution spending and paging alongside cumulative task totals. The canonical STP kernel remains unchanged. See [execution sessions](docs/execution-sessions.md) and [validation](docs/validation-0.34.0.md).

Sutra 0.33.0 — registered tools with governed installation

Sutra can now request Ruff lint checks and Pyright type checks from native chat. Ask “Run Ruff on code.py” or “Check code.py with Pyright.” The host offers installation if needed, then a separate source-check approval. **Sutra: Manage Tools** opens the catalog. Sutra Monitor provides **Manage tools** and **Tool permissions** buttons.

Use **Sutra: Configure Tool Policy** to choose Ask each time, Preapprove selected operations, or Disable optional tools. A preapproval requires one explicit confirmation of the workspace, operation list and registry identity; changing that policy or registry requires confirmation again. A repository settings file cannot grant itself authority. Workspace permissions and optional tool approvals are separate.

The first registry pins Ruff 0.17.5 and Pyright 1.1.414 to official package URLs and integrity hashes. Installation uses Sutra storage, without package-manager scripts or changes to project dependencies. Checks use fresh, approved Python snapshots in the macOS sandbox, with no network, source writes, fixes or project configuration. Other platforms have package mappings, but execution currently fails closed until a sandbox adapter is implemented and tested.

Tool operations reserve task/ancestor capacity and retain consumed charges through interruption and model changes. Receipts and paged diagnostic evidence use the existing Smriti store. Lost replies recover from receipts without replay; a newly approved reinstall quarantines an unverified prior copy. The canonical STP kernel is byte-for-byte unchanged. See [tool permissions and operation limits](docs/capabilities.md) and [validation](docs/validation-0.33.0.md).

Sutra 0.32.0 — paged architecture claim reviews

Architecture reports now receive a separate claim review in context-sized pages. Each page carries complete claims and host-generated evidence IDs bound to exact source text, paths and line locations. The reviewer selects IDs instead of copying quotes. All relevant claim/evidence pairs must be reviewed before publication; a contradiction on any page withholds that claim. Source IDs prove provenance, not correctness.

Accepted pages and the original draft survive interruption. Retrying the same request can continue unfinished review pages with another configured model, after fresh source, contract and reviewer-identity checks. Completed pages are not replayed. Each page has at most two total attempts across restarts; every dispatch still needs context, resource and remaining task allocation. Model changes never refund consumed budgets or grant permissions. Source-linked IDs appear in the report; detailed page judgments remain available in review details.

The canonical STP kernel is unchanged. This is a bounded review of the writer's supplied excerpts, not an exhaustive semantic review of every mapped file. Claims requiring evidence spread across pages can remain unsupported. A smaller model must fit each saved page; saved plans are not silently rewritten to drop context.

Sutra 0.31.0 — explicit task allocation recovery

When a saved review pauses because its task/context allocation is too small, open Sutra Monitor and choose Increase allocation… (or run Sutra: Increase Task Allocation). Review the added capacity and new totals in the confirmation dialog. Approval preserves the original task, constraints, context and all consumed spending; send continue in the same chat afterward. If an extension update requires Restart review, accept that separate recovery choice to read source afresh.

Allocation approval does not start inference, grant file permissions, enable reasoning, or override hardware/model capacity. The model cannot raise its own budget. Monitor shows spent/total amounts and the amendment version.

Models receive the task, relevant requirements, fresh evidence and Smriti context. Host governance records and constraint echoes stay in the gateway ledger. Model-proposed actions still require host validation and the existing permissions; source freshness and the STP kernel are unchanged.

New calls reserve the serialized model-facing input plus output before dispatch, then settle to completed runtime input/output counts. Failed completed calls still spend their measured usage; interrupted or unmeasured calls retain the full reservation. Historical charges are unchanged. Sutra Monitor distinguishes the model packet allowance from the settled task charge.

Recent turns, pinned statements and requested pages stay mandatory. TOC addresses are packed before optional older dialogue so the model can ask for a missing page. Old executable proposals remain stale after a policy update; compatible conversation data can be restored.


Output-limit failures now explain that reasoning and answer share the output allowance. A truncated answer remains quarantined. Adaptive reports can make one compact retry only within already allocated attempts and remaining task resources; failed calls remain charged. Reports scale detail to available evidence instead of targeting a fixed word count. Explicit On and Model default choices remain unchanged.

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

Auto starts new requests with the configured low-tier local model. Greetings and routine questions can finish in that first call without workspace authorization or running review tools.

Choose Model reasoning → Adaptive in Sutra → Settings → Reasoning & runtime, then Save runtime settings. Adaptive is the default for new configurations. Existing Off, On and Model default choices are preserved; this upgrade does not silently override them. The native VS Code setting is karma.thinking.

Adaptive keeps routine chat at the lowest supported effort. A small deterministic heuristic selects the first supported positive effort for explicit reasoning requests, numeric planning/corrections, code analysis and fenced structured results. It does not prove task difficulty. If a model requests missing Smriti context, Sutra pages it in first at the same effort. If the model reports that the evidence is sufficient but it needs stronger reasoning, the host checks the next supported effort on that same model. Only after that model's supported choices are exhausted may Auto select the next configured, untried tier. Explicit Low/Mid/High selections can increase reasoning but never silently switch models. A workspace request hands off immediately for fresh scoped reads; workspace generation starts at the lowest known control in Adaptive mode. Architecture writers, child reviewers and report checkers bind the setting to their receipts. This is not a retry cascade for every tool role.

Controls come from Ollama /api/show, not model names. Named levels must be advertised. Ollama documents think=false as a generic lowest-effort request; models that cannot disable thinking may still reason. If true is advertised, Sutra can explicitly enable thinking on a retry, even when false is absent from the metadata. The backend may map both requests to the same effective effort; this is not a guarantee of more computation. Unfamiliar or missing metadata uses the model default without inventing levels. Increasing reasoning can cost more time and tokens without improving correctness; a model's request for help remains unverified.

Reasoning choices and supported-control metadata are bound to receipts and rechecked before dispatch. Each retry uses the invocation gateway, existing task allocation and unchanged STP kernel. Reasoning and final text share the existing output reserve; no extra token or time allocation is created. Resource waits, exhausted budgets and mandatory context that does not fit produce a saved pause. Resume preserves spent budget and the pending effort level. Changing the saved request's reasoning setting or its model capabilities requires restoring the setting or starting a separate request, rather than replaying from the cheapest level.

Recent turns, pinned user statements and requested pages stay intact across model switches. Optional recent turns respect their allocated share so they cannot take all space for retrieval and the table of contents. The host never drops mandatory context to force a model to fit. Missing facts that cannot be paged require clarification, not a larger-model guess.

Chat distinguishes loading context, increasing reasoning on the same model, and escalating models. All routing stages and repairs contribute to token totals. Only the final admitted answer becomes conversation memory; quarantining an intermediate routing receipt invalidates dependent answers. Source freshness and workspace/edit/test permission checks remain independent of model and reasoning selection.

Conversation beside saved workspace work

New messages are classified through the invocation gateway before any saved-review recovery. A greeting stays conversational, with no permission prompt, source mapping or review restart. A saved review keeps its context, constraints and consumed allocation. Send continue/resume explicitly to resume it; new workspace work asks before replacing it. Policy-upgrade restarts remain an explicit choice.

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

Resource admission stops now pause native chat normally instead of causing Autopilot error retries. The saved stop includes the blocking OS pressure/memory measurement or missing/stale telemetry reason. Wait for Resources: ALLOW in Sutra Monitor, choose Resume and send continue in the same chat. Resume checks fresh telemetry before dispatch, retains completed child work, and keeps the original request, constraints and consumed budget across model switches. Conversation can resume without workspace authorization. Resource recovery does not replenish task allocation; the STP kernel and resource thresholds are unchanged.

Architecture reports now receive a separate claim-versus-source review before publication. Sutra prefers a smaller installed reviewer with different recorded weights, preserves the task contract, and charges both stages and repairs against the same allocation. Unsupported or contradicted claim groups are omitted; Review details records the verdicts, omitted claims and identities. A malformed or unavailable review withholds publication. Different weights and matching quotations do not prove correctness or statistical independence. API maps use the same identity-aware reviewer selection.

Test receipts compare outcomes only when the complete declared test inventory, runtime/runner identity, limits, policy and task epoch match. Changed tests are labelled separately; missing or incompatible historical provenance is inconclusive. These remain process observations, never proof that assertions ran or that a change is correct.

The experimental token-choice sensor is off by default. Enable numeric diagnostics in Sutra → Settings → Reasoning & runtime, or choose flagged token text explicitly. It measures available token likelihoods, not correctness; it never feeds STP, alters permissions or chooses a model. Local summaries are capped at 1 MiB, with separate cohorts for model identity, stage, schema and decoding settings. Some backends do not return log probabilities. Enabling them can add transport/compute overhead. Numeric mode omits token text from the sensor log; ordinary model traces retain their existing storage policy.

Architecture reviews now have consistently formatted components, request flow, strengths, risks and next checks. Sutra selects relevant declaration bodies and records their line ranges. Workspace calls prioritize fresh source over optional older dialogue, while retaining the latest turn, pinned facts/constraints and explicitly requested memory pages. Optional repair shares and expanded output reserves yield when required context cannot fit. Structured reports retain a separate reviewer share; every actual attempt still requires admission within the remaining shared allocation. Components, findings and flow references must name files whose excerpts were delivered; this checks source provenance, not the truth of a model conclusion. The host opens a compact dependency diagram beside native chat, built from literal import/require references to existing files. These are local import candidates; runtime loading is not verified. Same-file recursion is omitted from that overview and retained in the full static map. Arrows do not imply execution order. Use Review details and source citations for protocol identity, checked quotations, outstanding checks and the full map. Open Last Diagram reopens the saved review snapshot; it is not a fresh read.

Architecture reviewers receive a fresh cross-page file and module inventory, budgeted source excerpts, and available child reports. The evidence-delivery line distinguishes mapped files from sampled implementation coverage. Workspace context uses installed model metadata within remaining allocations; model changes do not reset budgets or permissions.

The conversation stage can request a workspace review in the same chat. Its lack of direct file tools does not disable the harness. Known false file-access refusals are withheld and sent through bounded repair. Workspace permissions, task scope and remaining budgets govern execution. Existing conversations keep their saved memory and contracts.


Use Sutra → Settings → Model reasoning to enable reasoning at the lowest supported level or use model defaults. Adaptive is the default for new configurations; existing choices are preserved. Calls & reasoning in Sutra Monitor shows live expandable details for parent and child calls, protocol outcome and final input/output tokens. Compatible VS Code hosts also show thinking inline; older hosts use the Monitor. Reasoning is unverified model commentary and is never saved as task memory or authority. It shares the existing output budget with the answer. Open each call’s reasoning details link in chat, or Sutra Monitor → Calls & reasoning → Open reasoning details. Plain-text diagnostic history survives reloads, bounded to 12 calls, 7 days, 1 MiB per workspace and 16,000 characters per call. It may contain source references. Older vanished streams cannot be recovered because previous versions did not save reasoning.


Sutra Monitor shows workspace permissions separately from the last request. A request that needs project files waits for the first permission choice. Future requests use the saved profile within the same trusted workspace.

Saved mapping failures are bound to their parser environment, contract and allocation. After a parser/policy upgrade, legacy failures rejoin the fresh-read queue; unchanged failures are not retried indefinitely. Existing receipts, permissions and consumed budget remain intact.

Child reviews read fresh, byte-bounded source pages within their existing allocations. An interrupted review from an older policy offers Restart review in the same chat; the original request, constraints and consumed budget stay intact. Compatible conversation memory is validated and retained on restart. Old worker results are not reused.

Compiled Python stdlib imports and readable project imports are recognized without widening action permissions. Long source lines continue with version-bound cursors. Test confirmation lists the exact snapshot files; required project modules must be explicitly granted.

Runtime checks now cover application code and the unchanged STP kernel before local modules load. Repeated audit checks reuse only an unchanged verified database connection.

Whole-codebase Python mapping uses the same source-page work allocation as TypeScript, instead of the small API-review packet limit. Pauses show the underlying cause and mapped-file counts in chat; file failures also appear in Sutra Monitor.

Resume or Discard a saved request from Sutra Monitor. Resume submits a
continuation in the originating Sutra chat and retains the draft. Context
allocation shortages are saved stops. Resume returns to the stopped stage
under the same contract and remaining budget. Interrupted actions are
reconciled without replay.

Task form drafts and expanded source lists survive refreshes. Start Ollama, then use Retry model discovery in Settings if setup is unavailable. File scopes persist with the task. Revoking workspace access stops native workspace work. Every edit or test still needs action confirmation.


Previously Karma. Your installed extension ID and saved settings are preserved.

GET STARTED
Run Sutra: Open Chat (Sutra Auto). This saves the compatible Local chat
and model for new chats. Existing chats retain their own selected model.
Sutra discovers installed models and fills empty tiers. Run Sutra: Configure
Local Models to change those choices.
Chat normally. The first request that needs project files offers Read only
or Workspace agent permissions. Approval is saved for the trusted workspace
and continues the request in the same chat. Workspace agent access lets Sutra
request file scopes for edits and local tests, then confirm each exact action.
Use Sutra: Workspace Permissions… to change your choice or Sutra: Revoke
Workspace Access to remove it.
Sutra Auto routes automatically. The Low, Mid and High entries in the model
picker are explicit overrides. Open Sutra Monitor for recovery and diagnostics.

ROUTING AND CONTEXT
The status bar shows workspace access. Check for a Sutra model beneath the
prompt and invocation details in its response to confirm routing.
Reloading preserves saved permission choices, settings and conversation
checkpoints. Legacy Karma receipt pointers remain valid under the same host
checks.

WORKFLOW
Native chat now sends events to one persisted host state machine. It classifies
the request, waits for user grants, maps fresh source, allocates child reviews,
joins accepted results and prepares actions. Children run sequentially in the
same chat. Their model calls inherit the full contract, Smriti context and a
narrower source scope. Parent and child allocations are checked by the unchanged
STP kernel. Switching models never refreshes spent budgets or grants permissions.
After an interruption, choose Resume this request in the original chat. Select
another Sutra tier and send "continue" there to use that tier. Replacing the saved
request requires confirmation and releases unused
child reservations. The saved task retains constraints, consumed budget and
evidence.

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

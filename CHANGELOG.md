# Changelog

## 0.23.0

- Calibrate cold MLX process memory across repeated synthetic workload sizes, through a tool-free invocation role and the unified gateway. Keep workload token counts separate from requested context and exclude pressure-affected or interrupted observations.
- Bind versioned, integrity-checked empirical profiles to exact model identity, backend executable/libraries, OS/hardware and server epoch. Apply the next larger measured workload plus an explicit uncertainty margin; cap paging to observed coverage and never extrapolate.
- Feed calibrated additional-memory cost through the existing Karma adapter and unchanged STP kernel. Preserve hardware pressure checks, system reserve, context pins, permissions and spent task budgets.
- Wait for a fresh post-unload memory sample before the next model allocation; retain a bounded wait and the OS pressure gate.
- Display profile identity, coverage and margin in Sutra Monitor. Missing/mismatched profiles fall back explicitly; backend restarts require fresh calibration because hidden server options may change.
- Preserve data-only 0.22.0 conversations; add recovery/model-switch and invalid-profile regressions.

## 0.22.0

- Size local runtime context to the admitted packet plus output reserve and margin; retain the original payload, task allocation and mandatory Smriti context. Known conventional KV layouts also constrain optional context selection before paging.
- Coordinate Sutra inference across windows with an OS-released local slot. Recheck resources and estimated incremental model memory before registering an invocation; publish model residency, compression, swap, context plans and recovery events in Sutra Monitor.
- Keep shared Ollama as the default. An explicit application-level exclusive-server setting permits at most one identity-matched idle release from a completed Sutra call per recovery. Record release intent and outcome; never unload interrupted or unowned models automatically.
- Require distinct healthy telemetry samples on resource resume. Recovery does not run inference, grant permissions or reset consumed budgets. Unknown MLX/hybrid KV costs remain labelled unknown; estimates do not prove peak RAM requirements.
- Preserve data-only 0.21.7 conversation compatibility; keep the canonical STP kernel unchanged.

## 0.21.7

- Persist resource admission failures as normal workflow pauses during routing, mapping, child reviews and synthesis/coding. Native chat shows recovery guidance instead of a retriable provider exception.
- Record the rejected measurement or unavailable/stale telemetry reason. Explicit Resume checks fresh resources before returning to the stopped stage; pressure cannot trigger automatic retries or replenish allocation.
- Preserve the original prompt, contract, completed child results and consumed budget across reloads and model switches. Routing recovery needs no workspace arming; workspace recovery retains the existing permission checks.
- Add data-only conversation compatibility for 0.21.6. Keep the STP kernel and resource admission thresholds unchanged.

## 0.21.6

- Let the initial architecture draft and review use shares previously reserved for optional repair attempts; reduce attempts accordingly and retain the model's normal output reserve when expanded output cannot fit. Task/parent allocations, permissions and STP admission remain unchanged.
- Save context shortages as recoverable workflow pauses with separate model-capacity and remaining-task-budget diagnostics. Resume returns to synthesis/coding, preserving context and consumed budgets; native chat receives a normal paused response instead of an exception that triggers Autopilot retries.
- Preserve validated conversation handoffs through a policy-only review restart, including data-only conversation compatibility for 0.21.5. Old executable proposals and child results remain invalid.
- Display the saved stop reason in Sutra Monitor. Regression checks cover exhausted allocations, optional repair fallback, reload/model-switch recovery and chat feedback.

## 0.21.5

- Bind test outcome comparisons to declared test inventories, contract epochs, runtime/runner identity and limits; keep assertion verification false.
- Record fresh mapper/reviewer identities at dispatch, distinguish manifest from weight identity, handle same-weight aliases and unknown metadata conservatively, and reject identity changes during a call.
- Review architecture claim groups in a separate governed invocation; omit disputed claims from publication and restored model-switch context. Account for reviewer and repair costs in the existing task budget.
- Add an optional observe-only token-choice sensor with numeric/text privacy modes, bounded storage, UTF-8 byte alignment, cohort metadata and isolated diagnostic failures.
- Preserve data-only conversations from 0.21.4; keep the canonical STP kernel unchanged.

## 0.21.4

- Render architecture reviews as structured components, request flow, grounded strengths/risks and next checks, with host-owned Markdown and safe webview elements.
- Select relevant declaration bodies beyond import preambles, retaining fresh source snapshots, line ranges and unchanged budget/permission checks.
- Preserve mandatory Smriti context while bounding optional older dialogue by model capacity; reserve one full repair within the existing task allocation.
- Open a bounded diagram from literal local import candidates; keep full static maps and protocol IDs in review details.
- Preserve data-only conversation compatibility with 0.21.3; keep the STP kernel unchanged.

## 0.21.3

- Deliver fresh structural context across all mapped source partitions instead of selecting only one snapshot and a tiny unrelated excerpt.
- Distinguish host-supplied source from optional Smriti pages and harness-created artifact flags.
- Preserve child answers and checked citations for synthesis, and display measured evidence coverage.
- Allocate review context from installed model metadata and remaining task/ancestor budgets, reserving bounded repair capacity.
- Keep the STP kernel unchanged and retain data-only conversation compatibility with 0.21.1/0.21.2.

## 0.21.2

- Keep separate bounded reasoning records across reloads, with per-call links in native chat and read-only details panes in Sutra Monitor.
- Distinguish conversation routing, child review and workspace review; close each native thinking stream explicitly.
- Retain diagnostic model/stage identity and interrupted status without adding reasoning to task memory, permissions or protocol evidence.
- Keep the STP kernel, model transport, policy hashes and remaining task budgets unchanged.

## 0.21.1

- Distinguish conversation-stage execution limits from the available same-chat workspace handoff. Bind explicit host routing capabilities into every new conversation packet.
- Reject known misleading file-access refusals and use bounded repair without granting permissions or resetting budgets. Preserve legacy conversation contracts and memory.
- Restore data-only conversation history from known 0.20.x and 0.21.0 policies after upgrade; historical executable proposals remain stale.
- Keep the canonical STP kernel unchanged.

## 0.21.0

- Stream local-model reasoning with parent/child invocation identity; show expandable Monitor details and native thinking where the host supports it.
- Add an accessible reasoning setting with model-aware controls and unchanged total output allocations.
- Keep partial answers withheld until validation; preserve context, permissions and budget accounting. No STP kernel changes.
- Forward workspace permission feedback into the Monitor.


## 0.20.5 — workspace confirmation and stale failure recovery

- Bind saved mapping failures to the policy, parser tools, Python version, contract and page allocation. Requeue legacy or outdated failures once, with an audit entry; retain consumed budgets and permissions.
- Keep temporary resource waits out of the persistent file-failure queue.
- Show waiting, granted, declined and reused workspace-access feedback; record dialog transitions in routing diagnostics and show the last outcome in Sutra Monitor.
- Test pending/declined confirmations, actual VS Code dialog cancellation/approval, and recovery of the reported 29 failures on a copy of the saved 113-file review. STP kernel unchanged.

## 0.20.4 — child source paging and upgrade recovery

- Keep child excerpt scope stable through canonical JSON storage; ranked excerpts no longer fail receipt replay because their object keys were sorted.
- Read each child partition through fresh source pages bounded by the existing task page allocation. Charge each page through the kernel; validate page ownership and source versions before invocation and join.
- Retain compiler configuration across child pages; split parser work-unit overflow without enlarging allocations. Report the specific file and remaining bytes when a read cannot fit.
- Detect old-policy workflow bindings before resuming. Explicit Restart review preserves the original prompt, task contract and consumed budget, releases unused reservations and discards incompatible model results. Contract/calibration changes cannot use this restart path.
- Keep recovery in the existing chat and display the incompatibility in Sutra Monitor. STP kernel unchanged.

## 0.20.3 — review findings and portability

- Recognize compiled standard-library imports, including Python 3.9, and local modules within the existing read grant without widening edit or test scope.
- Preserve long source lines with UTF-8-safe, file-version-bound read cursors. Reject changed-source continuations and altered request replays.
- Reuse verified audit results only on unchanged database connections; invalidate on writes, external commits, schema changes, rollback or file replacement. Cold checks still verify the complete chain.
- Normalize Karma and Smriti measurements using the selected boundary thresholds. Canonical STP kernel unchanged.
- Verify bundled application files and the kernel before loading local modules. Rebuild native telemetry from source, record provenance and add macOS CI compilation.
- Show exact test snapshot contents and document dependency limits. Use portable QA temporary directories; cover Python 3.9, 3.11 and 3.13 in Linux/macOS CI.
- Existing policy-bound workspace tasks require a fresh task after upgrading; conversation memory is retained.

## 0.20.2 — larger Python maps and actionable pauses

- Use the source-page allocation for Python static maps; retain the small API evidence packet limit and enforce aggregate graph bounds across languages.
- Persist mapping pause diagnostics across restarts and expose file failures in chat and Sutra Monitor. Expected pauses no longer become generic native-chat request errors.
- Preserve context, permission bindings and consumed budgets on Resume and model switches. Clear obsolete transient mapping errors after retry.
- STP kernel unchanged. Existing policy-bound workspace tasks require a fresh review after upgrading; conversation memory is retained.

## 0.20.1 — recovery and UX stabilization

- Explicit Resume/Discard, paused mapping recovery and pending-workflow discovery; retain context, permissions and spent allocations across restarts and model switches.
- Executing actions remain pending until cleanup and receipt settlement; recover accepted child receipts without repeated inference.
- Preserve task drafts, source disclosures and keyboard focus during refresh; expire stale temperature readings.
- Actionable model discovery errors, task permission duration, exact Python test selections and safer monitor navigation.
- Current packaged smoke contract, regression journeys and bundled third-party notices. STP kernel unchanged.

## 0.20.0 — centralized orchestration

- Move native workflow selection, source mapping, branching, child dispatch and
  joins into one persisted host state machine. The UI renders states and asks
  for user grants; it no longer selects the workspace execution path.
- Reserve child allocations atomically against the parent, using source-map
  measurements and available budgets. Every model, repository and action
  commitment records its orchestration owner and kernel admission.
- Run read-only child reviews sequentially through the invocation gateway, with
  immutable lineage, inherited constraints, narrower permissions, fresh source
  and receipt-backed Smriti working sets. Validate freshness again at synthesis.
- Resume interrupted reviews across process/model changes; retain failed-call
  costs and reject stale, quarantined or foreign child results. Track proposed
  actions through confirmation, execution and receipt without replaying them.
- Add workflow/child status to Sutra Monitor. Preserve 0.19.1 conversation data.
- Keep the canonical STP kernel byte-identical. Partition signals are workload
  heuristics, not a verified measure of semantic complexity or independence.

## 0.19.1 — continue in the same chat

- Arming no longer opens or selects another chat. Workspace requests await one
  confirmation, then resume with their original prompt, conversation receipt,
  task contract and existing permissions. No manual resend is needed.
- Cancel, disarm, disable and workspace changes revoke pending confirmations.
  Model switches retain the approved scope; edits/tests keep their action checks.
- Display host-owned workspace progress instead of unverified routing prose that
  may incorrectly claim source access is unavailable or that a child agent exists.
- Clarify that read-only reviews require source access and that permission is
  checked by the host after routing. Model intent classification remains fallible.
- Offer source-derived citation choices to avoid paraphrased quotes. Fresh-source,
  cross-file attribution and bounded-repair checks remain enforced.
- Restore 0.19 conversation data across this policy update. Old executable
  proposals remain stale. The canonical STP kernel is unchanged.

## 0.19.0 — model-aware conversation memory and one-step arming

- Replace the fixed 1,000-character working note with a generation-budget-derived
  limit and installed-model context metadata (including configured `num_ctx`).
- Keep exact recent turns, user-quoted facts and explicit constraints; store older
  turns as immutable Smriti pages. Budgeted TOC, lexical retrieval and governed
  model page requests avoid replaying the complete long conversation.
- Restore compatible 0.16–0.18 conversation data without accepting stale action
  approvals. Missing, corrupt or quarantined ancestors still stop restoration.
- Arm with one yes/no confirmation for workspace reading. Create/resume a task
  from the prompt. Request exact Python edit/test permissions when needed and
  retain budgets across permission changes, including interruption recovery.
- Show conversation allocation and saved/omitted turn counts in Sutra Monitor.
- Preserve the canonical STP kernel byte for byte.


## 0.18.0 — combined monitor and host token sizing

- Expand the docked panel into **Sutra Monitor**: token history, CPU/GPU Celsius,
  CPU usage, available memory, separate kernel decisions and context diagnostics.
- Clear expired hardware readings even if the host stops sending updates; retain
  recorded token totals with explicit stale-state labels. No inference for monitoring.
- Show signed-checkpoint preservation and missing-checkpoint compaction events.
- Replace UTF-8 byte length in VS Code's token-count callback with a bundled
  offline BPE estimate plus 15% headroom. This is approximate for local models;
  measured Ollama tokens and the harness's conservative allocation checks are unchanged.
- Preserve all backend files, the STP kernel, settings and signed memory formats.

## 0.17.0 — docked token dashboard

- Add **Sutra Usage** beside Terminal/Output and a persistent status-bar shortcut.
- Show measured input/output totals, recent-call charts and stage breakdowns;
  filter by model, stage, outcome and time. Preserve filters when the view closes.
- Provide `@sutra /usage`, an overview shortcut and clear chat-footer guidance.
- Share one persistent reader and one nonoverlapping refresh loop across both
  dashboards; stop polling when neither view is visible. No inference is needed.
- Distinguish unknown counts, real zero, empty results and stale snapshots.
- Keep every harness backend file and the STP kernel unchanged.

## 0.16.0 — Sutra

- Rebrand native models, chat participant, commands, dashboard and response
  footers as Sutra. The review participant is now `@sutra`.
- Preserve the installed extension ID, settings, command IDs, model IDs, local
  storage and authenticated conversation marker format.
- Accept legacy Karma and new Sutra assistant receipt labels.
- Add a Git repository, build instructions, provenance and known limitations.
- Keep every harness backend file, including the canonical STP kernel, unchanged.

## 0.15.1 — imported Karma baseline

- Authenticated conversation checkpoints across supported VS Code compaction,
  reload and model switching, with no extra compaction model call.
- Per-stage token accounting, local conversation, offline diagrams, governed
  workspace operations, resource monitoring and versioned calibration profiles.

Historical prototype notes are retained in `docs/legacy-karma.txt`.

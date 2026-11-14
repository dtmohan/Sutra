# Changelog

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

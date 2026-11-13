Karma 0.15.1 — memory survives native chat compaction

The previous provider mistook VS Code's internal summarization request for a new
user message. When history was compacted into a user-role message, the assistant
footer was no longer visible to the parent-receipt lookup. This created separate
empty conversation sessions and lost topic continuity.

Karma now handles the installed harness's compaction request without inference.
It returns an authenticated pointer to the admitted conversation receipt. The
pointer survives flattened history and repeated compaction. A per-chat pointer
is also saved in workspace state when VS Code supplies its conversation ID; this
is an optional compatibility hint, not the only recovery path. No global "last
conversation" fallback is used. New chats and other workspaces stay separate.

The next real model call revalidates the stored receipt, contract and ancestry;
a pointer cannot bypass quarantine or grant permissions. Exact user descriptions
and corrections are paged from the ledger as before, without conversation replay.
Footer status says Memory restored or New conversation. Compaction may still be
shown by VS Code, but this maintenance step makes no Ollama call and consumes no
model tokens. Per-stage token usage remains available in Usage.

Conversation web search is still unavailable. The separate agent's configured
MCP web tools are not exposed to Karma's conversation role. The model should say
so briefly and still discuss the user's actual topic using the supplied context.
No network permission, source access or workspace arming is silently changed.

Reload VS Code after updating and start a new Karma chat for this validator
version. Paste the listening chain once in that new chat; later prompts, model
switches and compaction can then reuse its stored context. STP kernel unchanged.

Karma 0.15.0 — per-stage token usage

Open Karma → Usage to inspect Ollama-reported input and output tokens. Filter
by model, stage, outcome and time. Totals and stage rows apply to the latest
250 invocations in this workspace; the view displays the full invocation count
so the history window is explicit. Conversation sessions are included even
without an armed task. Filters remain usable during inference and survive
panel reloads. Individual calls show conservative context charges separately.

Native Karma replies include a token footer for that response. It sums the
conversation/routing and workspace stages, including repair attempts and
withheld responses. A subsequent tool continuation has its own footer; earlier
response usage is not counted again. Counts also appear on @karma replies and
the panel's review responses. The conversation-details link shows that stage's
receipt and usage; the Usage tab includes all stages.

Measurements come from Ollama response metadata (prompt_eval_count/eval_count),
not model-written numbers: https://docs.ollama.com/api/chat
Input counts include the assembled prompt; output counts include structured
protocol output, not only displayed prose. Ollama does not provide a breakdown
by individual prompt component, so no system/source/memory token attribution is
claimed. Different model tokenizers can produce different counts.

New measurements live in immutable, hash-chained invocation receipts. They
survive restarts and pruning of diagnostic traces. Legacy receipts and calls
interrupted before a response arrives have unknown counts; unknown is never
presented as zero. Partial totals say "known subtotal". Existing conservative
kernel budget accounting is unchanged. The STP kernel bytes are unchanged.

After updating, reload VS Code. Start a new Karma chat for the new validator
version; historical receipts stay retained. Workspace task constraints remain
pinned, and workspace access requires arming after reload as before.

Karma 0.14.0 — formatted answers and offline diagrams

After installing, reload VS Code and open a new Karma Auto chat. Ask a normal
question for Markdown paragraphs, headings, lists, tables or code. Ask “Draw this
setup” for a local diagram beside the chat. The answer now comes first, followed
by a compact conversation-details link; full request/receipt IDs remain in the
dashboard and output log. Code review and action permissions are unchanged.

The bundled Mermaid 12.3.0 renderer runs offline in a restricted VS Code webview.
The model proposes bounded diagram DATA, never executable SVG, HTML or Mermaid.
The host checks schema, node IDs, endpoints, counts and role, then creates the
Mermaid source. The viewer offers fit, zoom, horizontal/vertical layout, Copy
Mermaid and Export SVG through the standard Save dialog. Use Karma: Open Last
Diagram to reopen the latest diagram, including after reload. No new model call
is made to reopen it. Original source-backed code diagrams keep their separate
validators; this conversation renderer does not certify architecture facts.

For an arrow-separated description, the host pages the exact most recent user
chain and up to two later explicit corrections from admitted conversation
receipts. This avoids relying only on a compressed model summary when preserving
component names and connection directions. It does not replay the full chat or
read workspace files. Diagram relationships and product capabilities remain
unverified model output; structural admission is not semantic verification.

This version deliberately refuses stale or quarantined conversation receipts.
A conversation begun under an older validator needs a new chat after upgrading;
its old ledger is retained. Within this version, the compact details link carries
the memory reference across model switches and window reloads. The original task
contracts and the canonical STP kernel are unchanged.

Runtime dependencies: Mermaid MIT license and bundled dependency notices are
retained in vendor/mermaid. The official npm package SHA-512 and version are in
provenance.json; runtime-integrity.json verifies bundled bytes at activation.
https://mermaid.js.org/config/usage
https://code.visualstudio.com/api/extension-guides/webview

Validation: unit, browser and live native-provider tests cover formatted replies,
the nine-device listening setup, correction paging across a model switch, native
diagram rendering, details-link receipt loading and reopening after reload. The
small model still made unsupported hardware claims in its written opinion; these
tests do not certify the audio advice or hardware compatibility.

Earlier release notes follow.

Karma 0.13.0 — conversation in native chat

Reload VS Code after installing. Open Karma chat, check Karma Auto beneath the
prompt, and talk normally: greetings, questions, pasted snippets, planning and
brainstorming work without arming a workspace task. The status now says
“chat ready · workspace unarmed”. Arming is needed only for workspace tools.
The older panel review form and @karma review participant keep their existing
review workflow; use Karma Auto for this conversational path.

The current user message is sent verbatim as the model's user message. A separate
system packet supplies the restrictive conversation contract, current workspace
contract metadata (when present), and an unverified compact note from the last
admitted conversation receipt. No workspace source is read for conversation.
Every model call still uses the unified invocation gateway, local-only provider
checks, resource measurements, task allocations, bounded repair and quarantine.
The canonical STP kernel is byte-identical.

Switch Karma Low/Mid/High in the same chat to change the local model. Its next
fresh call receives the compact note, not the conversation transcript. The host
receipt marker in the assistant response identifies the persisted note across
window reloads. New chats start with no prior conversation memory. If the marker
is lost or removed from the host transcript, memory cannot be restored; if its
receipt is invalid, stale or quarantined, the request is withheld. Summaries are
model-written and can omit details; they never replace pinned task constraints
or become evidence of source contents or completed work.

A model may suggest that a request needs workspace tools. That suggestion grants
no permissions. An unarmed request offers Arm task and stops before source access.
An armed request carries its admitted conversational note as unverified context,
rechecks the existing task binding and uses the same fresh-source,
edit and test paths. Conversation alone cannot execute tools, widen a scope,
change calibration or select a new workspace task. Existing edit confirmations
and test-proof limitations still apply. Required-tool mode fails when no permitted
action is available. Each response and dashboard routing entry identify the
model, role and protocol invocation. Conversation allocation is shared across
model switches; failed calls consume it too. This first version still needs a
trusted local workspace folder and the existing 1–1800 character prompt limit.

Validation and earlier release notes follow.

Karma 0.12.3 — visible arming progress and recoverable setup guidance

After installing, reload VS Code and click Arm task and open chat in Karma.
Choose the current task or a new read-only review, enter its goal, then select
Arm this task in the scope confirmation at the top of VS Code. The badge shows
Karma: finish arming until that confirmation completes, then Karma: armed /
routing unverified. A notification confirms successful arming.

Prompts received before arming or while its picker is open return setup guidance
instead of a failed-request stack trace. They make no model call and cannot
read source, edit code or execute tests. Requests are not silently queued: send
the request again after confirming the task. A nonmodal Arm task action provides
a way to begin setup from a disarmed response.

Repeated Arm commands share the pending setup. Open Chat during arming gives
guidance without replacing the picker. Dashboard buttons that would conflict
with setup are disabled. Escape cancels setup, releases the UI state and allows
a retry. Starting or completing arming clears the earlier chat-selection error.
Greetings in an armed read-only review render the existing host guidance rather
than treating it as an invalid model response. Setup/guidance records are kept
separate from admitted model invocations in routing status.

No scope is silently armed or widened. Reload still disarms; the task history,
budgets, model selection and Local-chat preferences persist. The STP
canonical kernel remains byte-identical to 0.12.2. The chat adapter now accepts
a measured source graph with zero call sites, while still rejecting empty
selections that hide existing calls and rejecting invented relationships.
Earlier release notes follow.

Karma 0.12.2 — compatible chat defaults and request routing evidence

GET STARTED
1. Reload VS Code after installing the VSIX.
2. Click the Karma status item, the Open Karma chat button, or run
   Karma: Open Governed Chat (Karma Auto).
3. Arm the task using the notification action or Karma: Arm Task and Open Chat.
   Successful arming opens the compatible Local chat as well.
4. Check that Karma Auto is displayed below the prompt, then send your request.

The explicit Open command saves both the model and the compatible chat harness:
  chat.defaultModel = karma-auto
  chat.editor.preferCopilotHarness = false
  chat.defaultToCopilotHarness = false
  chat.editor.localAgent.enabled = true
These preferences survive reload/restart and apply to ordinary new chats.
Existing conversations retain their own provider. The command focuses the chat
panel and opens a new Local conversation; it never submits a prompt. Task and
budget history survive reload; permissions still require re-arming. Activation
alone never rewrites your settings. Managed/workspace overrides and opening
failures produce errors and roll back this command's preference changes.
Model tier choices, utility models, autocomplete and Copilot sign-in settings
are preserved.

ROUTING STATUS
The former Karma enabled badge has been removed. The Overview now puts prompt
routing above machine resource readiness:
  Karma: not armed — no task permission has been granted.
  Karma: armed / routing unverified — task ready; current chat routing unknown.
  Karma: request received — an actual request is inside the Karma provider.
  Karma: action running — the governed action tool is executing.
  Karma: off — the native provider is disabled.
When idle, the status always says current-chat routing is unverified. The stable
VS Code API does not report the focused chat's model/harness to this extension.
An armed task, a saved preference, or a previous request cannot prove that a
new request in a different chat uses Karma.

Each actual provider request receives a unique request ID immediately. Admitted
responses include the backend model, task and protocol invocation ID. Dashboard
and output-channel records distinguish received, admitted, withheld, blocked,
cancelled and stopped requests. Last-request evidence is labelled historical;
it is not an indicator for other chat tabs. Receipt state clears on reload,
while the durable protocol ledger is retained. No prompt content is added to
the routing-status log. Admission is protocol acceptance, not semantic proof.

The STP kernel and all Python harness files are unchanged from 0.12.1.
Tested on VS Code 1.146.0 on this Mac. The extension requires the Local chat
command to exist and fails clearly if it is unavailable. Copilot's separate
Agent Host picker is not an interceptor target for this provider.

PREVIOUS RELEASE NOTES (historical command names and behavior follow)

Karma 0.12.1 — select and remember Karma Auto from native chat

Run Developer: Reload Window, then Karma: Use Karma Auto in Chat.
The same action is available in Karma Settings. It opens a Local chat on builds
that have a harness selector, selects Karma Auto, enables its action tool, and
saves chat.defaultModel = karma-auto. It submits no prompt and grants no task
permissions. Run Karma: Arm Native Chat before sending work.

The model entries now have distinct IDs: karma-auto, karma-low, karma-mid and
karma-high. They cannot collide with Copilot's generic auto model when choosing
a default. Selecting a model in an existing chat remains a per-chat choice; the
saved default controls new chats. Older chats can retain their own model.

If the prompt picker shows only an Ollama tab, selecting an underlying Ollama
model routes directly to that provider. Select Karma Auto for governed routing.
The karma.models low/mid/high settings configure Karma's backends; they do not
select Karma as VS Code's chat provider. These choices are preserved by this fix.
Reload still disarms the task. Persisting a model choice never persists arming.
The STP kernel and Python harness are unchanged from 0.12.0.

Karma 0.12.0 — Karma Auto in native VS Code chat

GET STARTED
1. Install the VSIX and run Developer: Reload Window.
2. Run Karma: Configure Local Models once. Pick your installed low/mid/high models
   in Settings; existing choices are preserved.
3. Run Karma: Arm Native Chat (or click “Karma enabled” in the status bar).
   Choose the current task, a new read-only review, or a scoped Python coding task.
   For coding, select existing source/test files and permitted unittest targets.
   Confirm the displayed scope and permissions.
4. In native VS Code Agent chat, choose Karma Auto in the model picker and enable
   “Apply a governed Karma action” in the tool picker for edits/tests. No @karma
   mention is needed. Models are discovered automatically after reload. If your
   build separates chat harnesses, choose Local, then Karma Auto.
5. Ask normally. Karma selects fresh source pages, routes to a configured local
   model, validates the response, and returns either an answer or its action tool.
   Review the proposed diff/test target before allowing an action. VS Code's tool
   confirmation policy applies; programmatic calls without a chat token receive
   an additional host confirmation dialog.

STATUS AND SWITCHING
The status bar shows Karma off / enabled / armed / working. Click an armed status
bar item to disarm. Settings provides Enable/disable, Arm and Disarm controls.
Disarming cancels active work and revokes pending tickets. Reloading disarms;
re-arm the current task to continue its preserved ledger and spent allocations.

Switch Karma Low, Karma Mid or Karma High in the native model picker to force a
configured tier for the next turn. Select Karma Auto to resume heuristic routing.
Wait for the active turn/action to finish before switching. Task identity, contract,
calibration binding, fresh source and host execution receipts survive the switch.
Routing is a deterministic complexity heuristic, not a model-quality guarantee.

BOUNDARIES OF THIS VERSION
The native Agent UI belongs to VS Code and can require its own sign-in/setup even
with a local model. In the isolated signed-out VS Code 1.146 profile, ordinary chat
submission stopped at the GitHub sign-in gate before reaching Karma. The real VS
Code provider/action API handoff was tested successfully. This extension does not
bypass VS Code authentication or govern its unrelated MCP startup/setup actions.

Karma governs requests sent to its four model entries and its own action tool.
It does not intercept other providers, extensions, shell commands or manual edits.
The native API supplies no stable conversation ID: there is ONE armed task per
workspace, shared by its native chat windows. A new chat window does not reset the
task. Use Arm Native Chat to explicitly start a new task or change approved scope.

Coding currently supports 1–8 existing Python files, with up to four fresh pages
selected per invocation. Exact edits work on macOS/Linux; sandboxed unittest runs
are macOS-only. Windows supports read-only review. Arbitrary shell commands, npm
scripts, new files and unrestricted repository editing are not implemented here.
Read-only reviews retain the existing automatic repository discovery/paging path.

Only the latest text request (up to 1,800 characters) is interpreted. Chat history,
attachments, other tools' output and workspace instruction files do not become
contract authority. Use the armed scope for source. The local model receives a
fresh system message and bounded packet with task, constraints, selected source,
validated working notes when current, and host execution receipts. No conversation
replay or persistent source cache is used. Save dirty editors before a native turn.

The local coding packet has a 16,384-token context allocation, 2,048-token output
reserve and conservative UTF-8 byte charging. It fails closed if the packet does
not fit. The native API token counter is a byte upper bound, not an exact tokenizer.
The outer native chat envelope can be larger because it is not replayed locally.
Task calls, conservative token charges, delivered source bytes and operation time
accumulate across models and are admitted by the unchanged STP kernel. Native
model operations count against the task operation allocation; host actions have
an independent max_actions allocation. Test execution time is bounded separately.
The defaults are allocations, not a claim that every model can finish any task.

Model output cannot widen scope, reset budgets, change calibration or authorize
completion. The action ticket is single-use, expires after five minutes, and is
bound to the armed session. Execution rechecks contract, source preimage, syntax,
permissions, available resources and action budget. A changed contract/calibration
requires re-arming. A test exit code of zero remains an observation: test code can
forge its own output, so Karma records assertions_verified=false and complete=false.

The canonical STP kernel is unchanged (SHA-256):
9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac

The existing Karma panel remains available for overview, settings, receipts,
calibration and advanced tools. @karma is retained for compatible Local chat.

PREVIOUS RELEASE NOTES

Karma 0.11.0 — a clearer dashboard and settings

Run Developer: Reload Window, then Karma: Open Local Harness.

Overview: CPU/GPU temperature summaries, whole-machine CPU usage, available memory,
plain-language resource readiness, current task permissions, budget bars and context
usage. Individual sensor zones and raw JSON are collapsed under Troubleshooting.
The Settings view offers local-model dropdowns, history allocation and an optional
Python executable field. No JSON editing is required for these preferences.

Chat: the same governed local conversation and preserved task context.
Tools: existing scope, editing, testing and architecture workflows. Additional task
limits now use labelled fields. Existing constraints are retained when fields are
left blank, so an update cannot silently remove a previously pinned constraint.

@karma /models opens Settings. @karma /dashboard opens Overview. The last view is
remembered by the panel. Saved model choices are still preserved until changed.
Runtime settings apply to later requests; saving them does not create a task or
invoke a model. Raw receipts remain available for debugging.

Backend, native temperature collector, model routing and STP kernel are unchanged.
Temperature readings remain advisory; allocation bars display measured use without
introducing additional limits or changing kernel decisions.

Karma 0.10.1 — chat entry and model-picker fixes

In VS Code Local chat, @karma hello and @karma /help show guidance and buttons
without starting inference or changing the active task.

@karma /models opens the actual installed-model dropdowns inside Karma. VS Code's
Settings editor displays the saved object values; it is not the discovered-model
picker. Existing saved choices are preserved. Choose Use discovered defaults to
populate the draft dropdowns, then Save choices to apply them. Refreshing does not
replace saved choices. The first-open picker waits for the webview to be ready.

An existing editing or older architecture task stays active under Advanced controls.
A normal review prompt now offers Open current task controls and Start separate
review without a duplicate error. The latter asks you for the new goal. You can also
send @karma /new Review this codebase and explain its architecture. A new task has
read-only workspace scope and new allocations; it is not a continuation or migration
of the old contract. The old task's ledger/history remains on disk. It is not deleted.

The panel shows a banner for older tasks and a direct button to their controls.
@karma /dashboard opens live measurements; @karma /status reports CPU/GPU maxima
and OS thermal pressure in chat. Actual errors are shown once by VS Code.

No STP kernel, adapter, task contract or temperature collection logic changed in
this patch. Temperature limitations and model selection heuristics remain as below.

Karma 0.10.0 — live CPU/GPU temperatures and installed-model defaults

Open Karma after reloading VS Code. The three model tiers are populated from the
local Ollama /api/tags and /api/show metadata. Empty tiers receive editable defaults:
smallest eligible chat model for low, MoE (or the middle size) for mid, and largest
dense model for high. Existing nonempty choices are preserved, including missing
models so that a model removal cannot silently reroute a saved choice.

The Model settings section provides three prefilled dropdowns, Save choices and
Refresh installed models. Autocomplete/base models, embedding models, cloud models
and models whose metadata could not be read are excluded from tier defaults.
No model is loaded for inference or downloaded by discovery. These suggestions
are resource/capability heuristics, not a quality benchmark. On the tested machine
this selects qwen3:4b, qwen3.6:35b and local-assistant:latest.

ACTUAL CPU/GPU TEMPERATURES
The read-only native helper now reads AppleSMC float sensors once per second.
The profile is specifically verified for Mac17,13 / Apple M5 Pro. The dashboard
shows the maximum currently readable mapped CPU and GPU sensor temperatures,
mean values, readable sensor counts and missing-key counts. Expand CPU/GPU
 temperature sensors for individual keys and readings. The measured CPU set has
18 readings; 7 of 8 mapped GPU keys were readable on the tested host. Missing
keys never become zero-degree readings and are never filled from old samples.

These are sensor-zone maxima, not a single die temperature or a claim that each
sensor represents one physical core. AppleSMC is an undocumented hardware interface;
other hardware or an incompatible OS/driver reports unavailable rather than guessed
CPU/GPU data. Access uses the current user account; no privileged helper, root
service, fan-control path or firmware write is installed. The only driver commands
in the collector are read-key-info (9) and read-key-bytes (5).

The adapter rejects wrong profiles, unsupported machines, unknown/duplicate keys,
nonfinite values, invalid types, samples older than 8 seconds, future timestamps,
and readings outside the 0..150 Celsius validity window. Failed groups clear
independently; the frontend also clears old readings when dashboard updates stop.
This validity range is data validation, not a calibrated safe-temperature threshold.

Celsius is advisory. Existing OS thermal pressure and memory gates still make the
same resource decisions through the unchanged STP kernel. No learned thresholds
or new temperature-triggered FIN conditions are introduced. Kernel SHA-256:
9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac

Sensor key reference: https://github.com/exelban/stats/blob/master/Modules/Sensors/values.swift
AppleSMC protocol reference: https://github.com/beltex/SMCKit/blob/master/SMCKit/SMC.swift
Protocol attribution/license: vendor/smckit/LICENSE.txt. The collector implements
only read operations. Its native binary remains verified by runtime-integrity.json.

Build on the supported Mac:
  clang -O2 -fobjc-arc -framework Foundation -framework IOKit harness/telemetry.m -o bin/telemetry
After rebuilding, update the bin/telemetry hash in runtime-integrity.json before
packaging. Test the native ABI/decoder without hardware access with:
  clang -O2 -fobjc-arc -framework Foundation -framework IOKit test/native-temperature.m -o /tmp/karma-native-temperature-test
  /tmp/karma-native-temperature-test
  node test/model-selection.cjs

Upgrade preserves task contracts and allocations. As with previous policy-source
updates, old model notes/proposals may be withheld until a fresh invocation.
The sections below document earlier releases; the model setup and temperature
behavior above supersede their earlier limitations.

Karma 0.9.0 — one prompt, three local model tiers

QUICK START
Open Karma from the status bar or run Karma: Open Local Harness. Choose Configure
models once: low for focused questions, mid for cross-file work, high for demanding
reviews. All three must be installed Ollama chat models; the same model can serve
more than one tier. Configuration is saved in the VS Code user setting karma.models.
No model downloads, cloud inference, API keys or monetary charges are added.

Ask about the open workspace and press Send (Cmd/Ctrl+Enter). Source discovery,
paging and model selection run through Karma. Follow-up prompts preserve the
existing task, constraints, calibration bindings and spent allocation. New task
creates a separate read-only workspace review. Existing advanced tasks continue
through Advanced controls until you explicitly start a new chat task.

The simple workflow reviews code; exact code edits and sandboxed test execution
remain available under Advanced controls with their existing explicit contracts.
The first chat task uses read-only workspace scope and the established review
allocations. A prompt is a question within that contract, not permission to change
scope, run a shell, access the network or install dependencies. Set narrower scope
or other explicit constraints through Advanced controls before starting work.

@KARMA IN VS CODE CHAT
The same backend is registered as @karma via the Chat Participant API:
  @karma Explain the current codebase architecture.
  @karma /low Explain this function.
  @karma /mid Compare the dependencies across modules.
  @karma /high Review concurrency risks.
  @karma /new Review another task.
  @karma /models
  @karma /status
VS Code supplies the chat UI; Karma selects and calls local Ollama models directly.
The participant does not use req.model, VS Code model inference, or replay chat
history. Only the current prompt, pinned task, fresh excerpts, source facts and a
compact admitted working note reach the local model.

Compatibility: Microsoft's current documentation marks chat participants deprecated
and supported only in its Local harness, not the Copilot harness. The simple Karma
panel is independent of that chat integration and invokes exactly the same backend.
See https://code.visualstudio.com/api/extension-guides/ai/chat

ROUTING AND SCOPE
local-tier-routing-v1 is a transparent heuristic: focused requests use low;
cross-file/detail requests use mid; architecture/security/audit/concurrency and
other broad review requests use high. A per-turn selector or /low /mid /high can
override it. Tiers are user-defined compute/capability choices, not inferred prices.
The rule is not a calibrated semantic complexity estimator. Its input signals,
model mapping, chosen model, reason, request hash and source receipt are committed
before invocation. The response cannot change them or authorize another provider.
No automatic escalation, unconfigured fallback, or retries outside existing budgets.
Every attempt goes through InvocationGateway, role validation, resource admission,
versioned calibration bindings and bounded repairs. The STP kernel is unchanged.

The host maps supported source pages within task allocations. Each response
receives excerpts from a selected relevant fresh page and selected call facts;
it does not semantically review every mapped file. Coverage and excerpt paths are
shown separately. The diagram is generated from the deterministic source map.
Model prose remains unverified commentary, even when its citations match source.
Stopped allocations remain spent across model switches. Stop cancels the bridge;
interrupted operations are reconciled by the existing recovery protocol.

TEMPERATURE DIAGNOSTICS
On Apple Silicon, the native helper polls NSProcessInfo.thermalState once per
second. It reports nominal/fair/serious/critical thermal pressure, not Celsius.
The measured value zero means nominal; it is not a missing measurement.
The previous code deliberately collected no CPU/GPU Celsius sensor. This release
shows the API source, measured thermal label and an explicit Celsius-unavailable
reason in the dashboard. No temperature is fabricated from a pressure enum.
Hardware gating remains based on measured OS thermal/memory pressure and available
memory. The local powermetrics help lists a thermal-pressure sampler, not an SMC
temperature sampler on the tested Mac. Device-specific SMC/HID temperature mapping
is a separate integration and is not included or claimed here. Portable hosts keep
unavailable thermal signals explicit.

The rest of this README documents retained calibration and hardening behavior.

Karma 0.8.0 — two domain adapters, versioned calibration profiles

WHAT CHANGED IN 0.8
Smriti owns context-capacity and page-freshness normalization. Karma owns counted
operations, task allocations, hardware and storage normalization. Both use the
same canonical STP implementation through kernel_boundary.py, which validates
finite numbers but contains no domain policy. kernel.py and kernel_adapter.py
are compatibility aliases, not additional kernels or domain adapters.

The canonical kernel is unchanged, SHA-256:
9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac
Its COMPOUND_FIN check retains priority over ordinary FIN, BRIDGE and ALLOW.
Existing deterministic policy thresholds, scope checks, hash checks, schema
checks, test-evidence restrictions and task allocations are retained. Exact TTL
expiry and integer context-capacity checks remain separate hard checks.

UNCERTAIN-DOMAIN CALIBRATION
An optional profile supplies a versioned measurement interpretation and kernel
tolerance for an uncertain domain. No profile is selected automatically. No
empirical profile ships with this release, and no automatic fitting is claimed.
Calibration requires representative data and held-out validation from the actual
domain and measurement method. The host records declared evidence hashes, sample
counts, procedure, objective and evaluator. It validates their structure, not
the truth of the declarations or the quality of the underlying experiment.

Use the Command Palette:
1. Karma: Import Calibration Profile — choose a JSON profile file.
2. Karma: Select Calibration Profiles — explicitly select versions for the active
   task. Deselect all to clear the selection. Importing v2 does not replace v1.
3. Karma: Evaluate Calibrated Measurement — choose a selected profile and a fresh
   measurement JSON file. Opens a receipt with normalization and kernel decision.
4. Karma: Revoke Calibration Profile — select a version and enter a reason.

The dashboard's Kernel, adapter and receipt details show selected profile hashes,
status, adapter responsibilities and the last uncertain evaluation. The raw debug
snapshot contains the full binding, evidence declaration and receipt validity.
These commands are user-mediated. Agent tools cannot import/select/revoke profiles;
command arguments cannot bypass the file/selection prompts. Public extension API
remains open/getState/version. Other extensions with OS filesystem authority are
outside this application boundary.

PROFILE SCHEMA (all fields required; unknown fields rejected)
schema_version=1; profile_id (lowercase letters/digits/dot/underscore/hyphen,
starting with a letter); version (positive integer, sequential from 1);
adapter (smriti or karma); domain; metric; unit; measurement_method;
producers (1..16 distinct measurement-producer identities);
normalization={method:linear_deviation, offset, scale, input_min, input_max,
sigma_definition}; kernel={k,sigma_clause}; valid_from; valid_until;
max_sample_age_seconds (1..86400);
evidence={kind:empirical|synthetic, dataset_sha256, report_sha256,
calibration_samples, validation_samples, procedure, objective, evaluator};
limitations. Timestamps are Unix seconds. Whole profiles are limited to 16 KB.

omega = (value - offset) / scale, with scale > 0 and
offset <= input_min <= value <= input_max. input_min must be below input_max.
This first schema supports increasing deviation only; no extrapolation, arbitrary
normalizer code, or hidden statistical transformation. sigma is supplied already
normalized and its meaning must be declared in sigma_definition. k is the kernel's
deviation tolerance; it is not a headroom coefficient or variance coupling term.
Both k and sigma_clause must be finite and positive. NaN/Infinity/bools rejected.

MEASUREMENT SCHEMA (all fields required)
domain, metric, unit, measurement_method, producer, value, sigma, observed_at,
source (host_observation|model_estimate|synthetic), evidence_sha256.
Identity/units/method must match the selected profile. Producer must be covered.
Samples must be fresh and in range. source is a declaration, never authority.
Changing models does not change a binding; an uncertain measurement from a new
producer needs a profile that explicitly covers that producer. Ordinary source
analysis does not require a calibrated model score.

Profile records and task selections are append-only and hash-bound. Runs, model
deliveries, invocation/assertion receipts and checkpoints retain the exact selected
versions. Changing selection, expiry or revocation prevents dependent results from
being reused; a change during inference quarantines that response. New versions
never rewrite old ones. Explicit reselection does not change the user contract,
refund spent allocations or replay earlier actions. Historical receipt integrity
can still be verified after a profile expires or is revoked.

Uncertain evaluation is an explicit diagnostic path in this release. Its ALLOW,
BRIDGE, FIN or COMPOUND_FIN is recorded as a domain observation; it neither grants
tool permissions nor proves semantic correctness. It is not automatically wired
to a model's self-reported confidence or completion claim. Deterministic permission,
budget, source and test-evidence checks never read optional calibrated tolerances.

SYNTHETIC EXAMPLES
The companion source ZIP includes examples/make_synthetic_profiles.py. Run:
  python3 examples/make_synthetic_profiles.py /path/to/fixture-output
It generates two explicitly synthetic v1 profiles, valid for one day, and samples
valid for 60 seconds. The example value 20 normalizes to omega=.2 and BRIDGE.
These are protocol fixtures, not statistical calibration. Regeneration changes
timestamps and hashes: use a fresh test workspace or advance the version before
importing changed content under an existing profile ID. Do not deploy these
fixtures as empirical thresholds.

UPGRADING
Install the VSIX using Extensions: Install from VSIX, then reload VS Code. Existing
ledger schemas migrate additively. A new adapter logic_hash withholds older model
notes/proposals; the user contract and spent allocations remain. Rebuild source
maps as needed and continue with a fresh invocation. No installed extension is
silently replaced by the source archive or test scripts.

Retained 0.7 recovery and evidence hardening follows.

WHAT CHANGED IN 0.7
- Project test output is untrusted. Receipts record process exit status and the
  test program's reported results, never assertion proof. Automatic completion
  from unittest output is disabled. Even an exit-zero run may have skipped or
  forged assertions. Old "passed" action receipts display as legacy_unverified.
- On the next bridge request, the exclusive workspace lock allows recovery of
  abandoned operations. Missing model results are quarantined; read-only work
  receives an interrupted receipt. Edits are compared with their committed
  before/after byte hashes: reconciled_before, reconciled_after, or
  interrupted_unknown. Recovery never repeats edits, refunds budgets, or treats
  a byte match as proof of who wrote the file. Unknown outcomes are quarantined;
  inspect current source and prepare a fresh proposal to continue.
- The bridge has a 450-second cooperative deadline (POSIX alarm; per-operation
  timeouts elsewhere) and handles cancellation before the 480-second watchdog. Forced termination is still recoverable
  on the next request. Reports update in open tabs. The dashboard reuses one
  read-only Python worker; polls never overlap or spawn a process per sample. Diagnostic polling does not reconcile work still holding the lock.
- Public extension exports are limited to open, getState and version. Task
  creation commands always prompt; agent tools only use existing tasks. This
  prevents accidental authority escalation through Karma's API, not malicious
  extensions with the same operating-system account and filesystem authority.
- CRLF edits preserve newline bytes. A task may list missing files and shows
  them as missing; creating files is still outside the exact-replacement editor.
- TypeScript 6.0.3 and its declaration libraries are bundled, with license notices.
  The compiler and native telemetry hashes are checked at activation. VS Code's
  private TypeScript installation is no longer a runtime dependency. Intel Macs
  measure free + inactive memory via vm_stat; unavailable measurements are not
  treated as healthy. Intel/Windows/Linux native behavior is not certified here.

STORAGE
Diagnostic call files contain metrics and a request hash, not prompt/response
bodies. They are pruned after 7 days or above 8 MiB. Settled action snapshots are
pruned after 7 days or above 32 MiB. Pending/unknown action snapshots are protected.
Immutable SQLite evidence (including committed source deliveries) is retained.
The workspace allocation defaults to 512 MiB (karma.storageLimitMiB, 64..8192).
The unchanged kernel evaluates storage admission before new work; headroom is
reserved for the next operation and recovery. At the limit, new work stops while
state, diagnostics and recovery remain available. This is an admission allocation,
not an exact filesystem quota: recovery records and SQLite overhead can exceed it.
To archive manually, close Karma/VS Code and preserve the complete workspace
storage directory, including any SQLite WAL files. Do not delete individual
ledger files or reset usage to continue the same task. Archive retained history
before starting fresh storage, or increase the allocation explicitly.

TESTING AND DISTRIBUTION
The VSIX contains runtime modules and bundled dependencies, not tests or
experiment drivers. Tests and reproducible packaging code are in the companion
source ZIP. From its harness directory: python3 -B -m unittest discover -v.
Set KARMA_NODE if Node is not on PATH; the bundled TypeScript is discovered.
macOS sandbox tests and missing optional compiler tests skip explicitly elsewhere.
Host regression: node test/hardening.cjs. Required legacy protocol modules remain
because registered roles and the policy hash import them; unused experiment,
audit, validation and telemetry-build drivers are omitted from the VSIX.

WHOLE-CODEBASE REVIEW
Open a trusted local workspace and run Karma: Review Whole Codebase, or use
Automatic codebase review at the top of the Karma panel. Enter the review goal
and a scope ("." means the workspace). No file selection is required.
Start automatic task, choose a local model, then Review next page or Run pages
within budget. Pause after current page allows a model switch between pages.
The goal, permissions, file queue, source-version receipts, commentary and budget
consumption survive model changes and VS Code restarts. Restart never resumes
inference automatically. #karmaAutoReview continues an existing user-created task.

Karma discovers permitted source and metadata paths on every step. It ranks
source paths by goal words, entry-point names and observed import hints. Ranking
is a retrieval heuristic, not a claim that semantic relevance has been proved.
Source pages are packed by their byte allocation, not a file count. The automatic
path removes the 128-file catalog and eight-file selection caps. Python and
JS/TS source mapping is supported; JSON, Markdown and other formats are cataloged
but not semantically analyzed by this path. Dependencies outside a supplied page
can remain unresolved. Unsupported files and parser failures remain visible.

Each mapped page uses freshly read source and a fresh compiler Program. Previously
mapped source hashes are rechecked before its coverage or report is used. Changed
pages return to the pending queue. New files enter through the refreshed catalog. Opening a saved report also
checks directory membership, so new source files appear as pending immediately.
This is a durable table of contents and progress log, not an analysis/source cache.
Parser graph capacity scales with source-page bytes (with a 16384-item work-unit guard).
Compiler diagnostics are counted in full; displayed diagnostic text is bounded.
Missing external types can produce many diagnostics without erasing unrelated
source relationships. Diagram edges remain host-owned, not model-authored.

TASK BUDGETS AND THE UNCHANGED KERNEL
The expandable Task budget allocations form defines the contract before starting.
Defaults are explicit starting allocations, not learned optimal values:
  repository operations 1000; model calls 40 (including repairs);
  conservative model token charge 250000; source processing 65536 KiB;
  active operation time 1800 seconds; source page allocation 512 KiB.
All settings can be changed before starting a task. They cannot be silently
increased by a model or reset by switching models/restarting VS Code.

The adapter measures usage and the requested next slice. It sends dimensionless
ratios to the unchanged four-decision STP kernel. Sigma is the greatest consumed
fraction across task allocations. Omega is k times the greatest requested-cost /
remaining-allocation ratio. ALLOW admits a fitting slice. BRIDGE asks for a smaller
slice; the model path can reduce source call-site evidence while retaining the
whole contract. FIN withholds a work unit that cannot fit this frame. Exhaustion
produces COMPOUND_FIN before another dispatch. Compound precedence is unchanged.
The run saves the queue and reports remaining work; budget exhaustion never
means the whole codebase has been reviewed. An empty queue is structural coverage
completion, which is distinct from a kernel budget decision or semantic approval.

Token accounting reserves conservative input charge + maximum output for every
call, including failed calls. It is intentionally stricter than actual tokenizer
counts and is not refunded after short output. Source processing counts the
bytes allocated to source analysis (not every repeated freshness-check I/O byte).
Automatic inference uses keep_alive=0 so model residency ends after each call;
this trades reload latency for memory headroom during model changes.
Active time sums repository operations and inference; paused UI time and some
bookkeeping are excluded. Admission reserves up to the adapter's 20-second source
operation / 150-second transport deadline, then uses measured elapsed time after
completion. An interrupted dispatch retains its reservation. Counts, reservations,
source receipts and decisions are durable. The dashboard exposes both current
consumption and the last request's stop decision.

Per-call context/transport limits and parser work-unit guards remain. These bound
one operation; they are not total repository coverage limits. A parser overflow
splits a page and retries a smaller work unit. A single file that cannot fit the
source-page allocation is reported as needing attention. This adapter does not
pretend those limits are semantic measurements supplied by the model.

REPORTING
Open accumulated report shows fresh compiler-resolved module references, admitted
page commentary, remaining files and allocations. Commentary samples a bounded
set of call sites per page. "Mapped" never means every function was semantically
reviewed. The host reports semantic_review_complete=false. A whole-codebase pass
is a structural inventory and source-reference map, not a correctness/security
certification. Scope exclusions include hidden files, dependency/build trees,
symlinks, hardlinks, known credential filenames and unsupported extensions.

UPGRADE
Existing task identity, contract and charged allocations survive this upgrade.
Interrupted operations receive append-only recovery receipts. Missing or old-
adapter catalogs are rebuilt from fresh source using the remaining allocation.
Old model notes and maps are withheld when their adapter hash differs; previously
mapped pages may require fresh analysis. Budgets are never reset by upgrading.
If the catalog cannot be rebuilt within the allocation, the task and warning
remain readable. Model switches within this version keep the same contract.

The original manual source-map and editing workflows remain available below.

Karma Local Harness 0.7.0 — fresh source maps and local explanations

QUICK START
1. Install VS Code and Python 3.9+ on the target machine. Install this VSIX via
   Extensions > ... > Install from VSIX. Reload the window when idle.
2. Install/start Ollama on localhost:11434. Model weights are NOT in the VSIX.
   Pull a model suitable for that machine's RAM. qwen3.6:35b is the optional MoE
   explanation model used on this 64 GB Mac; a smaller installed model can be
   selected instead. No model downloads are automatic.
3. If Python is not found, set the user/machine setting karma.pythonPath to its
   full executable path. Empty uses python on Windows, python3 on macOS/Linux.
4. Open a trusted local folder and run Karma: Open Local Harness.
5. In Architecture review, enter the goal, exact file/directory scope and budget.
   Click Start architecture task. Scope is immutable within that task.
6. In Fresh source map & local explanation, select up to eight source/config
   files, one per line. Include tsconfig.json explicitly to resolve path aliases.
   Click Build fresh map for deterministic analysis with no model call.
7. Choose an installed explanation model and click Explain with local model.
   Choose another model after it finishes, then Explain next batch. The task,
   all constraints and the previous accepted note carry across fresh calls.
   Use Open source map for the full deterministic diagram and batch commentary.

Command Palette alternatives: Karma: Build Fresh Source Map, Karma: Explain
Source Map, Karma: Open Source Map, Karma: Switch Local Model.
Agent tools: #karmaSourceMap and #karmaExplainMap. These require a task created
by the user in Karma. Other agents' ordinary file reads/chat remain outside Karma.
The Runtime dashboard is below the map sections in the Karma panel.

WHAT IS DETERMINISTIC
The host owns every diagram edge. TypeScript's fresh Compiler Program resolves
supported functions, methods, import/default/re-export aliases and immutable
aliases. Python uses a conservative AST binding profile. Recursion is permitted
when the source resolves to itself. Chained calls have distinct call-site IDs.
An edge is a static declaration reference, NOT proof of runtime execution,
reachability, correctness or actual dynamic dispatch. External dependencies and
unsupported/dynamic callees remain unresolved. Diagnostics are preserved.

Only explicitly selected project files and trusted TypeScript standard libraries
are visible to the compiler. It does not run the project, plugins, tsconfig
extends, include globbing, package code or build hooks. Explicit config options
are whitelisted; aliases cannot escape the virtual source root. Missing modules
produce diagnostics without erasing unrelated resolved references.

FRESHNESS AND CONTEXT
Every Build/Explain request rereads all selected files and creates a new compiler
Program. Exact content hashes are checked again after analysis, before/after
inference, and on reuse of a report/checkpoint. Config and compiler/library
versions are included. Unsaved VS Code buffers block map/explanation requests.
A fresh request nonce precedes code facts in the serialized model packet. Karma
keeps historical receipts, not a reusable analysis/source cache. Ollama may still
cache an identical system prefix; its global caching settings are unchanged.

Models receive the current goal, versioned contract, exact constraint echo,
bounded current facts and at most the previous accepted working note. There is
no full conversation replay. A model cannot extend its scope, change constraints,
add diagram edges, claim completion or omit unresolved calls. Up to two repairs
are allowed. Rejected outputs remain quarantined. Source changes invalidate old
notes and reset explanation coverage; accepted earlier facts are not silently
relabelled current. Commentary is explicitly NOT semantically verified.

The canonical four-decision STP kernel is unchanged, including COMPOUND_FIN
priority. SHA-256:
9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac
Mapping consumes one repository tool slot per fresh scan. Explaining also creates
a fresh scan. Each model dispatch uses the unified invocation gateway with role
checks, budget/resource decisions and immutable receipts. The production source
explanation path uses an 8192-token context and a 768-token response reserve.
Batches contain up to four call sites, reduced as needed while retaining the
complete contract. Limits: eight files/configs, 64 KB per file, 128 catalog files,
1024 JS/TS nodes/calls and 256 displayed diagnostics; total diagnostic count is retained. Python retains the smaller AST limits.

PLATFORMS
One VSIX contains the read-only mapping and explanation path for macOS, Windows
and Linux. Python and Ollama are separate installations; no absolute paths from
this Mac are required in the extension settings. The compiler ships in the VSIX.

macOS Apple Silicon: full end-to-end validation performed. Native memory pressure
and OS thermal state are measured. Existing exact Python edits and sandboxed
unittest execution remain available under their separate action contracts.
macOS Intel: uses the portable monitor; not tested on Intel hardware.
Linux: portable memory/CPU monitor; native Linux validation has not been run.
Windows: portable monitor, Windows byte-range locks and handle-based scoped file
reads rejecting reparse points/hardlinks; native Windows validation has not been
run. Read-only architecture/mapping/explanation is the supported feature scope.

Portable monitoring uses measured available memory with a 4 GiB floor and CPU as
advisory. Thermal state, memory pressure and temperature are explicitly unknown;
this profile provides no thermal guard. The same STP kernel evaluates the adapter
measurements. Missing/stale/invalid memory samples block dispatch.

Sandboxed project tests are macOS-only. Windows edits and non-macOS test execution
fail explicitly; there is no unsandboxed fallback. Linux atomic Python edits use
the existing POSIX path but have not been validated on Linux here. For a platform
smoke check, run test_platform_support.py from the companion source ZIP. Full host validation is still required
before treating Windows/Linux behavior as proven.

LEGACY WORKFLOWS
The model-drafted API map / smaller-model independent review remains available
as a separate workflow. It uses the older conservative lexical mapper and may
reuse hash-checked indexes. The fresh source-map path above never reuses analysis.
The earlier free-form architecture diagram remains unverified. Code editing/test
controls are separate and require an explicitly permitted action contract.

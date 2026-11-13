Sutra 0.19.1 — continue workspace requests in the same chat

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

DASHBOARD
Click Sutra Monitor in the status bar, or run Sutra: Open Monitor.
The combined dashboard opens beside Terminal and Output. It shows CPU/GPU
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

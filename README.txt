Sutra 0.18.0 — tokens, system telemetry and kernel status together

Previously Karma. Your installed extension ID and saved settings are preserved.

GET STARTED
Run Sutra: Configure Local Models to select installed Ollama models.
Run Sutra: Open Governed Chat (Sutra Auto). This saves the compatible Local chat
and model for new chats. Existing chats retain their own selected model.
Chat normally. Arm a scoped task only when workspace tools are needed.
Use Sutra: Arm Task and Open Chat for source access, proposed edits and tests.
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
The canonical STP kernel and all harness policy files are unchanged in 0.18.0.
VS Code now receives an offline tokenizer estimate instead of a byte count.
Its compaction threshold remains under VS Code's control. Harness budget charges
and measured Ollama token counts are unchanged. Conversation memory is still lossy.
Conversation has no web tools. Diagrams are conceptual, not factual verification.
Python edits/tests are limited to supported operating-system sandbox adapters.

SOURCE AND TRACKING
https://github.com/dtmohan/Sutra
Build: python3 -B package.py --output dist
See README.md and docs/development.md in the source repository.

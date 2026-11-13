# Changelog

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

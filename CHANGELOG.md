# Changelog

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

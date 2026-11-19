# Independent verification — Sutra 0.50.0

Date: 2026-11-18, 23:26–23:45 PT
Reviewer: QA and UX validation lead
Snapshot: `HEAD f20fa68` + uncommitted 0.50.0 tree (86 dirty paths)
Environment: macOS arm64, VS Code 1.147.0, isolated profiles, three
`sutra:engine/*` profiles, managed Strata runtime on `127.0.0.1:19483`.

This verifies the 0.49.1 resolution register against the current tree. It is
independent of `docs/qa/VALIDATION-0.50.0.md` and uses fresh runs.

## What I ran

| Check | Command | Result |
| --- | --- | --- |
| Standard gates | `scripts/qa.sh` | **22/22 passed** |
| Python suite | `python3 -B -m unittest discover -s harness -p 'test_*.py' -q` | **1,109 OK**, 8 skipped |
| Package | `package.py --output /tmp/sutra-verify-dist` | 281 runtime files |
| Packaged contents | `check-release.py --package-dir /tmp/sutra-verify-dist` | passed, 0.50.0 |
| Consent journey | `test/live-permission-journey.py` | **passed** (5 screenshots) |
| Native engine | `test/live-engine-worker.py` phases low/mid/restart/high/verify | **5/5 passed** |

The consent journey is the project's real-dialog test: a normal isolated VS Code
window, the real `native_provider.js`, `workspace_authorization.js` and
`usage_view.js`, real modals, and a deterministic backend. The engine phases use
the real invocation gateway and the Sutra-owned worker; each phase is a fresh
caller process.

## Finding-by-finding

| ID | Verified behavior | Evidence |
| --- | --- | --- |
| L-01 | The settled response now contains **"Workspace access needed"** with a clickable `[Choose workspace access]` link; a notification action and Monitor button exist. The response explicitly says **"no model call is waiting for consent."** | `native_provider.js:264-271`; journey phase `01`; live test checks |
| L-02 | Dismissing the real modal preserves the saved request and produces **"Workspace access was not granted. The request, context and consumed budget are saved."** — no interruption/“model stopped” message. | journey phase `02`; transcript assert `!text.includes('the model stopped before finishing')` |
| L-03 | Resume no longer calls `workbench.action.chat.open`. It shows the saved request and offers **Copy resume instruction**, explicitly stating the host cannot address that chat directly. No sign-in was initiated. | `native_provider.js:366-379`; journey phase `03` |
| L-04 | Host chat labels remain VS Code-owned; the extension now explains them in settings. I did not re-test full Agent-mode chat (see limits). | code/copy; resolution register |
| L-05 | The pause I hit while the managed runtime was resident was typed: `RESOURCE_WAIT: Cold provider memory estimate and system reserve exceed available memory`. After releasing the idle managed runtime, the same engine phases passed. | engine phase attempt; live run |
| L-06 | Metadata-only stages are no longer printed as conversation body; the consent flow reports a single actionable message. | `native_provider.js:264-271`; screenshots |
| L-07 | One label family: **Workspace access** in command (`Sutra: Workspace Access…`), modal (`Workspace access for Sutra`), chat, status and Monitor. | `package.json`; journey screenshots |
| L-08 | Task picker labels **Current request** / **Parked request**; Monitor shows state and identity. | `native_provider.js:317` |
| E-07 | `check-release.py` now asserts every `runtime_files()` member, rejects duplicates and unexpected entries. | `scripts/check-release.py:43-52`; `test_release_check.py` |
| E-08 | `CHANGELOG.md` starts with `# Changelog`, current release first under a single heading. | `CHANGELOG.md:1`; release test |
| E-10…E-20 | Covered by the 22/22 standard gates and 1,109 Python tests (auth framing, socket cleanup, registry modes, profile state, drafts, progress validation, hash scope, docs, codec isolation). | gate output |
| QF-24 / QF-33 | Diagram theme and `/new` help fixes are in the tree and their gates pass. | browser/release tests |

## Real engine run (after releasing the competing runtime)

The managed Strata runtime was idle (no client connections) but resident with
20 GiB of weights, so the owned-engine cold load correctly paused. I stopped it
with `strata runtime stop --profile sutra-high --wait 30`, ran the phases, then
restarted it with `strata runtime start --profile sutra-high --wait 300` and
confirmed `generation_ready`.

| Phase | Seconds | Result |
| --- | ---: | --- |
| low / 16K | 22.283 | passed |
| mid / 24K | 21.773 | passed |
| restart into high | 21.254 | passed |
| high / 32K | 17.730 | passed |
| verify | — | all 9 invariants true |

The verify phase confirmed durable context/constraints/permissions, preserved
consumed budgets, receipt chains, unchanged canonical kernel, Low/Mid/High recall,
owned-worker restart and recovery, fresh process per phase, no Ollama/Strata
Control calls, and profile/instance bindings. All four answers retained
Cedar-724, CED724, `21 − 18 = 3` and the prohibited operations.

## Screenshots

- `screenshots/05-fixed-permission-modal.png` — real modal with Read only / Workspace agent / Cancel and the notification action.
- `screenshots/06-fixed-resume-guidance.png` — dismissal preserved; copy-resume guidance; no sign-in.
- `screenshots/07-fixed-resumed-report.png` — same chat resumed on Mid with the deterministic report.

## Residual limits (not regressions)

1. **Full Agent-mode inference through VS Code chat is still not verified
   end-to-end.** On a fresh logged-out VS Code 1.147 profile the Agent chat can
   show Copilot sign-in before Sutra's provider is invoked. The project documents
   this host boundary; the new resume path no longer initiates it. Consent is
   verified with a deterministic backend, inference separately through the gateway.
2. **Resume requires returning to the original Local Sutra chat**; the host cannot
   target a session URI. This is explicitly explained in the UI.
3. **Native first load can still pause when another managed runtime is resident.**
   The message is now typed and actionable, but the user must release the competing
   runtime or use it instead.
4. I did not run the four Chrome webview suites or the packaged VS Code smoke
   (Playwright/Chromium is not installed for those). The project's own validation
   reports 27/27 with them; I did not reproduce that expanded set.

## Verdict

The 0.49.1 resolution is **verified** for every item that can be tested on this
machine: the permission journey, dismissal semantics, resume guidance, task
labeling, release completeness, changelog structure, and real native-engine
continuity. The standard 0.50.0 gates are green. The remaining items are
documented host limitations and optional browser/packaged gates, not observed
regressions.

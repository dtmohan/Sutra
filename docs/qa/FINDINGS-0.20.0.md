# Findings — Sutra 0.20.0

Date: 2026-11-13
Reviewer: QA and UX validation lead
Scope: `HEAD` working tree (0.20.0, centralized orchestration) as staged in the
repository. Static code review plus the automated suites; no live VS Code + Ollama
run and no browser run in this review (see *Verification limits*).

**Verification key:** ✅ read and confirmed in code · ◐ static review, needs a live
repro to confirm impact · ☐ needs a live run to classify.

## Baseline (what is actually green today)

| Check | Result |
| --- | --- |
| `python3 -B -m unittest discover -s harness -p 'test_*.py'` | **392 tests, OK** (≈32 s) |
| `node test/{native-provider,hardening,panel-entry,model-selection,branding,host-tokens}.cjs` | **6 suites, exit 0** |
| `sha256sum harness/stp_canonical/kernel.py` | `9dfef247…4eac`, matches README and `package.py` |
| `runtime-integrity.json` (121 entries) | all files exist and hashes match |
| Manifest wiring | 29/29 commands registered; 9/9 language-model tools handled |
| Packaging (`package.py`) | builds; excludes `test/`, `docs/`, `.git/`; bridge imports from unpacked VSIX |

These are real strengths. The orchestration tests in particular assert invariants
across process restarts, not just happy paths.

## Findings

### S1 — release blockers

| ID | Area | Finding | Evidence | Status |
| --- | --- | --- | --- | --- |
| QF-01 | First run | With Ollama stopped, model setup has no preflight. The webview opens with an empty catalog, `showModelChoices` returns early, so the built-in "No local chat model available" copy never renders. The bridge's connection failure can surface as a raw Python error. No Retry affordance. | `extension.js:168-171`; `harness/vscode_bridge.py:15-21,468`; `media/panel.js:190-191` | ◐ |
| QF-02 | Trust | The arming dialog always says "Network access and new dependencies remain off", but arming reuses an existing task's contract (`current` → `binding: identity(st)`), and tasks created from the dashboard can set `network_allowed` / `dependencies_allowed`. The dialog can therefore state a guarantee that is false. | `native_provider.js:59` vs `:64-66`; `orchestration.py:99`; `media/panel.html:113` | ✅ |

**Why S1:** QF-01 is the first experience for every user without a running model
server; QF-02 is a false safety statement in the one dialog whose job is to be
trustworthy.

### S2 — high

| ID | Area | Finding | Evidence | Status |
| --- | --- | --- | --- | --- |
| QF-03 | Recovery | Resume works only for `continue|resume|please continue|please resume`. Any other prompt silently cancels the saved flow's unused child reservations. There is no Resume/Discard affordance and no "workflow saved" notice after Stop. | `native_provider.js:225-229` | ✅ |
| QF-04 | Cancellation | `EXECUTING_ACTION` permits only `DONE`. `cancel()` walks `descendants`, which includes the node itself, so cancelling during an executing action raises inside the transaction and rolls the whole cancel back. A long test run may be uncancellable. | `orchestration.py:34-35,295-300,119-123` | ✅ |
| QF-05 | Recovery | A stopped mapping writes `PAUSED`, but no phase handler accepts `PAUSED`; the next step falls through to `raise InvalidUpdate('Workflow needs explicit recovery')`. The flow is wedged unless something else recovers it. | `orchestration_flow.py:167,202` | ✅ |
| QF-06 | Monitor UX | `renderMonitor` runs every 1 s and calls `flow-nodes.replaceChildren()`. The child "Full source scope" `<details>` and keyboard focus are destroyed on each tick; an open disclosure collapses after one second. | `media/usage.js:34,39,92` | ✅ |
| QF-07 | Release gate | The packaged smoke driver asserts API version `0.12.3`, exactly three API keys, and participant name `karma`. The shipped contract is 0.20.0 with `getRoutingStatus` and participant `sutra`, so the only real-VSIX test cannot pass. | `test/index.js:8,10`; `extension.js:509`; `package.json:441` | ✅ |
| QF-08 | Model setup | All four tiers register even when `karma.models` is empty. The picker shows "Configure this tier"; the first send then fails with "The <tier> tier model is unavailable…" and no settings affordance. | `native_provider.js:178`; `model_selection.js:15` | ✅ |
| QF-09 | Permissions UX | A file permission is task-wide (`allowed_paths` persists, and switching models keeps it), but the dialog only says each edit/test gets its own confirmation. Users are not told how long the file scope lasts. | `native_provider.js:82`; `test/native-provider.cjs:152-160` | ✅ |
| QF-10 | Navigation | Clicking the Sutra status bar runs `karma.useNativeChat`, which opens a new Local chat and writes four global `chat.*` settings. A user expecting the dashboard gets a new chat and changed editor defaults. | `extension.js:502`; `native_provider.js:107-122` | ✅ |
| QF-11 | Data loss | Any `state` push overwrites goal, paths, test paths and budgets in the Tools form; a model switch posts `state`. Text typed but not saved is silently replaced. | `media/panel.js:124-127` | ◐ |
| QF-12 | Test grants | A test grant keeps only `test*.py` paths. A selected `tests/support.py` or `foo_test.py` produces an empty runnable set that looks like a successful grant. | `native_provider.js:80` | ✅ |

### S3 — medium

| ID | Area | Finding | Evidence |
| --- | --- | --- | --- |
| QF-13 | Docs | `README.md` still says "Version 0.16.0" while manifest, README.txt, CHANGELOG and UI say 0.20.0. | `README.md:6` |
| QF-14 | Docs | Known limitations still says conversation memory is capped at 1,000 characters; 0.19 replaced that with a budget-derived limit. | `docs/known-limitations.md:14`; `conversation_memory.py:83` |
| QF-15 | A11y | Diagram SVG has no `role="img"`/accessible name; the only alternative is Mermaid source in a collapsed disclosure. | `media/diagram.js:10`; `media/diagram.html:3` |
| QF-16 | A11y | Token bars encode series by color only; counts are `title`-only on non-focusable elements. One invalid count shifts the surviving bar into the other series' slot. | `media/usage.js:76-77` |
| QF-17 | A11y | Live telemetry (`#overview-status`, `#resource`, `#thermal`, `#activity`) has no live region, so screen readers never hear freshness or state changes. | `media/panel.html:28`; `media/usage.html:12` |
| QF-18 | Freshness | The panel renders temperatures when `available` is true without the `age_seconds` check the usage view applies, so an expired reading can look live. | `media/panel.js:171-174` vs `media/usage.js:20` |
| QF-19 | States | The panel always says "No model calls match these filters", even when there are no calls at all; calls/unmeasured default to `0` before data exists. | `media/panel.js:229`; `media/panel.html:56-57` |
| QF-20 | CSP | Two inline `style="overflow-wrap:anywhere"` attributes are blocked by `style-src 'nonce-…'`; long IDs can overflow. | `media/panel.html:27,65` |
| QF-21 | A11y | Tabs are pressed buttons without `aria-controls`/`role=tabpanel` or arrow-key behavior; view changes do not move focus. | `media/panel.html:14`; `media/panel.js:181-185` |
| QF-22 | Terminology | Raw kernel codes (`ALLOW/BRIDGE/FIN/COMPOUND_FIN`), `ω σ K`, "Smriti", "kernel" reach the UI without an in-product legend; "Open Sutra Monitor" / "Open combined monitor" / "Advanced dashboard" name one destination. | `media/usage.js:6,29`; `media/panel.js:80`; `media/panel.html:27,44` |
| QF-23 | Drift | Two token-UI implementations (`panel.js` vs `usage.js`) already differ (timeline, empty state, table width, color classes). | `media/panel.js:227-253`; `media/usage.js:62-88` |
| QF-24 | Theme | The diagram forces a light palette (`#f6f8fc`, light Mermaid theme), producing a white slab in dark themes. | `media/diagram.css:1`; `media/diagram.js:3` |
| QF-25 | Drivers | Four host drivers hardcode obsolete API versions and would fail against 0.20.0. | `test/native-vscode.js:7`; `test/visual-vscode.js:3`; `test/selection-vscode.js:7`; `test/compaction-vscode.js:7` |
| QF-26 | Packaging | `THIRD_PARTY_NOTICES.md` is not shipped although the README points installed users to it. | `README.md:129`; `package.py:25` |
| QF-27 | Infra | No CI, no single runner, no lint/format config, no version-consistency check, no tag for 0.20.0. | `.github/` absent; `package.json` has no scripts |
| QF-28 | Coverage | New orchestration branches are untested: illegal-transition guard, `PAUSED` recovery, child NACK→quarantine, action-second charges, receipt-committed-but-node-not-DONE crash window, double settlement, corrupt-chain detection, second concurrent workflow, `start_existing`/`inspect`. | `orchestration.py:60,72,136`; `orchestration_flow.py:101-103,193`; `subagent_runtime.py:97-103` |

### S4 — low

| ID | Area | Finding |
| --- | --- | --- |
| QF-29 | UI | CPU sparkline uses `120/59` rather than `120/(n-1)`, squashing partial histories (`media/panel.js:71`). |
| QF-30 | UI | Tables have `th` cells but no `thead`/`scope`; the sensor table has no caption (`media/panel.js:86,169-176`). |
| QF-31 | A11y | Disabled controls use `opacity:.45` (≈2.8:1) instead of `--vscode-disabledForeground` (`media/panel.html:7`). |
| QF-32 | Perf | Render intervals keep running while the view is hidden (`media/panel.js:179`; `media/usage.js:92`). |
| QF-33 | Copy | "Arm Workspace Access" vs "Arm Sutra" vs "Arm task in Sutra"; help text says "/new followed by a review request" while `/new` alone opens a prompt. |

## What is solid

- **Kernel identity and integrity:** kernel hash pinned and matching; 121 runtime
  hashes verified at activation; VSIX excludes tests and docs.
- **No webview XSS found:** all model and telemetry text uses `textContent`; Mermaid
  is attribute-screened and re-validated host-side; CSP has no `unsafe-inline`
  script.
- **Orchestration invariants have real tests:** sibling overspend, no refund of
  failed calls, stale source blocking a join, contract change invalidating
  children, committed fork/join recovery, forged child context rejection.
- **Honest labeling in places:** "armed · routing unverified", stale-snapshot
  labels, "unknown" emission counts, and a separate resource-ALLOW explanation.

## Verification limits of this review

- No live VS Code host run and no Ollama; QF-01, QF-11 are code-level findings and
  need a repro on the exact VS Code version.
- Playwright is not installed, so `test/*-browser.cjs` did not run; QF-06 is
  confirmed from the code path (1 s interval → `replaceChildren`), not observed.
- Windows/Linux behavior was not exercised; platform claims were checked by reading
  the adapters only.

## Recommended order

1. **QF-01, QF-02** — fix before any external install. These are the first-run and
   trust surfaces.
2. **QF-07** — replace the stale smoke driver with a version-agnostic one; without
   it there is no release artifact gate.
3. **QF-03, QF-04, QF-05** — recovery and cancellation: add Resume/Discard UI, a
   cancellable executing state, and a `PAUSED` exit.
4. **QF-06, QF-11, QF-18** — stop render churn and silent overwrites; share one
   freshness predicate.
5. **QF-08, QF-09, QF-12** — model and permission copy that matches what is granted.
6. **QF-28** — add the missing invariant tests, starting with the crash window
   (receipt committed, node not `DONE`) and double settlement.
7. **QF-13, QF-14, QF-26, QF-27** — documentation, notices and a CI runner.

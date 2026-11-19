# Findings — Sutra 0.49.0 working tree

Date: 2026-11-18
Reviewer: QA and UX validation lead
Snapshot: `HEAD fa245d7` + uncommitted working tree (version 0.49.0)
Method: gate execution, static review, targeted repros, packaging build.

> **The tree moved during this review.** At 14:29 PT it was 0.48.0 with a stale
> integrity manifest and missing engine files; by 14:53 PT it was 0.49.0 with a
> regenerated manifest and all engine files shipped. Findings below are
> timestamped accordingly. Re-run the gate after any further edit.

## Gate results (final snapshot, 14:53–15:00 PT)

| Gate | Result |
| --- | --- |
| `scripts/qa.sh` (22 gates) | **22/22 passed** (second run) |
| First run at 14:53 | 21/22 — `workspace-authorization-store` failed once, then passed isolated and on rerun |
| Python suite | **1012 tests OK**, 7 skipped (~90 s) |
| `python3 package.py --output dist` | VSIX 4.97 MB, 278 runtime files |
| `check-release.py --package-dir dist` | passed; kernel unchanged |
| Integrity manifest | 276/276 entries match (was stale earlier) |
| VSIX contents | `engine_service.py`, `engine_process.py`, `engine_codec.py`, `engine_control.py`, `vendor/sutra_engine/` all present |

This is a large improvement over 0.20.0: the project now has 22 gates, a CI
workflow, a release checker, an explicit integrity regeneration script, browser
suites and a packaged-VS Code smoke gate.

## Resolved during the review

These were real at 14:29 and were fixed by concurrent edits; kept here so the
history is not lost.

| ID | Finding at 14:29 | Disposition |
| --- | --- | --- |
| R-01 | `runtime_files()` omitted `engine_service.py`, `engine_process.py`, `engine_codec.py`, `engine_control.py`, `engine_management.js`, `requirements-engine.txt`. `engine_client.py` spawns the service by path, so a packaged VSIX could not run native tiers. | Fixed at 14:46 in `harness/governance.py` `POLICY_FILES` and `package.py`; verified present in the built VSIX. |
| R-02 | `runtime-integrity.json` stale (11–18 hash mismatches, 7–13 unlisted files). Activation failed closed; `check-release`, `hardening` and `runtime-integrity` gates failed. | Regenerated with `scripts/runtime-integrity.py --write`; now 276/276 match. |
| R-03 | `test_strata_model.test_reserved_provider_name_cannot_become_an_ollama_default` read the real user registry (`~/Library/Application Support/Sutra/Engine/models.json`) through `combined()`. Failed on this machine, would pass on clean CI. | `model_catalog.combined()` now appends engine rows only when `SUTRA_ENGINE_*` is present; test passes isolated. Residual risk in E-20. |
| R-04 | Version strings were mid-flight between 0.48.0 and 0.49.0. | 0.49.0 aligned across `package.json`, `README.md`, `README.txt`, `CHANGELOG.md`, `media/panel.html`. |

## Live session findings (2026-11-18, VS Code 1.147.0)

Full log, screenshots and repro: [LIVE-0.49.0-native-session.md](LIVE-0.49.0-native-session.md).

The native engine worked (a real `sutra:engine/engine-low` call, 5,230 in / 99 out,
2 calls, 14 s), but the governed workspace journey dead-ended:

| ID | Sev | Confusion |
| --- | --- | --- |
| L-01 | **S1** | Chat says "Choose Sutra workspace permissions" while no dialog or button exists; the palette command **Sutra: Workspace Permissions…** silently does nothing during an active request. |
| L-02 | **S1** | Clicking/dismissing the permission popup aborts the request with "Response interrupted: the model stopped before finishing. Try again." — a model-failure message for a permission decision, with the wrong recovery. |
| L-03 | **S1** | **Resume saved request** opens the Copilot chat and GitHub sign-in instead of the originating Sutra chat. |
| L-04 | S2 | Copilot branding and sign-in prompts on a local-only flow whose model is Sutra Auto. |
| L-05 | S2 | `RESOURCE_WAIT` says memory/thermal pressure while the Monitor reports ALLOW, Normal and 26.1 GiB available. |
| L-06 | S2 | Host stage lines ("call details", "Reasoning control: off") appear in the conversation. |
| L-07 | S3 | Four names for the permission action across chat, status bar, command and modal. |
| L-08 | S3 | `Open tasks (2)` for one user request; Resume target ambiguous. |

## Open findings

### S2 — high

| ID | Area | Finding | Evidence |
| --- | --- | --- | --- |
| E-01 | Engine trust | The engine registry accepts any absolute executable, args and allowed-prefix env. `profile_hash` is an **unkeyed** SHA-256 of the descriptor, and artifact bindings are metadata (size/mtime/dev/inode), so a same-user process can repoint a profile at another binary without changing a cryptographic identity. The only remaining barrier is the user assigning that profile (`engine_model.py:59`); display text is attacker-controlled. The env allowlist in `normalize()` is not enforced where `EngineProcess` builds the environment. | `engine_profiles.py:107-176,172,69-70,128-131`; `engine_process.py:163-170`; `engine_model.py:59` |
| E-02 | Worker lifecycle | A stale or wedged worker is not revalidated by PID/owner before a new child is spawned. The new child can die on `owner.lock` while the old worker keeps model RAM and the socket directory; the startup loop can spend ~210 s before a generic "exited during startup". | `engine_client.py:161-165,191-202`; `engine_service.py:245` |
| E-03 | Startup UX | "Start the selected native profile when VS Code opens" is inaccurate: startup picks the **first fixed tier with a native profile** (Low before Mid/High), can overwrite the manager-selected `startup.json`, and also fires on settings changes, not just window open. | `extension.js:59-82,879` |
| E-04 | Readiness truth | The settings "Loaded" pill trusts the native CLI `state.state` only; it never verifies `/v1/status` + telemetry identity the way catalog/activation do. Settings can say Loaded while the first chat activation fails. | `strata_management.js:180-195`; `media/panel.js:392` |
| E-05 | Error mapping | Any activation `paused`/`busy`/`timeout` becomes a memory `ResourceWait`, and chat guidance points at System pressure / Last model allocation. A failed profile or contended slot sends the user to the wrong remedy. | `harness/strata_model.py:70-78`; `native_provider.js:409-424` |
| E-06 | Loading state | `publishStrata()` republishes the old state with `busy:true`; the panel clears the pending text and repaints `error||message`, erasing "Checking resources and loading…". | `extension.js:279-284`; `media/panel.js:381-406` |
| E-07 | Release gate | `check-release.py` compares archive entries to the tree but never asserts that every `runtime_files()` member is present. A future packaging omission would pass "packaged contents" and fail at activation. | `scripts/check-release.py:39-45` |
| E-08 | Release metadata | `CHANGELOG.md` places `## 0.49.0` (line 1) and `## 0.48.0` (line 16) **above** the `# Changelog` heading (line 23), and `check-release.py` only tests for the substring. | `CHANGELOG.md:1,16,23`; `scripts/check-release.py:25` |

### S3 — medium

| ID | Area | Finding | Evidence |
| --- | --- | --- | --- |
| E-09 | Flake | `workspace-authorization-store` failed once under load, passed isolated and on rerun. Its only wait is a 5 s revocation observation. | `test/workspace-authorization-store.cjs:93` |
| E-10 | Frame auth | Frames carry a static per-worker bearer token, no HMAC/nonce/sequence; `hmac` is imported but unused in the client. A same-uid reader of `worker.json` can replay calls. | `engine_client.py:3,113`; `engine_service.py:291-293` |
| E-11 | Socket dir | `/tmp/sutra-engine-<uid>-<hash>` is predictable, never cleaned up, and can be pre-created by another local user to cause permanent refusal. | `engine_client.py:38-48`; `engine_service.py:334-347` |
| E-12 | Registry read | Writes use 0600 + atomic replace, but `read()` never checks owner/mode, so a group/world-writable registry is accepted. | `engine_profiles.py:179-202` |
| E-13 | Profile states | Per-profile `state` is discovered but not shown; a `failed` profile still reads "loads on demand" and stays selectable. Add/Locate/Setup do not refresh tier dropdowns. | `strata_management.js:189-191`; `media/panel.js:251-255`; `extension.js:398-411` |
| E-14 | Draft loss | Tier selects are not dirty-tracked; state pushes reset unsaved choices, and `save_model_tiers` publishes discovery before validation, so a failed save wipes the replacement choice. | `media/panel.js:244-269,488-492`; `extension.js:655-658` |
| E-15 | Copy | Tier cards say "a smaller/larger model" while the README states one weight set and profiles differ by context. | `media/panel.html:71`; `README.md:8-9` |
| E-16 | macOS gate | "Locate" stays enabled on non-macOS inside a collapsed section; the platform error appears only after clicking. | `media/panel.html:96-99`; `media/panel.js:410`; `extension.js:360-366` |
| E-17 | Frame/progress | `progress` frame payloads are not validated before `activity.emit(**progress)`; a non-dict aborts a healthy generation. Timeout settlement trusts worker-reported usage (upper-bounded only). | `engine_client.py:137-139,96-99`; `engine_model.py:113,145-159` |
| E-18 | Hash scope | `runtime_hash` hashes every harness `.py/.js/.json` on each generate; an unrelated harness edit can unload a resident model mid-session. | `engine_client.py:51-75` |
| E-19 | Docs | `docs/native-tier-validation.md` still describes 0.48.0 and claims 922 Python tests; the suite is now 1012. | `docs/native-tier-validation.md:1,19` |
| E-20 | Test isolation | `engine_model.append_catalog()` has no `path` parameter and defaults to the user registry; only the env-absent branch is tested. `test_engine_codec.InstalledPackTests` read the real Strata pack. | `engine_model.py:169-179`; `harness/test_engine_model.py:153-157`; `harness/test_engine_codec.py:11` |

### S4 — low

- No tags for 0.47.0–0.49.0; `HEAD` is still the 0.47.0 commit.
- `docs/development.md` lists seven Node suites; `scripts/qa.sh` now runs 22 gates and has `--browser`/`--vscode` flags.
- Old findings still open: QF-17 (only one live region on the panel), QF-22 (three names for the monitor), QF-23 (two token renderers), QF-24 (forced light diagram theme), QF-33 (`/new` help text).

## Closure of the 0.20.0 register

Of the 33 findings in `FINDINGS-0.20.0.md`: **26 fixed**, **5 open** (QF-17, 22,
23, 24, 33), **1 changed** (QF-27 — CI and one runner now exist, still no
lint/format gate), **1 fixed without a regression test** (QF-10, status-bar
target). The S1 blockers QF-01 (first-run dead end) and QF-02 (false arming
guarantee) are both fixed, with tests/copy.

## Release checklist for 0.49.0

1. **Fix L-01/L-02/L-03** (permission affordance, dismissal semantics, resume
   targeting) or block the governed workspace journey in the release notes.
2. Freeze edits; confirm `git status` no longer changes.
3. `python3 -B scripts/runtime-integrity.py --write` **last**, after the final edit.
4. `scripts/qa.sh --with-package` twice; both runs 22/22 (the first run's flake is E-09).
5. Move the 0.49.0/0.48.0 entries below `# Changelog` (E-08).
6. Update `docs/native-tier-validation.md` to 0.49.0 and the real test count (E-19).
7. Commit, tag `v0.49.0`.
8. Optionally fix E-07 (assert runtime-file completeness) before tagging.

## Verification limits

- No live VS Code host run (Ollama or native engine) and no Playwright run in
  this review. Browser suites and the `--vscode` packaged smoke remain optional
  and unexecuted here.
- The tree changed repeatedly during the review; figures are for the 14:53–15:00 PT
  snapshot only.

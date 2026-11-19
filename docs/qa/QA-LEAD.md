# QA and UX validation — charter

Owner: QA and UX validation lead
Status: active
Applies to: Sutra VSIX (`package.json` version), repository root

This charter defines what "validated" means for Sutra. It is deliberately
independent of feature work: QA does not implement the behavior it certifies.

## 1. Mission

Provide independent evidence that:

1. **Governance claims hold.** Model output cannot create or widen permissions,
   replay a step, double-charge a budget, or promote stale or foreign evidence.
2. **The UI tells the truth.** Every state, badge, dialog and error describes what
   the host will actually do, and failures are distinguishable from success.

A green suite is evidence, not proof. Pass criteria below say what each gate
demonstrates and what it still leaves unverified.

## 2. What we validate

| Dimension | Question | Primary evidence |
| --- | --- | --- |
| Governance integrity | Can a model, replay, or crash widen scope, duplicate a commitment, or promote quarantined evidence? | `harness/` invariant tests, adversarial cases, ledger inspection |
| Functional correctness | Do the tier routing, review, edit/test and monitor surfaces do what the manifest promises? | Node suites, VS Code host drivers |
| Recoverability | After interrupt, reload, model switch or quarantine, is state reachable and visibly explained? | Live drivers, replay tests, manual journey sheet |
| UX clarity and trust | Does copy match behavior? Are unknown, zero, stale and empty distinguishable? No false guarantees. | Copy audit against code paths, journey reviews |
| Accessibility | Keyboard-only, screen-reader and low-vision use of dashboard, monitor and chat. | Static audit + browser checks |
| Freshness and performance | Are telemetry and snapshots labelled stale? Does polling destroy focus or burn CPU? | Render-loop review, browser checks |
| Release integrity | Hashes, packaged file set, version alignment, working smoke test. | `package.py`, `runtime-integrity.json`, smoke driver |

### Out of scope

Model answer correctness, semantic quality of reviews, model benchmarks, and OS
sandbox guarantees beyond the platform the project claims to support.

## 3. Severity and triage

| Severity | Definition | Release rule |
| --- | --- | --- |
| **S1 blocker** | A primary journey is broken with no workaround; a safety or permission claim can be false; data loss or silent destructive behavior. | Must be fixed, or accepted in writing by the owner with rationale, before release. |
| **S2 high** | A journey is degraded but recoverable; state can be silently wrong; a documented QA gate cannot run. | Fix before release or record a dated waiver. |
| **S3 medium** | Friction, accessibility defect, documentation drift, or an untested invariant branch. | Triage into the next milestone. |
| **S4 low** | Polish, naming, minor layout. | Backlog. |

Triage rule: a finding is not closed until a failing check exists (automated or a
documented manual repro) and passes after the change. "Looks fixed" is not closure.

## 4. Critical journeys and acceptance criteria

Each journey is a release gate. Criteria are written so a tester can pass or fail
them without reading the source.

### J1 — First run with Ollama stopped

- Given a fresh install and no Ollama, when the user opens Sutra chat or model
  settings, then they see a guided, non-technical state naming the missing
  prerequisite, a Retry action, and no raw Python or socket error.
- When Ollama starts and the user retries, the installed models appear without a
  reload.
- **Fail if** a traceback, `ConnectionRefusedError`, or an empty picker with no
  explanation is the only outcome.

### J2 — Configure tiers

- Given installed chat models, when the user runs **Configure Local Models**, then
  low/mid/high prefill sensibly, saved choices survive a refresh, missing models
  are never silently invented, and saving is explicit.
- Given no eligible model, the Save path is disabled with a reason.
- **Fail if** a tier can be saved that the first request will reject.

### J3 — Arm and resume the same request

- Given an open chat with a typed draft, when the user arms workspace access and
  confirms once, then the same request continues in the same chat, the draft is
  not lost, and no second chat opens.
- The confirmation states the scope actually being granted (read paths; whether
  network or dependency flags are inherited), not a generic promise.
- Cancel, disarm, disable or workspace change revokes the pending confirmation.
- **Fail if** the dialog claims a restriction that the retained task does not have.

### J4 — Governed read-only review

- A review maps fresh source, runs bounded children sequentially, joins only
  current receipts, and reports what was inspected and what remains.
- A child cannot read outside its assigned paths, edit, run tests, or use network.
- Quarantine or source drift after a child completes blocks the join.
- The response does not claim access it did not have.
- **Fail if** the UI implies parallel inference or proof of semantic correctness.

### J5 — Edit proposal to receipt

- An edit is proposed as an exact replacement, confirmed with an exact-action
  dialog, applied once, and followed by a receipt or an explicit failure.
- Declining leaves the task readable and armed.
- The dialog states how long the file scope lasts (until reload, disarm, or a new
  contract), not just that each action is confirmed.
- **Fail if** applying twice is possible, or the user cannot see what was applied.

### J6 — Test execution

- On a supported platform, the permitted test runs in a scoped snapshot with
  network denied, and the result is labelled a process observation.
- Files that will not be runnable are not silently accepted as a test grant.
- On an unsupported platform, the user is told before granting, not after.
- **Fail if** a zero exit code is presented as proof that assertions passed.

### J7 — Model switch mid-task

- Switching model retains the task, constraints, calibration binding and consumed
  budget; it does not re-ask for permission already bound to the contract.
- The response still reflects the same source versions or refreshes them visibly.
- **Fail if** a switch resets budget or silently changes scope.

### J8 — Interrupt and continue

- Stopping a request preserves consumed budget and evidence.
- A **Continue** affordance resumes the saved workflow; an unrelated prompt warns
  that saved child reservations will be cancelled, or offers Discard.
- After reload, the user is told arming is required again.
- **Fail if** recovery depends on knowing a magic phrase, or discarding work is
  silent.

### J9 — Monitor and usage

- Unknown counts, real zero, stale snapshots, empty data and filtered-empty are
  visually distinct.
- No polling loop destroys focus, expanded rows, or in-progress edits.
- Hardware readings expire visibly; a resource ALLOW is never labelled as
  permission.
- The panel and the docked monitor stay consistent or share one renderer.
- **Fail if** a value changes under an open disclosure or an unlabelled stale
  reading looks live.

### J10 — Packaging and install

- The VSIX activates, bundled hashes verify, commands and tools are registered,
  and the packaged smoke driver passes against the packaged artifact.
- Version strings in manifest, docs and UI agree.
- **Fail if** the documented smoke test asserts an obsolete version or name.

## 5. Test layers and current inventory

| Layer | Where | Current state |
| --- | --- | --- |
| Python unit/integration | `harness/test_*.py` | 392 tests, green on a clean tree |
| Node contract/UI boundary | `test/*.cjs` | 6 suites, green |
| Browser (Playwright) | `test/*-browser.cjs` | Optional; not installed in CI |
| VS Code host drivers | `test/*-vscode.js` | Manual; need isolated profile and Ollama |
| Packaging smoke | `test/index.js` | Stale assertions; see findings |
| Adversarial | `test_checked.py`, `test_architecture.py`, `test_native_access.py` | Present for injection and scope escalation |

Run everything with `scripts/qa.sh` (or `--with-package` to include the VSIX).
Browser and host-driver runs stay opt-in because they need external runtimes.

## 6. Release gates

| Gate | Evidence | Owner |
| --- | --- | --- |
| G0 — build | `package.py` succeeds; kernel hash and `runtime-integrity.json` verify | release |
| G1 — automated | `scripts/qa.sh` green | QA |
| G2 — backend invariants | Targeted review of touched state transitions plus new tests for changed branches | QA + backend |
| G3 — journeys | J1–J10 sheet completed on macOS; unsupported-platform paths explicitly marked | QA |
| G4 — accessibility | Keyboard-only pass, axe or manual checklist on `media/` views | UX |
| G5 — packaged smoke | Smoke driver passes against the built VSIX; version strings agree | QA |

A release is blocked by any open S1, or an S2 without a dated waiver.

## 7. Cadence and artifacts

- **Per change:** run `scripts/qa.sh`; add a failing-then-passing check for the
  behavior; update user-facing copy and `docs/known-limitations.md` if a guarantee
  moved.
- **Per release:** update `docs/qa/FINDINGS-<version>.md`, complete the journey
  sheet, publish the gate table above with pass/fail and evidence links.
- **Triage:** severity assigned within one working day of a finding.

## 8. Definition of done for a QA-approved change

1. The behavior has an automated check or a documented manual repro.
2. The user-visible copy still matches the behavior.
3. Documentation and known limitations are updated when a guarantee changes.
4. No new S1 or S2 is open against the change.
5. The change was exercised end-to-end at least once on the platform it claims.

## 9. Release-candidate freeze

A sign-off is only valid for a frozen tree. The 0.49.0 review changed under the
reviewer three times (0.48.0 to 0.49.0, stale-to-fresh integrity manifest,
missing-then-present engine files), so the gate result and the tree could not be
quoted together. Rules:

1. **Freeze before sign-off.** Announce the freeze, confirm `git status` is
   stable across two checks at least five minutes apart, and record the commit.
2. **Hash last.** Run `scripts/runtime-integrity.py --write` after the final edit,
   never before packaging. A stale manifest fails closed at activation and blocks
   `check-release.py`, `test/hardening.cjs` and `test/runtime-integrity.cjs`.
3. **Run the gate twice.** A single green run does not clear a suite that failed
   earlier in the day; capture both runs and name any flake.
4. **Verify the artifact, not only the tree.** `scripts/qa.sh --with-package`
   must pass, and the VSIX must contain every runtime file the code spawns by
   path (service workers, engine modules, vendored packs).
5. **No sign-off on a WIP version.** Version strings, changelog and docs must
   name the same release before the gate is quoted.

## 10. Native-engine addendum (0.48+)

The native engine adds a new trust boundary. These checks are required in
addition to the journeys above:

| Check | Pass condition |
| --- | --- |
| Registry trust | A profile cannot silently repoint at a different binary. The resolved executable path is confirmed to the user before first spawn; the identity is keyed or content-derived, not an unkeyed descriptor hash. |
| Environment | The executable environment is built from the same allowlist the registry validation enforces; loader variables (`PYTHONPATH`, `DYLD_*`, `LD_PRELOAD`) are rejected. |
| Worker lifecycle | A wedged or stale worker is detected by owner identity before a new child is spawned; startup has one deadline and reports the live owner instead of a generic timeout. |
| Readiness truth | "Loaded" is shown only after server/telemetry identity is verified, or is explicitly labelled unverified. |
| Slot lease | Acquisition, cancellation and release are bounded; a killed child releases the OS lease; a failed release is reported, not hidden. |
| Packaging | Every process spawned by path ships in the VSIX, and the packaged gate asserts completeness. |
| Recovery | Failed activation, slot contention and profile change have distinct messages with the correct next action (Load/Retry, wait, re-import). |

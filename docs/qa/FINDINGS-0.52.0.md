# Findings — Sutra 0.52.0

Date: 2026-11-20
Reviewer: QA and UX validation lead
Snapshot: `HEAD bc74250` (clean tree), version 0.52.0, installed copy
`~/.vscode/extensions/deepak-local.karma-local-harness-0.52.0`.
Method: gate runs, packaging, installed-build comparison, static review and
targeted probes over the 0.50.0 → 0.52.0 delta (185 files).

## Verified baseline

| Check | Result |
| --- | --- |
| `scripts/qa.sh` (standard) | **24/24 passed** |
| Chrome browser suites (dashboard, diagram, usage, strata-manager) | **4/4 passed** |
| Python suite | **1,576 OK**, 8 skipped |
| Package | 290 runtime files, 288 integrity hashes |
| `check-release.py --package-dir` | passed |
| Installed = repo | 290/290 payloads byte-equal; 288/288 hashes match in both |
| Canonical kernel | `9dfef247…4eac` in repo, installed, VSIX |
| Release metadata | CHANGELOG first entry, version/README/panel aligned |

The installed 0.52.0 is bit-identical to the repo tree for every runtime file.
No packaging or integrity defect was found.

## Findings

### S1 — release evidence

| ID | Finding | Evidence |
| --- | --- | --- |
| R-01 | The validation report claims **99 focused tests** and **1,803 Python-to-JavaScript snapshots**. No script, fixture, artifact or log defines either number; the five core observation files contain 75 tests, and `grep` finds `1,803` only in the report and commit message. The authoritative run log shows 27 gates and 1,576 tests, not these figures. | `docs/qa/VALIDATION-0.52.0.md:99-100`; `/private/tmp/sutra0520-qa.log`; `harness/test_{coherence_gateway,coherence_observations,generation_activity,generation_progress,text_coherence}.py` |

This is a process defect, not a code defect: the shipped build is correct, but
the sign-off report contains two numbers that cannot be reproduced.

### S2 — high

| ID | Finding | Evidence |
| --- | --- | --- |
| C-01 | When more than 64 source aliases are delivered, `alias()` drops new ones and `root()` returns `None` for **every** unknown alias. Claims citing a dropped source are then classified `unsupported_claim` ("no source basis"), and genuine missing references are not reported. Probe: 70 pages → `unsupported_claims=32`, `missing_references=0`. | `harness/coherence_observations.py:113-115,209-214`; `harness/text_coherence.py:338-343` |
| C-02 | Page-fact scoping reads `context`/`subject`/`time`/`mode` and `current_version`, but delivered page entries contain only `id/version/hash/authority/status/text/facts`. All facts collapse into one factual context in production, so "scoped value differences" and "stale references" are only reachable in hand-built fixtures. `paging.py` also forces string fact values, making the int/bool branches unreachable. | `harness/coherence_observations.py:335-343,362-366`; `harness/evidence.py:210`; `harness/paging.py:303-316` |
| C-03 | `latest()` has no session filter, and `monitoring.py` feeds every session into the Monitor; `read_observation()` accepts an invocation id without a session. A probe returned rows from another session, contradicting the "does not read another task's history" claim. Leaked content is IDs/roles/counts, not source text. | `harness/coherence_observations.py:522-557`; `harness/monitoring.py:193-194`; `harness/orchestration_flow.py:203-207` |
| S-01 | The new repetition indicator is `role=status`/`aria-live=polite` but is the 5th child of a **closed** `<details>` card, so it is not announced. Its text is also reassigned on every `usage` message (~5/s while streaming), which would re-announce when open. The browser test only checks existence and text. | `media/usage.js:298-311`; `test/usage-browser.cjs:276-291` |
| S-02 | The activity archive slices to the last 12 calls **at start**, while the decoder admits 64 concurrent. With 13 concurrent calls the earliest still-generating call is evicted and its later thinking/progress is dropped. Reproduced with 13 starts. | `model_activity.js:91,138,142,147` |
| R-02 | The VSIX SHA-256 is not reproducible. `dist/Package.json` records `f88e156c…`; a clean rebuild produced a different digest for byte-identical content because `package.py` writes the two generated entries with `writestr` (current timestamps) while preserving source mtimes. Three different hashes are recorded for the same payload. | `package.py:59-62`; `dist/Package.json` |

### S3 — medium

| ID | Finding | Evidence |
| --- | --- | --- |
| C-04 | One malformed optional row sets the whole projection to `malformed` with zero diagnostics instead of a partial result. | `harness/coherence_observations.py:493-497,278,307-308,385,400` |
| C-05 | The gateway wraps observation capture and the adapter call in one `try`; an adapter failure replaces a valid projection with `unavailable`. | `harness/invocation_gateway.py:281-298` |
| C-06 | Internal projection caps (16 contexts / 48 sources / 96 items / 192 edges / 48,000 chars) differ from the documented 32/64/128/256/65,536. Conservative, but the 16-context cap drops page facts in wide sessions. | `harness/coherence_observations.py:24,348`; `harness/text_coherence.py:18-20` |
| C-07 | `latest()` parses and hashes up to 32 receipts of up to 2 MiB each per Monitor refresh, with no SQL prefilter (~64 MB worst case). | `harness/coherence_observations.py:19,542-544` |
| C-08 | IMMUTABLE triggers block `UPDATE`/`DELETE` but not `INSERT OR REPLACE`; no code uses REPLACE today, so this is hardening. | `harness/invocation_gateway.py:73-75` |
| C-09 | `_ROLES` omits `reviewer`/`document_verdict_reviewer`; chat/conversation/subagent deliver `pages=[]`, so claim-review and fact checks are only exercised by coder-style payloads. | `harness/coherence_observations.py:22` |
| S-03 | The live indicator latches even when `analysis_complete` is false, and the inline notice carries no coverage qualifier; a 1 MB chunk inspects 16,384 chars and still says "Repeated reasoning text observed". | `harness/generation_progress.py:116,143,164`; `media/generation_progress.js:13` |
| S-04 | Every streamed event republishes the whole activity archive (~198 KB for 12 full calls) and re-writes all card bodies; no signature guard, unlike the usage snapshot path. | `usage_view.js:33-40,101`; `media/usage.js:299-311` |
| R-03 | No release tags for 0.50.0 / 0.50.1 / 0.52.0; provenance rests on commit messages. | `git tag -l` |
| R-04 | "Read-only" ledger diagnostics open the database `mode=rw` and rely on `PRAGMA query_only`; this still permits WAL/SHM sidecar creation and write-lock upgrades during Monitor refresh. | `harness/ledger_sqlite.py:25-35,49-58` |

### S4 — low

- The repetition notice is emitted as plain assistant text with no "Sutra observed:" attribution and can interleave between thinking parts (`model_activity.js:39,62-64`).
- The decoder gate accepts any progress event with `repetition_indicator && alert_emitted`, not just the one-shot alert edge (`model_activity.js:98`).
- `chatActivity`'s per-call map is unbounded; only the decoder/ActivityLog are bounded (`model_activity.js:22-27`).
- Card prepending and summary lengthening cause layout shift that the browser fixture does not measure.
- Test gaps: no Python→JS snapshot parity fixture; no `analysis_complete=false` case; no `aria-live`/shift assertion; browser endurance dispatches fabricated rows and bypasses the decoder; nothing covers >12 concurrent calls or publish cost.

## Verified-safe properties of the new feature

- No new model call, prompt instruction, required schema field, repair loop, or
  admission/permission/budget effect. Capture runs after validation and never
  queries the ledger; the Karma frame is observation-only.
- Proposal/response hashes exclude observations; receipts remain insert-only with
  digest and chain verification; foreign/corrupt/oversized rows are rejected.
- Bounds are enforced before graph work; adversarial 200k-finding and 5k-page
  inputs complete in ~1 ms; the 8 KiB metadata cap raises before insert.
- Diagnostic errors do not withhold a valid answer or strand a reservation;
  cancellation still propagates.
- The repetition observer is bounded (128 digests, 32 novelty, 4,096 pending,
  16,384 inspected), fires once, and reaches only `progress.report(TextPart)` —
  no cancel, retry or control message.
- The activity decoder separates 64 active from 128 completed calls; 220
  sequential calls still emit activity; replay is rejected; shutdown is
  idempotent.

## Core product issue unchanged

The 0.51.9 live review remains accurate: architecture-review acceptance failed
(discovery convergence, cursor-namespace repair, bridge-headroom deferrals), and
0.52.0 explicitly leaves those as separate work. The new diagnostics improve
observability but do not address review quality or convergence.

## Release checklist for 0.52.0

1. Correct or remove the unreproducible 99 / 1,803 figures (R-01).
2. Fix C-01 (per-alias omission accounting) and C-03 (session filter) before the
   Monitor is trusted; C-02 needs either production fields or removal of the
   claim.
3. Fix S-01 (one page-level live region outside the closed card) and S-02
   (evict finished, not started, calls).
4. Make the VSIX digest reproducible (fixed ZIP timestamps) and republish the
   canonical digest (R-02).
5. Tag `v0.50.0`, `v0.50.1`, `v0.52.0` (R-03).
6. Carry the 0.51.9 review-convergence work as an explicit open risk in the
   release note.

## Verification limits

- Live inference was not re-run for this review; the 0.52 changes are diagnostic
  and the 0.50.0 engine phases remain the reference. The browser suites ran
  against local Chrome; the VS Code host smoke was not re-run.
- C-01/C-02/C-03 impacts were confirmed by code plus probes; the remaining
  observations are static or unit-level.

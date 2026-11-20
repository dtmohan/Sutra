# Sutra 0.51.8 validation

Date: 2026-11-19. The release repairs confirmed host overhead, scoped-search restrictions and review-memory lookup gaps found in the signed-in 0.51.7 trial. Native answer quality remains a separate acceptance check.

## Changes

- New architecture search decisions can target one exact authorized catalog file. Capability derives from the accepted immutable architect delivery. Query limits, source admission, replay binding and source-byte accounting remain enforced. Old unflagged searches retain their wire/schema/charge behavior.
- Architect result loading validates each unique ancestor once within a bounded read batch. A final sweep rechecks live dependencies, including quarantine, task revision, calibration, source contents and handoff. Ledger mutation, database identity/schema and guard changes invalidate the batch. No success cache survives the batch. Registration and finalization keep ownership of their existing transactions.
- New discovery packets retain a content-addressed nested directory, their latest working note and focused recalls from the same accepted journal. Index and root pages are bounded to 512 canonical bytes. Previously advertised addresses remain resolvable on demand after later checkpoints arrive. Missing mandatory capacity pauses before inference; the fitter cannot silently erase the directory or latest note.
- The latest note is delivered once under the new memory profile. Notes and source-position indexes are unverified context, not source evidence or permission. Source freshness, current contract and normal gateway admission still apply.
- Data-only conversations from the released 0.51.7 policy remain readable after upgrade. Its policy hash was independently recomputed from the released VSIX. Old executable proposals and checkpoint authority remain stale after a policy change.

## Validation

Command: `scripts/qa.sh --with-package --browser`, using the installed local Node runtime, vendored TypeScript and headless Chrome. Full log: `/private/tmp/sutra0518-qa.log`.

| Check | Result |
| --- | --- |
| Release gates | 26 passed, 0 failed |
| Python suite | 1,476 tests; 8 skipped; 312.418 seconds |
| Node contract and runtime-integrity suites | Passed |
| Browser webviews and accessibility checks | Passed |
| Offline package and exact packaged-content verification | Passed |
| Shipped runtime files | 285; tests excluded |

New regression coverage includes:

- File-scoped search following more than five unrelated global matches; exact one-file charges; path/symlink/foreign-execution checks; accepted-decision-to-tool recovery and immutable replay.
- Twenty accepted ancestors validated 20 times instead of 210, both outside transactions and inside actual gateway registration/finalization. Tests retain transaction ownership, reject changed sources even with restored modification times, and reject ancestor quarantine, wrong sessions and ledger mutation.
- Fifteen checkpoint steps across nine paths followed by eviction, reload and a model switch. The actual delivered packet retains its root/latest gap and retrieves an earlier note. Tests exercise historical directory addresses after a newer checkpoint, typed-content tampering, quarantine, changed source, physical-capacity refusal, counter failure and offline stored-proof verification.
- The existing all-notes-retired regression now explicitly selects the historical memory profile. Two synthetic byte-as-token prefix fixtures use a 16,384-token window to accommodate the new mandatory directory floor while still proving partial coherent source paging. The 4,096-token overflow refusal and no-replay assertions remain intact.

## Measurements and limits

In a synthetic 20-checkpoint ledger fixture, three result-verification measurements gave median strict traversal 371.495 ms and batched traversal 40.850 ms. Results and receipts were identical. This measures one host verification operation, not native review latency or model quality. Artifact: `/private/tmp/sutra0518-architect-batch-benchmark.json`.

Default journal-build work counts were 89, 173 and 343 for 20, 40 and 80 checkpoints. No historical prefix was expanded on that default path. Historical reconstruction is requested on demand and scoped to known advertised references. Artifact: `/private/tmp/sutra0518-memory-work-counts.json`.

The new review journal is scoped to the current session and discovery key. It does not automatically migrate 0.51.7 executable checkpoints or its older review notes across a policy change. Original receipts and spending remain preserved. Native policy recovery must explicitly revalidate current source and scope.

Financial reservations still use the existing conservative UTF-8 accounting profile. Exact token counts govern supported physical context windows. Switching financial planning and offline replay to exact input-token reservations requires a separate versioned change; it is not included here. No historical reservation was recomputed or refunded.

The STP kernel is unchanged: SHA-256 `9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`. It is stateless and can return FIN directly. Child negotiation and allocation recovery remain host/adapter responsibilities; this release does not add a new negotiation protocol.

## Native acceptance baseline

The saved High review is `review-25447a34c017`, original flow `a77a5db8654d4ffb9e2dd3fbb70f8fc7`, paused at 234,833 consumed units with no pending inference or repository effects. All 23 model receipts and all earlier task/source prefixes were verified before upgrade. The next native trial must use this saved task and preserve those records; a fresh budget would not test recovery.

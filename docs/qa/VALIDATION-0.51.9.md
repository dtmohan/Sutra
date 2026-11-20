# Sutra 0.51.9 validation

Date: 2026-11-19. This release addresses the actual missing-source-body condition and reasoning override discovered in the native 0.51.8 review. It preserves the existing gateway and canonical STP kernel.

## Implemented behavior

New discovery packets can recall whole relevant source units from current, scoped repository receipts. Their selection descriptor and ordering are regenerated during validation. A required unit per focused source and the coherent fresh-source prefix cannot be silently removed during fitting. Other focused units take priority over unrelated older source. Source indexes and notes remain unverified context; code text in the evidence field remains bound to its original source receipt and current hash.

This page-in is context preparation, not a new `repository_tools.execute` operation. It does not refund prior source charges or create a second repository-tool receipt. Current file hashing and freshness checks still read actual source bytes when required. Every subsequent model input incurs its normal reservation and settlement.

The new discovery reasoning profile preserves explicit On, Off and Model default. Adaptive can select the first supported positive control when source is supplied. The control is rebound after packing changes and counted into the exact physical proof before dispatch. Output reserves and retry ceilings are unchanged; unknown capabilities do not receive an invented control.

The next-request boost applies to orchestration planning and answering. The bridge passes a separate configured base preference for delegated children; ordinary requests clear that temporary child key, and the worker restores its environment on success and failure. Child permissions, models and budgets are unchanged. This does not implement a new child self-authorization or negotiation protocol.

Data-only conversation compatibility includes the released 0.51.8 policy, independently recomputed from its VSIX. Old executable checkpoints are not automatically promoted across policy changes.

## Full validation

Command: `scripts/qa.sh --with-package --browser`. Full log: `/private/tmp/sutra0519-qa.log`.

| Check | Result |
| --- | --- |
| Release gates | 26 passed; 0 failed |
| Python suite | 1,501 tests; 8 skipped; 391.164 seconds |
| Node contract, worker, authorization and integrity suites | Passed |
| Browser webviews | Passed |
| Offline VSIX and exact packaged-content checks | Passed |
| Runtime files | 285; tests excluded |

Focused new coverage comprises nine source-recall cases, fifteen reasoning/environment cases and one end-to-end exposure/publication case, in addition to adjacent and historical compatibility tests.

The source regression first reproduces a 15-step journal whose note and index survive while a needed code body is evicted. The new recipe restores a whole later authorization unit, rather than only a function header, after reload and a model switch. Tests cover physical paging, required-floor capacity refusal, exact-counter failure, source changes with restored modification times, quarantine, scope, forged descriptors, ordering and explicit reread. Historical flag-absent wire/proof behavior remains unchanged.

Reasoning tests cover the actual selector gateway, explicit preferences, supported Adaptive controls, default/unknown capabilities, final-source rebinding and offline exact proofs. Worker and extension tests check parent On with child base preference, explicit clearing on the next ordinary request, exception restoration, malformed controls and unchanged persisted settings.

## Saved-budget end-to-end regression

The conditional fixture reports only when its current evidence contains both decisive caller and callee bodies. Notes and an index alone cause a reread. It seeds 400,167 units of governed spending under a 500,000-unit limit in a temporary ledger; it does not copy or rewrite the user's receipts.

From identical temporary prefixes, the old recipe reproduces the reread. The corrected branch reloads on High, pages both bodies into actual evidence, produces an accepted report decision without another repository operation, then reloads on Mid and completes writer and claim publication. The writer also checks that both bodies occur in its real excerpts. Original receipt/source prefixes, constraints and limits remain unchanged, and new model charges are retained. The case passed in 14.114 seconds in the isolated composition.

This proves synthetic source exposure and protocol recovery. It does not prove that a real model will choose the right files, produce a complete explanation or finish within a predicted duration.

## Native continuation baseline and limits

The real review remains task `review-25447a34c017`, successor flow `a7411afbc87540fc802ca52462e45175`, paused at 400,167 consumed units with no pending work. Its 39 model receipts and every earlier model/source prefix were verified before upgrade. Remaining capacity is 99,833 units, eleven calls and 588.418159 charged seconds before any permitted allocation recovery. No budget reset or grant beyond the configured workspace ceiling is authorized by this release.

The next native test must preserve that prefix and verify actual source-body recall, positive supported planning controls, report quality, citations and diagrams. No native quality pass is claimed by these offline tests.

Canonical kernel SHA-256 remains `9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`. BRIDGE/FIN remain unchanged and stateless. Financial planning still uses the existing conservative reservation profile; this release does not rewrite historical charges or introduce exact-token financial accounting.

# Sutra 0.51.8: saved native review and source-recall gap

Date: 2026-11-19. The tested installed VSIX SHA-256 was `04d5786d734b617ac7c9c77bb17183912e7ce6560d495f2cfc8760d59744adb5`. All 285 runtime files matched the package before VS Code was reloaded. Full release validation passed 26 gates and 1,476 Python tests, with eight skips.

## Actual native continuation

The existing signed-in chat remained on Sutra High, with the configured Low/Mid/High tiers unchanged. The user-facing prompt remained exactly the stored detailed tools/architecture/diagrams request. Its contract goal matched that prompt; no older high-level-first objective displaced it.

Native `continue` and the matching policy-recovery confirmation created successor flow `a7411afbc87540fc802ca52462e45175`, explicitly restarted from `a77a5db8654d4ffb9e2dd3fbb70f8fc7`. Both used task `review-25447a34c017`. The original 234,833-unit prefix was retained, not reset.

Source mapping completed approximately 129.1 seconds after work selection. Sixteen new architecture discovery calls were then accepted. No new-policy model response was rejected, but no parent draft or publication candidate was produced.

The request was deliberately stopped through the native chat after a completed call, rather than allowing repeated source requests to continue spending. The backend saved `PAUSED / BRIDGE_INTERRUPTED`; subsequent observations confirmed no pending model reservation or repository effect, `busy=false`, `queued=0`, and the selected model still loaded.

| Final observation | Value |
| --- | ---: |
| Total consumed task budget units | 400,167 |
| Additional units during this trial | 165,334 |
| Remaining tokens/units under current allocation | 99,833 |
| Remaining calls | 11 |
| Remaining charged seconds | 588.418159 |
| Returned source bytes during this trial | 130,464 |
| Unique returned source positions, in bytes | 69,779 |
| Overlapping returned positions, in bytes | 60,685 |
| Distinct source paths | 6 |
| Parent candidates | 0 |

Every original model and source prefix remained exact, including all 23 pre-upgrade model receipts. There was no refund, spending reset or deletion. Overlap is reported separately from new coverage; a repeated model request is not a replay of a completed host effect.

## What worked

All sixteen new memory packets passed independent hash checks. Directory roots grew from zero through fifteen notes and remained at most 500 canonical bytes. For every nonempty journal, the exact latest note was delivered. Later calls also received exact earlier notes from the same policy journal after those notes would otherwise have left the working window.

For example, the third selector received both earlier and latest repository notes and 17,706 source bytes. Later packets recalled earlier extension and capabilities notes alongside their current gaps. This is live evidence that same-policy note recall worked. It is not evidence that old-policy review notes were automatically migrated.

The explicit file-search capability and the complete-note byte allowance were accepted without new formatting rejections. The initial accepted summary had 88 characters and was not rejected for the old per-field character bound.

## Remaining source-memory defect

Directory and note recall did not ensure that the corresponding source body was present. Selector nine received the earlier capabilities note describing authorization, reservation and dispatch, but its actual evidence contained only extension and repository-tool source. Its subsequent capabilities read can therefore be a request for source authority, rather than proof that the model ignored available source or lacked capability.

The source-index leaf contains a receipt address and source positions, not the original code text. `_bind_memory` selects notes and index pages; it does not promote the indexed body in `packet.evidence`. The fitter can still retire that older body while retaining its note and directory address. Repeated source reads followed: repository-tools line 290 was requested four times, capabilities line 164 repeatedly, and architecture-runtime line 113 repeatedly. Some extension and architecture-runtime reads also extended earlier ranges, so they are not all identical overlap.

The next correction must page relevant whole source units from already verified, current scoped receipts into the actual evidence field. Notes remain unverified. Source freshness, scope, input charges, physical fit and kernel admission must continue to apply. Prohibiting rereads would conceal this missing-data problem.

## Timing observations

The sixteen provider calls reported 342.32 seconds of total call elapsed time, including 300.78 seconds of prompt processing and 41.24 seconds of decoding. All reported `cache_n=0`. These are the runtime's per-call observations; no cache experiment or attribution of the cache misses was performed.

Between successive dispatches, subtracting the preceding recorded call duration left 643.43 seconds of unattributed host intervals. Early intervals were approximately 23–29 seconds; later ones reached approximately 51–76 seconds. They include preparation, validation, source work and transport/orchestration intervals. They cannot be attributed entirely to one function from these receipts alone.

The previous two-minute host intervals improved, but substantial overhead and the source-recall loop remain. A local microbenchmark of ancestry validation is not an end-to-end latency claim.

## Reasoning-control audit

Source inspection found that `review_reasoning.bind(..., bounded_selection=True)` rewrites explicit On and Model default to Adaptive, then selects Off for discovery. Existing tests deliberately preserve that historical behavior. It contradicts the documented explicit-override semantics; the one-request toggle reaches the bridge correctly and is overridden later in host policy. This live trial used Adaptive, so it was not an experiment with explicit On.

The user's subsequent scope decision is to boost the model doing orchestration planning and synthesis for one request, while children retain task-specific reasoning within their delegated envelope. That does not expand child permissions, models or budgets. A future versioned correction must preserve old packet proofs and distinguish a one-request parent boost from the child's configured base preference.

The canonical STP kernel remains unchanged at SHA-256 `9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`. No claim of useful native architecture output is made for this trial: no parent draft was generated.

Local artifacts: `/private/tmp/sutra0518-final-audit.json`, `/private/tmp/sutra0518-observer.jsonl`, `/private/tmp/sutra0518-baseline-proof.json`, and the native UI captures. These diagnostics are not shipped runtime authority.

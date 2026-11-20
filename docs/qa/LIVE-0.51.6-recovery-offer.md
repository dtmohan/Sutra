# Sutra 0.51.6: native recovery offer and deliberate Decline

Date: 2026-11-19. This test used the existing signed-in native VS Code chat after installation and Reload of 0.51.6. All 285 installed runtime files were verified against the package, and the canonical STP kernel was unchanged. Automated results and the test-adapter correction are recorded in [0.51.6 validation](VALIDATION-0.51.6.md).

## Result and scope

Native Continue displayed the confirmation for an **unpublished terminal review**, explaining that the original task, constraints and consumed spending would be retained and that older open work would remain saved and be parked. The confirmation was deliberately declined through its native Cancel button. The helper reported `AXError(rawValue: 0)`, and the subsequent native capture showed “Saved request retained.”

This was a recovery-offer and Decline test. No positive policy restart, source mapping, model inference, new candidate or published report was requested by the declined offer. Architecture and diagram quality remain untested in this trial.

## Target identity: scoped ledger evidence and the native modal

The UI did not expose a workflow ID. Its modal was specific to a closed unpublished review: “This unpublished review ended under an older Sutra policy,” followed by the explanation that the older open task would be parked. It also displayed the unchanged original architecture request.

The intended target was independently deduced from that terminal-only modal and a read-only reproduction of the central lookup. The observer used a SQLite `mode=ro` connection with `query_only`, without constructing an `InvocationLedger` or changing application state.

The reproduction followed the implementation's immutable ordering:

1. Scope flow rows to `7a78…`'s exact workspace and original native chat, `ad70023ceb53ebb1be1248a68972961c8062ab554ff7baa4deb044f34e28b4cc`.
2. Select the latest original ask by SQLite append order, excluding restart rows. This selected `e7a5d038fcc14b4db925b3c2f201a6ed`.
3. Walk each candidate's `restarted_from` chain, checking that workspace, chat and prompt remain identical. Select the newest descendant of that original ask. This selected `7a78afd3bad542edbe87a0f60755f7fb`.
4. Check terminal-review eligibility: `DONE`, `WORKFLOW_FINISHED` with publication withheld, original task-stack ownership, and the saved accepted parent candidate's immutable receipt and checkpoint bindings.

Only `7a78afd3bad542edbe87a0f60755f7fb` qualified. Its task is `review-493c39377419`, with the saved candidate `ebe69325da8f4828956b2d0c17a81608` and 188,254 settled units. The envelope, receipt, response, delivery and checkpoint hashes matched; the candidate's task, actor flow, workspace and original request also matched, and the invocation was not quarantined.

The newer-created `b7205b19b66843a99a83bf9549d9b949` did not become a newer ask. Its chain normalizes to the older original `4c4a1e3b423e469f829c4d8ff6e87725`, on task `review-2e6d73773c99`. This distinction addresses the older-open-task selection observed in the [0.51.5 native trial](LIVE-0.51.5-workspace-review.md). A same-prompt match or a later restart timestamp was not used to establish identity.

These observations support the intended recovery offer. They do not substitute for a visible UI flow ID or a completed positive restart; neither was produced by this Decline test.

## Post-decline continuity

The final read-only check, at `1795139508.498079`, found **zero new flow rows and zero new Sutra model invocations** since the pre-offer observation boundary at `1795139215.827892`. No background sampler was started.

All recorded invocation and receipt hashes and settlement amounts remained unchanged:

| Task | Retained model receipts | Settled units |
| --- | ---: | ---: |
| Intended `review-493c39377419` | 21 | 188,254 |
| Older open `review-2e6d73773c99` | 3 | 69,045 |
| Historical `review-72c1695d466b` | 15 | 135,642 |
| Historical `review-1a772edb76d2` | 9 | 58,473 |
| Historical `review-b9a32b0299a8` | 14 | 199,496 |

The older task's 20 source operations also retained their exact commitments, receipts, charge records and settlements, including **5,668,622 source bytes** and 20 operation units. This includes the previously interrupted mapping operation. Declining the offer did not refund or replace those records, reset the intended task's spending, or discard the older task.

## Capacity and remaining native acceptance

The database remains at the reported 994.27 MiB, with zero free pages, against the user-approved 1,024 MiB history limit. Approval to increase the allowance to **2,048 MiB** is pending. No additional allowance increase, history pruning or direct database edit was applied. A full mapping must wait for the required capacity decision.

Positive native Continue, fresh source-page analysis, reviewer recovery, and a useful source-supported architecture report with diagrams remain outstanding. The picker was restored to Sutra Auto and the owned High runtime was restored to available and idle; this does not establish report inference or acceptance.

## Local evidence

- Native offer, Decline and final UI captures: `/private/tmp/sutra0516-offer-ui.txt`, `/private/tmp/sutra0516-declined-ui.txt`, `/private/tmp/sutra0516-final-ui.txt`.
- Read-only scoped target and receipt proof: `/private/tmp/sutra0516-scoped-recovery-proof.json`.
- Final continuity check: `/private/tmp/sutra0516-after-decline-proof.json`.
- Model and source baselines: `/private/tmp/sutra0516-baseline.json`, `/private/tmp/sutra0516-source-baseline.json`; baseline verification record: `/private/tmp/sutra0516-baseline-proof.json`.
- Strict descendant-only observer, prepared and unstarted: `/private/tmp/sutra0516-observer.py`.

The temporary artifacts document this bounded native test. They are not a published architecture report or evidence of semantic correctness.

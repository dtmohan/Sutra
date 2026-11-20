# Sutra 0.51.5: Continue selected an older open task

Date: 2026-11-19. Tested in the existing signed-in VS Code window after native installation and Reload. All 26 automated gates passed before the trial: 1,421 Python tests, eight skipped, plus browser, Node and package checks. The installed bytes of 285 runtime files were verified, and the canonical STP kernel remained unchanged.

## Result

Native Continue resumed an older open review with the same prompt, rather than recovering the intended task's withheld report. The older flow was safely cancelled through the native UI. No new model invocation or model-token charge occurred. This trial provides **no report-quality verdict** and does not test the new source-page retention or reviewer-availability behavior through inference.

The intended task's 188,254-unit, 21-receipt prefix remained unchanged. The observed 69,045-unit balance belonged to a different historical task; it was not a reduced or reset balance for the intended task.

## Intended task and actual lineage

The unchanged original request was:

> can you review the workspace project and go through the tools logic and help me understand the architecture in detail? a few block diagrams would be great

The trial marker was recorded at `1795138237.8628411`. Its intended anchor was the completed 0.51.4 flow `7a78afd3bad542edbe87a0f60755f7fb`, on task `review-493c39377419`, with 188,254 settled units. Native Continue showed a policy-restart modal containing the original request text. That text was shared by multiple historical reviews and did not establish task identity.

| Identity | Intended recovery | Observed restart |
| --- | --- | --- |
| Task session | `review-493c39377419` | `review-2e6d73773c99` |
| Prior flow | `7a78afd3bad542edbe87a0f60755f7fb` | `4c4a1e3b423e469f829c4d8ff6e87725` |
| New flow | No descendant of the intended anchor was created | `b7205b19b66843a99a83bf9549d9b949` |
| New flow creation | — | `1795138259.7039711` |
| Bound execution | Intended prior execution: `3212437f38114a31987f7f239e027e75` | `930e069e86464e0d8f34ea6bfc940a09` |
| Historical settled model units | 188,254 across 21 receipts | 69,045 across three receipts |

The immutable intended lineage is `7a78… ← 9ade3264b7004e8793c8c4c2a0e41588 ← e7a5d038fcc14b4db925b3c2f201a6ed`. The observed flow instead records `restarted_from: 4c4a1e3b423e469f829c4d8ff6e87725`. That older flow was created at `1795108651.687729`, before the intended task's trials. There is no restart edge connecting these two lineages. The observed execution's `previous` field is null.

The completed `7a78…` flow had closed its task-stack entry and restored the older open `4c4a…` focus. Native Continue prioritized that open task before the withheld-report recovery lookup. The restart preserved the older task's own binding and spending. This was an open-task selection problem, not a session migration, inherited-payment recovery or accounting refund.

The older task's 69,045 units were all historical quarantined child calls:

| Invocation | Creation time | Settled units |
| --- | ---: | ---: |
| `68ada6d56752499783d26213064f82cb` | `1795108782.316382` | 7,304 |
| `1e24922871c04ae695d87fce15f0042f` | `1795108810.308807` | 30,833 |
| `3792e2fd3ed24e96b3a49d6f05638f2a` | `1795109218.14241` | 30,908 |

## Cancellation, source work and preserved charges

The observed restart mapped source and reached `CHILDREN`. Native Cancel then produced `BRIDGE_INTERRUPTED` and saved `PAUSED` at `1795138423.834074`.

- New model invocations: **zero**. New model-token charges: **zero**.
- New repository receipts: **20**, comprising **19 successful** operations and **one interrupted** operation.
- Source-byte cost recorded across those operations: **5,668,622 bytes**. This is the source reservation/accounting total across mapping work, not unique source bytes delivered to a model.
- Pending model reservations and unsettled charges in the new execution: **zero** after cancellation settled.
- The older task remained at **69,045 settled model units**; the intended task remained at **188,254**.

Read-only verification checked the original invocation and receipt hashes and settlement amounts. All four recorded prefixes remained unchanged: 135,642 units for `review-72c1695d466b`, 58,473 for `review-1a772edb76d2`, 199,496 for `review-b9a32b0299a8`, and the intended task's 188,254 units. No spending reset, replacement receipt or refund was applied.

## Observer correction

The initial observer contained a fallback that discarded an old terminal anchor, then searched for a newly created flow with the same workspace and exact prompt. It selected `b7205…` through that fallback. The first status message incorrectly inferred that this flow descended from `7a78…`.

That attribution was corrected after inspecting the immutable `restarted_from` edges, flow events, root bindings and historical receipt dates. The receipt-prefix checks correctly showed that the intended 188,254-unit history was still intact. A matching request string was insufficient evidence of a recovery link.

The sampler was stopped and its observations were retained unchanged. The replacement observer requires an explicit anchor and follows only its immutable descendants. When checked against `7a78…`, it reports `WAITING_FOR_DESCENDANT` with the intended task's 188,254 units instead of selecting another same-prompt task. Other new flows are recorded separately and never adopted through a prompt match.

The 0.51.6 observer was prepared without starting a sampler. Its verified baseline retains the four original prefixes and adds the older task's three receipts at 69,045 units. A separate source baseline binds the 20 mapping commitments, receipts, charge records and settlements, including the 5,668,622-byte cost, so future parking can be checked without rebasing changed records.

## Storage boundary and validation limits

The post-trial database check reported **994.27 MiB** against the already approved **1,024 MiB** history allowance, with **zero free pages**. Approximately 29.73 MiB of file-size headroom remained, insufficient for another full mapping under the existing conservative storage admission. No history pruning or additional allowance increase was applied. This is a history-storage boundary, separate from financial model units and physical model-context capacity.

The automated pass establishes the tested fixture, package and browser behavior. This native trial establishes safe cancellation and unchanged historical accounting after the wrong older task was selected. It does not establish architecture completeness, source-page usefulness, reviewer recovery or diagram quality; no inference, candidate or published report was produced in this trial.

## Local evidence

- Final automated log: `/private/tmp/sutra0515-qa-final.log`; browser artifacts: `/private/tmp/sutra0515-qa-final-ui/`.
- Native marker: `/private/tmp/sutra0515-ui-start.json`.
- Original observation log and baseline: `/private/tmp/sutra0515-observer.jsonl` and `/private/tmp/sutra0515-baseline.json`.
- Settled wrong-task audit: `/private/tmp/sutra0515-wrong-task-audit.json`.
- Prepared strict observer: `/private/tmp/sutra0516-observer.py`; verified baselines and proof: `/private/tmp/sutra0516-baseline.json`, `/private/tmp/sutra0516-source-baseline.json` and `/private/tmp/sutra0516-baseline-proof.json`.
- Canonical STP kernel SHA-256: `9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`, unchanged.

These temporary artifacts are diagnostic evidence, not a published architecture report or model authority.

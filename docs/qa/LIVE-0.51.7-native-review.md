# Sutra 0.51.7: native review, sleep recovery and retrieval diagnosis

Date: 2026-11-19. Tests used the signed-in VS Code chat and its installed extension, not a direct substitute invocation. This records observed behavior before the next repair. A saved workflow and accepted protocol records do not establish answer quality.

## Configuration and scope

- Installed version: 0.51.7; package SHA-256 `512cc67fffeb0fc36517c98cb332f7ef63fdd8e43e768d54187ea6583f42e8a8`.
- Low: `qwen3:4b`; Mid: `local-assistant:latest`; High: `strata:high`. Tier settings were preserved.
- Shared reasoning: Adaptive. The native picker was explicitly switched to High for the second comparison.
- High's observed server model: `qwen3.8-flash-next-q2_0`; verified context window: 32,768 tokens.
- User-approved history ceiling: 2 GiB. No receipts or prior spending were deleted.
- Source was frozen throughout inference. Repairs were first prepared in isolated temporary copies after the live request was deliberately paused.

The High prompt was:

> Start a fresh read-only review and keep earlier tasks saved.
>
> can you review the workspace project and go through the tools logic and help me understand the architecture in detail? a few block diagrams would be great

The High flow was `a77a5db8654d4ffb9e2dd3fbb70f8fc7`, task `review-25447a34c017`. It was a new comparison task, not a reset of the earlier Mid review's allocation.

## Outcomes

The Mid review completed and displayed a report and a working diagram preview. Its detailed architecture quality failed independent source-support assessment: the parent received only 449 bytes of source, mainly declarations, and none of the seven predefined architecture mechanisms was adequately supported. A successful protocol result and the small claim reviewer's positive opinions were insufficient evidence of a useful architecture review.

The High review had not produced a parent candidate when deliberately paused. It therefore has no report-quality score. It made 23 model dispatches: four child dispatches (two successful children, each following one structured-output rejection) and 19 discovery dispatches (15 accepted selections, four quarantined results including the sleep interruption). At the final saved checkpoint:

| Observation | Value |
| --- | ---: |
| Consumed task budget units | 234,833 |
| Pending model reservations | 0 |
| Pending repository operations | 0 |
| Current token allocation | 312,500 |
| Returned governed source bytes | 101,266 |
| Unique returned source positions, in bytes | 75,970 |
| Overlapping returned source positions, in bytes | 25,296 |

Overlap records include deliberate new model read requests. They are not evidence that recovery replayed a completed effect. Returned positions also do not establish semantic understanding or that all text remained in a later prompt.

## Sleep and model recovery

The battery-related sleep interrupted one discovery invocation. Its completed runtime usage was unknown, so the full committed 65,668-unit reservation remained charged. The receipt's fallback seconds value is a declared reservation, not a measured backend duration.

After the Mac returned, the owned server was alive but its model was unloaded. Native Start alone did not restore it. Native Stop followed by Start restored the same selected model. Sending `continue` in the existing chat resumed the same flow and task from synthesis. Both completed child checkpoints and the earlier 12,271-byte repository page remained available; they were not rerun to recover the task.

The post-sleep prefix contained six model receipts totaling 116,508 units and 27 repository receipts. Every prefix record remained byte-identical after resumption. All five earlier review prefixes also remained unchanged, including the Mid task's 281,827 units. All 281 prior repository operations, recording 72,063,318 source bytes, remained unchanged.

The old deadline error still appeared as a generic native chat failure during the sleep interruption even though the backend retained a resumable pause. This run does not demonstrate a complete sleep-related UX fix.

At deliberate cancellation, the native chat saved `BRIDGE_INTERRUPTED` with `resume_phase=SYNTHESIS`. The backend subsequently reported `busy=false`, `queued=0`, and `activity.in_flight=0` before source repairs began.

## Confirmed harness problems

### Working state was no longer discoverable

The complete current request remained in the delivered packet. In the inspected later selector packet, however, the older working-note collection was empty and both child notes had been retired. The latest handoff note was still projected as `earlier_work_unverified`; this was not complete loss of working memory. The bounded progress view named only two previously supplied paths and omitted seven others. The actual supplied source was 13,088 bytes: bridge code, orchestration code and search snippets.

The model then described `extension.js` activation as unopened and requested it again, although an earlier accepted call had received 10,327 bytes from that file. This supports missing working state as a contributor to repeated reads; it does not establish that it was the only cause.

The review's `source_page_toc` indexes only source units not yet supplied in an accepted call. Fully supplied pages disappear from that index. It is not a persistent directory of saved review notes and already supplied source. Conversation-history lookup does not reconnect this review-specific journal. The exact records survived, but their model-facing lookup path was incomplete.

Physical context and financial allocation were distinct. For example, one selector had an exact physical requirement of 6,486 tokens including output and framing, while its conservative financial packet occupied roughly 98% of its 21,358-unit per-call allowance. Financial fitting retired notes even though the physical context had room. No byte-to-token ratio was substituted for a verified count, and historical charges were retained.

### Repeated ancestry validation increased host work

Checkpoint intervals grew to roughly two minutes while several corresponding model calls took approximately 15–24 seconds. Sampling showed substantial bridge CPU use in Python evaluation and object comparisons. Native samples did not identify Python function names, so the live sample alone does not attribute all residual time to a particular function.

Source inspection and an isolated real-ledger regression identified repeated ancestry walks: loading every result in a 20-checkpoint chain separately performed 210 validation-body visits. A candidate batch verified each unique architect ancestor once (20 visits), followed by a fresh dependency sweep. That count result is not yet a measurement of live latency improvement.

### Search scope caused avoidable rejections and irrelevant retrieval

The model repeatedly proposed a bounded literal search within `harness/invocation_gateway.py`. The old tool accepted only an empty target for global search, yet its error blamed the literal query. After the model corrected the target to empty, the first five global matches were in unrelated files rather than the desired definition. This was an interface and retrieval restriction, not an established physical-capacity failure.

The isolated correction permits one exact, authorized catalog file as the search target for newly versioned discovery requests. It derives that capability from the accepted immutable architect delivery, retains literal-query limits and path checks, and charges only the selected file. Legacy unflagged requests retain their previous wire format and behavior.

## Interpretation and remaining acceptance criteria

Recovery preserved records, permissions and consumed spending. A useful High report was not delivered in this run. The request was paused deliberately to avoid further repeated discovery calls while repairing the confirmed host problems.

The next native acceptance run must verify saved-task continuity, bounded validation work, useful source lookup after eviction, a supported parent report, claim review, and usable diagrams. Budget recovery alone, an accepted model envelope or a terminal `DONE` state is not a quality pass.

The canonical STP kernel remains unchanged at SHA-256 `9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`. The kernel is stateless: a request can enter FIN directly; BRIDGE is not a guaranteed earlier state. Requests for more evidence or allocation belong in the host adapters and existing gateway. A memory reference does not grant permission, and any revised admission must preserve prior spending.

Local diagnostic artifacts: `/private/tmp/sutra0517-high-final-prepatch-audit.json`, `/private/tmp/sutra0517-high-repetition-audit.json`, `/private/tmp/sutra0517-bridge-89740.sample.txt`, and the native UI captures. These temporary files are not shipped runtime evidence.

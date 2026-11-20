# Sutra 0.51.4: accepted draft, withheld report and insufficient architecture evidence

Date: 2026-11-19. Tested in the existing signed-in VS Code window against the frozen 0.51.4 package, using native Continue and an explicitly confirmed policy restart of the same saved request.

## Result

The native trial ended `DONE` with the response **withheld**. A Mid parent draft passed the gateway's structural checks and was saved, but no architecture report or diagrams were published. Claim review never invoked a model: reviewer identity selection probed the stopped High runtime, received an unavailable Strata status, and aborted selection instead of continuing through the other reviewer identities.

The accepted draft also failed the manual architecture-quality check. It explained part of extension activation, then supplied speculative Python module responsibilities and a request flow unsupported by its delivered source. Resolving reviewer availability alone would not make this draft an adequate answer to the original request.

## Native reproduction and accounting

Original prompt, retained unchanged:

> can you review the workspace project and go through the tools logic and help me understand the architecture in detail? a few block diagrams would be great

- Task: `review-493c39377419`, reused from the 0.51.2 and 0.51.3 trials.
- Previous flow: `9ade3264b7004e8793c8c4c2a0e41588`; policy-restarted successor: `7a78afd3bad542edbe87a0f60755f7fb`, created at `1795135056.232826`.
- Native picker: **Sutra Mid**, using `local-assistant:latest`. The idle, Sutra-owned High runtime had been stopped through the native UI before Continue to make room for Mid. Saved tier assignments were not changed.
- The final native chat turn ran from approximately 16:37 to 16:50 local time; the accessibility snapshot reports an elapsed time of **13m 46s**.
- The task began this trial with **104,960** settled budget units across 15 receipts. Six new model calls added **83,294**, reaching **188,254 total settled units**, with **zero pending reservations**. This total includes paid work from earlier versions of the same task; it is not this trial's measured token count.

The final UI said: “Sutra withheld the response: Architecture report not published: Strata status is unavailable on localhost”. Its footer showed `17,198 + unknown in · 2,211 + unknown out · 6 calls`. The unknown counts belong to the two timed-out selectors. They were charged their full reservations, not treated as zero or refunded.

| New invocation | Result | Measured input / output | Settled budget units |
| --- | --- | ---: | ---: |
| `f17a137b85894727b29870d344ee0762` | Architect timeout after 150.036 seconds | unknown / unknown | 31,918 |
| `ce6c1d0e943646568e9dfc79dc59a950` | Architect timeout after 150.031 seconds | unknown / unknown | 31,967 |
| `678788972eaf4ff6b179b90ea623c190` | Accepted architect: read `extension.js`, line 24 | 5,321 / 232 | 5,553 |
| `4d7fd5a40e1f4118b39e965a490dd106` | Accepted architect: read line 124, column 16 | 4,836 / 262 | 5,098 |
| `2ffe49cb48894e13a18826cea8c98fd9` | Accepted architect: read line 210, column 11 | 4,504 / 262 | 4,766 |
| `ebe69325da8f4828956b2d0c17a81608` | Accepted parent draft; publication withheld later | 2,537 / 1,455 | 3,992 |

The read-only observer verified unchanged invocation and receipt hashes, settlement amounts and ordering for the original 104,960-unit prefix. It also verified the older task prefixes at 135,642, 58,473 and 199,496 units unchanged. No task reset, receipt replacement or spending refund was used.

## What the source interface actually delivered

Selection version 2 supplied real snapshot-bound function addresses and the new governed read profile: **8,096 bytes / 256 lines** per read, split into source units no larger than 1,200 bytes. The first selector received 2,169 bytes of addresses, including `extension.activate` at line 24, `request` at 215 and `invoke` at 955. Supplied column and end-position metadata preserved mid-line locations. These addresses were an improvement over the name-only 0.51.3 overview.

After the two timeouts and packet shrinking, three selectors completed. They requested only sequential reads of `extension.js`; no discovery search, list operation or second source file was selected. Their completed repository receipts retained **18,937 bytes**:

| Read receipt | Source bytes retained by the host | Next fresh cursor |
| --- | ---: | --- |
| `01e7d48220c979c73c4a57dd53e2de2f22677421ed12b8918ae5a9fc70eccf23` | 6,762 | line 124, column 16 |
| `571bf0e5a967f4deb1660e279707a046f9f8df1abb848e40d095c8193783aa9f` | 6,300 | line 210, column 11 |
| `8f81cd10d7eb2b026e18d00bf7dfa85fea6d97ef3b44c44f25e85d7b37232814` | 5,875 | line 270, column 28 |

Retaining a repository receipt did not mean the next model saw its source. The model-facing working set retired most of the paid read before a selector could use it:

- The line-124 decision received only **880 bytes** from the preceding 6,762-byte read, covering its final source units.
- The line-210 decision received only **11 bytes** from the preceding 6,300-byte read: `      panel`, with an exclusive end at line 210, column 11. Its original excerpts added only 53 bytes.
- The symbol window had also shrunk to 512 bytes, retaining only `extension.activate`; the earlier `request` and `invoke` addresses were absent from those later calls. Continuing the cursor therefore does not prove that the model ignored addresses that were no longer supplied.

Meanwhile, every selector still received the full **391-path catalog**, approximately **11,591 bytes** in canonical form. In the late, source-starved selector this occupied about **62.5% of model-facing text bytes**. This is a prompt-composition measurement, not a token-usage percentage; JSON transport escaping changes the byte denominator. Full host scope authorization and the size of the model's lookup index are separate concerns.

Discovery stopped with `stop_reason: budget_boundary`, three accepted selector decisions and three completed reads. This was the discovery stage's protected budget boundary, not evidence that the entire 250,000-unit task allocation had been spent. The parent received **1,213 bytes**, all from `extension.js` lines 24–40, and no child reports. Its source body was about 6.4% of the bytes retained by the three new read receipts.

The actual parent wire also supplied two exact static call statements: the `tierReasoning(config)` lookup at line 20 and `if(manual)return manageStrata('load-selected');` at line 60. These support those local source relationships. They do not establish a native request path, runtime execution or the behavior of Python files absent from the supplied source. Dropped host graph facts were excluded from this quality assessment.

## Manual assessment of the saved parent draft

The parent source supports activation, reading `runtime-integrity.json`, checking the canonical-kernel hash metadata, rejecting absolute or parent-traversal integrity paths, comparing file hashes and requiring the named local UI/provider modules. The draft correctly recognized some of this bootstrap behavior.

It did not explain the requested tools architecture. Five Python components were assigned responsibilities while every component's supporting path was `extension.js`. The draft proposed that the Python workflow begins with `harness/analyze.py`; that file is an experiment/audit CLI, rather than the native chat orchestrator. It speculated about dispatch through a native provider without showing the actual caller/callee connection. It also described the integrity check as preventing unauthorized runtime modifications, despite the supplied comment explicitly limiting it to a consistency check and excluding protection against replacement of the entry point. Unresolved static calls were presented as possible missing dependencies or incomplete code without source establishing that diagnosis.

| Required mechanism | Assessment of delivered evidence and accepted draft |
| --- | --- |
| Native chat → orchestration flow | Activation/module imports only; native provider → bridge → flow handling was not explained. |
| Host permission, branch and join authority | No supporting implementation body or mechanism explanation. |
| Gateway registration and finalization | No supporting implementation body or mechanism explanation. |
| Adapter → boundary → canonical kernel | Kernel integrity metadata was visible; proposal admission and canonical-kernel evaluation were not explained. |
| Scoped repository execution → receipt | Three real read receipts existed in the host ledger; the draft did not explain their authorization, execution or receipt path. |
| Memory and checkpoint continuity | Host recovery preserved the paid task; the model was not given and did not explain the relevant memory/checkpoint implementation. |
| Parent publication and source claims | The host withheld publication; the draft did not explain the claim-review/publication mechanism. |

The draft contained an ordered flow, but its later steps were speculative and its proposed next checks requested the Python source still needed to answer the original question. No published report, request-flow diagram or local-dependency diagram resulted from this native trial. Browser fixture coverage of those views is separate from actual report usefulness.

## Publication failure and integrity limits

The parent candidate was durably saved at `PARENT_REVIEW_CHECKPOINT`. The next stage failed during reviewer identity selection because `strata:high` was unavailable after the owned High runtime had been stopped. That unavailable identity aborted reviewer selection before a claim-review invocation was registered. There was no rejected claim verdict, claim timeout or completed factual review to attribute to this trial. `WORKFLOW_FINISHED` recorded `observation_delivery: withheld` at `1795135855.924077`.

The financial and physical admission boundaries remained in force. Calls used the governed packet/reserve checks; the first selector's 31,918-unit reservation fit its 32,384-unit profile, and the parent's 14,257 input reservation plus 7,304 output reserve fit its 21,912-unit profile. The accepted parent settled to 3,992 measured units and released its unused reservation. These reservation units must not be relabeled as measured physical input tokens. The Mid calls did not carry the newer exact physical-token proof field; a historical High proof shown by the observer is not evidence for these new Mid calls. No context limit, memory-admission threshold, permission gate or kernel rule was bypassed.

Those checks establish accounting and admission behavior for the observed calls. They do not establish architecture completeness or semantic correctness. In this trial, host packet composition discarded useful source while retaining a large catalog, and the accepted draft remained inadequate even before the independent reviewer-availability failure.

## Frozen validation and local evidence

Before the native trial, all **26 automated QA gates** passed: **1,393 Python tests**, eight skipped, plus Node, browser and package checks. These are automated results, not a native report-quality pass.

- Package: `dist/Sutra-0.51.4.vsix`, 5,056,684 bytes; SHA-256 `e581800656961471c04c5e3e3a55dc4820c2ef5b238e658f09ee8d96b53c2ef0`.
- Canonical STP kernel SHA-256: `9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`, unchanged.
- Native marker: `/private/tmp/sutra0514-ui-start.json`.
- Final native accessibility capture: `/private/tmp/sutra0514-final-ui.txt`.
- Read-only observation and receipt-prefix baseline: `/private/tmp/sutra0514-observer.jsonl` and `/private/tmp/sutra0514-baseline.json`.
- Automated log and browser artifacts: `/private/tmp/sutra0514-qa.log` and `/private/tmp/sutra0514-qa-ui/`.

These temporary artifacts record the trial; they are not report publication or model authority. The scoped follow-up is to retain the latest earned source page during packet fitting, bound the model-facing catalog independently of host authorization, and treat unavailable reviewer identities as a recoverable selection/pause condition. A further native trial must establish whether those changes produce a useful, source-supported architecture report and diagrams.

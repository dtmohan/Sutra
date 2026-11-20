# Native VS Code review: 0.50.5–0.50.6

Date: 2026-11-19. Real signed-in VS Code window, existing chat and saved
workspace review. Prompts were submitted through native chat controls, not the
Python bridge or a model API. Backend observation was read-only.

Request: “can you review the workspace project and go through the tools logic
and help me understand the architecture in detail? a few block diagrams would
be great”

## Observed failures

- The earlier 0.50.4 deadline included a macOS idle-sleep interval. Request-scoped
  idle-sleep protection now leaves display sleep and explicit sleep available.
- A useful child checkpoint had previously been rejected solely for five
  informational follow-ups. Remaining lists now retain all entries within the
  existing individual, working-note, response and transport bounds.
- In 0.50.5, two Strata structured-output failures returned no measured usage.
  Their full reservations, 30,792 and 30,939, remained charged. The parent was
  waiting for settled children, but generic allocation recovery refused it as
  unfinished execution.
- In 0.50.6, the verified Strata prompt-and-validate path delivered usage before
  host parsing. Three attempts measured 6,428, 7,276 and 6,579 total tokens.
  Their errors were an invalid JSON start, a missing delimiter, then a valid
  findings object lacking the required child report.
- The third response contained architecture findings, four fresh citations and
  three concrete next checks. Its only rejection was the completed child-report
  requirement. No claim is made that its conclusions were semantically verified.
- Native Continue allocated one bounded replacement. Auto selected Mid, whose
  host memory/context cap was 4,389. Its mandatory packet could not fit the
  conservative byte-based accounting check. This was not measured token-window
  exhaustion and did not establish that the model's advertised context was small.

## Recovery evidence

The policy-upgrade confirmation restarted the original request in the same chat.
The session, contract, conversation and workspace grant were retained. Previous
spending remained 61,731, and the linked execution received 188,269 from the
250,000 original allocation. No failed-call reservation was refunded.

The child token-recovery tests cover additive amendment, unchanged siblings,
accepted checkpoints, reload, another model, a legacy unclosed run and a resource
pause during repair. Independent review caught and corrected retry-slot and
output-repair-flag resets before packaging.

## Validation and remaining acceptance

0.50.6 passed 24/24 package gates and 1,226 Python tests with eight environment
skips. This did **not** establish useful end-to-end publication: the native review
still paused as described above.

0.50.7 separates child findings from parent report presentation and removes the
parent introduction-length limit from child answers. The parent still requires
its report and source evidence. Final native publication and diagram quality must
be observed separately; a protocol success badge or unit test is insufficient.

Known context-planning limitation: calibration coverage is in actual model-token
units while conservative packet reservations use UTF-8 bytes plus an output-token
reserve. These are deliberately conservative bounds, not equivalent measurements.
Exact model tokenization and warm incremental memory measurements remain separate
work; memory gates were not weakened during these fixes.

The canonical STP kernel remains SHA-256
`9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`.

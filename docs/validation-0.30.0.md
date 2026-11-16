# Sutra 0.30.0 validation

Date: 2026-11-15. Host: this Apple Silicon Mac, Python 3.9, bundled Node and
TypeScript, Chrome for the webview tests, local Ollama models. User task ledgers
were not modified. Live tests used separate storage and fresh source reads.

## Release gates

`scripts/qa.sh --with-package --browser`: **13 passed, 0 failed**.
The Python suite ran **645 tests**, all passing. Node contracts, browser webviews,
runtime integrity, packaging and packaged-content checks passed. The new monitor
accounting display was rendered and visually inspected; the narrow layout also
passed the browser check.

Canonical kernel SHA-256 remains:
`9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`.

The 13 new tests in `harness/test_model_packet.py` cover actual HTTP packet
projection, single-use packet binding, source-text preservation, rejection of
model-supplied host fields, exact memory and retrieval IDs, optional static graph
omissions, completed/missing/invalid usage, overrun accounting, historical charges,
source-change quarantine, reloads, child/ancestor accounting, interruption recovery
and model switching. Existing permission and action suites remain unchanged
except three historical transport stubs explicitly identifying their legacy format.

## Live model switches

`test/live-model-boundary.py` made three fresh calls:

| Model | Result | Elapsed |
| --- | --- | --- |
| qwen3:4b | Acknowledged Cedar / CED724 | 2.42 s |
| local-assistant:latest | Applied correction to CED725 and listed all restrictions | 11.88 s |
| qwen3:4b | Recalled Cedar / CED725 and every restriction | 3.56 s |

The ledger was reopened between turns. No edits, tests, external-network use or
new dependencies were granted; the kernel-unchanged instruction remained in the
saved context. Contract identity and limits were unchanged, and receipt verification
passed. Across the three calls, **44,721 units were reserved; 5,261 measured tokens
were charged** after completion. The released amount was unused reservation,
not a refund of work. No workspace tool operation was dispatched in this test.

## Architecture review and tight budgets

The established six-module regression completed at its normal allocation in
**42.01 s**, using qwen3:4b for both draft and report review. The draft received
**2,820 source-excerpt bytes**. It used 2,644 input / 912 output tokens; its reviewer
used 2,537 input / 1,434 output. Total task charge was **7,527 tokens**. The two
initial reservations totalled 49,327 units. The report and receipts were retained.

A separate task with only **21,025 budget units** exercised normal automatic
allocation and an actual reviewer-model switch:

- Draft: qwen3:4b, 10,512 context allocation, 2,628 output reserve. Input reserve
  7,435 plus output reserve was admitted; completed usage was 796 in / 682 out.
- Reviewer: local-assistant, reusing the remaining allocation after settlement.
  Completed usage was 1,632 in / 553 out. Contract and source bindings held.
- Total consumed charge: **3,663**, including the withheld reviewer output.
- Publication was correctly withheld because a supported recommendation lacked
  the required source quote. An earlier run was withheld for quotes copied from
  static facts rather than the provided source excerpts. The model-facing guide
  now states that distinction explicitly; validation was not weakened.

Before the optional static graph sample was paged, this tight test stopped before
inference. After paging, both model calls were admitted. Omitted call/node/diagnostic
counts are explicit, and the complete graph remains in the host record.

An offline comparison using the saved 0.29.1 failed packet reduced its conservative
input reservation from 15,674 to 8,347 units (46.7%), including disclosed omission
of optional static details. Mandatory conversation and requirements stayed intact.
**That exact historical packet still does not fit a 10,512 allocation with 2,628
reserved for output.** This release does not reset historical spending or promise
that every previously paused packet will fit unchanged.

## Practical limits

These tests establish the invocation boundary, host-enforced permissions,
accounting and selected continuity cases. They do not establish general model
competence. The normal review's same-weight checker accepted unsupported claims,
including treating unresolved static calls as dependency gaps and diagnostic
counts as runtime-error evidence. Reviewer agreement and citation presence remain
unverified opinions about prose; the report is not a certified architecture audit.

Admission still uses conservative UTF-8 sizing with schema/template allowances.
It is not an exact tokenizer for arbitrary models. This release was not run on
Windows or Linux or installed into the user's VS Code session as part of this task.

# Sutra 0.34.0 validation

The canonical STP kernel remains unchanged, SHA-256 `9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`.

Release validation: 687 Python tests and all 14 release gates passed, including Node contracts, browser webviews, runtime integrity and packaged contents.

Focused regressions cover immutable execution allowances, cumulative parent spending, retry/reload recovery, low-to-high model switching, retained Cedar/CED724 memory and constraints, unrelated requests while paused, explicit allocation successors, unchanged child grants, interrupted reservations, fresh-source paging, no useful page, and source changes after planning. Existing tests also exercise action and tool permission boundaries.

## Saved-task validation

A copy of the user's paused review database began with 66,943,064 source bytes spent out of 67,108,864, leaving 165,800 bytes. A fresh catalog and selected source page mapped 22 files using 165,714 bytes. The task contract, prior spending and total allocation were preserved; no amendment was made and no model inference was needed for this read-path check. The original database was not modified. This confirms the previously failing source gate can admit a smaller useful page; it is not a claim that the whole historical review was completed.

## Live local-model validation

`test/live-execution-sessions.py` runs in an isolated workspace with a 10 KiB source allowance. It routes through `qwen3:4b`, saves a host pause before synthesis, reloads, resumes with `qwen3.6:35b`, and completes a reviewed, bounded response. The verifier checks contract and pinned-memory retention, cumulative accounting, fresh evidence, closed execution state, receipt integrity and unchanged source files.

In the first run all model work completed; the validation script's SQL lookup then failed because the role is stored in invocation JSON, not a separate column. The lookup was corrected and the retained receipts were verified without rerunning inference. Workspace spending rose from 7 to 8 operations, 0 to 2 calls, 0 to 4,122 model units, and 7,964 to 9,955 source bytes. One fresh file was selected and three deferred. All model/workspace constraints remained intact and the allocation version stayed zero. Artifacts are outside the source package under `Sutra 0.34.0 validation/`.

These are bounded fixture and saved-state checks. They do not establish answer completeness or semantic correctness across arbitrary repositories or models.

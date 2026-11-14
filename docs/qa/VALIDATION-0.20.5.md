# Sutra 0.20.5: confirmation feedback and stale mapping failures

## Confirmed cause

The reported workspace had 113 supported source files, 84 mapped and 29 failures
saved by the old parser. The failure queue only compared source hashes. Updating
the parser did not retry unchanged files, leaving the review paused even though
the corrected parser could map them.

Failures now carry a mapping-environment binding: policy, contract, page
allocation, Python version and trusted Node/TypeScript identity. Missing legacy
bindings or a changed environment put failed files back in the queue once. An
audit event records the requeue; old receipts and consumed budget remain. Errors
under an unchanged environment stay bounded, and temporary resource waits no
longer poison a file's persistent failure entry.

## Permission feedback

The native provider calls VS Code's modal confirmation API and awaits its result.
The previous diagnostics did not record the intermediate confirmation outcome,
so they cannot establish why the user did not see the earlier dialog. The UI
test below verified that a real dialog appears and both buttons work.

The chat now explicitly distinguishes waiting for the dialog, access granted,
access declined, and an existing armed grant. Routing diagnostics record the
requested, confirmed, granted, declined or cancelled state with a confirmation
identifier and timestamps. Sutra Monitor displays the last permission feedback.
No default approval, timeout approval or broader file grant was added.

## Validation

- Python 3.12: 430 tests passed; Python 3.9: 13 automatic-review tests passed.
- Native-provider suite: 35 journeys, including pending and declined dialogs
  blocking workspace dispatch, delayed approval after cancellation, and model
  switches retaining an already armed task.
- All 13 release gates passed, including browser checks, integrity mutation
  checks, packaging and activation of the extracted VSIX in isolated VS Code.
- A second isolated VS Code profile exercised real, unmocked confirmation
  dialogs. Cancel left access unarmed; Arm Sutra enabled it; repeated arming
  reused the grant without another dialog; Disarm cleared it. Arming itself
  created no task and made no model call. Screenshots were inspected.

A read-only SQLite backup and copied metadata from the affected workspace were
used for a full recovery run. The original live ledger was not modified. The
real state machine, fresh source readers, deterministic parsers and invocation
gateway completed all 113 files and two children using small/large model
fixtures. All 29 legacy failures cleared. The contract and read-only/network-off
permissions remained unchanged; invocation and audit checks passed.

| Allocation used | Before recovery | After fixture review |
| --- | ---: | ---: |
| Repository operations | 381 | 388 |
| Workspace model calls | 4 | 7 |
| Charged tokens | 55,459 | 99,837 |
| Source bytes | 44,961,828 | 47,261,178 |

No budget was refunded and no incompatible old worker response was reused.
These are protocol and recovery tests using controlled responses, not live LLM
inference or an evaluation of architecture-answer quality.

The STP kernel is unchanged:
`9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`.

## Applying the update

Reload the affected VS Code window after installing 0.20.5. Send **continue** in
the existing Sutra chat and select **Restart review** for a saved old-policy
workflow. Confirm the **Arm Sutra** dialog when workspace access is unarmed.
The original request and task permissions remain; the 29 old failures are retried
under the updated parser within the remaining task allocation.

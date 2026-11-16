# Sutra 0.29.0 validation

Validated on the development Mac with Python 3.9, the installed Ollama 0.40.16,
qwen3:4b and local-assistant:latest. No user settings, source workspace, installed
extension or task ledger was changed by the live tests.

## Automated checks

- All 13 `scripts/qa.sh --with-package --browser` gates passed, including 621
  Python tests, Node contract tests, browser webviews and package integrity.
- After that run, one additional coding-fallback regression was added. All seven
  `test_resource_routing` tests passed, for 622 distinct passing Python tests.
  The added test retains the exact file grant, denies network/tests/dependencies,
  charges the replacement call and requires confirmation before applying an edit.
- Warm-memory tests reject expired, mismatched, undersized and missing resident
  entries. Disappearing residency and changed manifests stop before inference.
- Fallback tests cover high → mid → low, two-switch exhaustion across Resume,
  explicit overrides, duplicate model/weight aliases, OS pressure, spent budget,
  completed child preservation and original context after reopening the ledger.
- Native-chat contract tests confirm fallback progress continues in the same
  request. Browser checks cover the cold/credit breakdown, recorded fallback,
  plain-text escaping and narrow layouts; the rendered screenshot was inspected.

## Live local-model sequence

An isolated conversation ran qwen3:4b → local-assistant:latest → qwen3:4b with the
ledger closed and reopened between turns. Both recall answers retained project
`Cedar-724`, the unchanged STP kernel requirement and the prohibitions on network,
edits and tests. Every dispatch retained the same contract; call/token charges
grew rather than resetting.

The large model was preloaded only after its cold plan was admitted. At 16,384
context tokens, the calibrated cold estimate was **31.23 GiB**. Matching residency
credited **16.91 GiB** of weights, producing **14.32 GiB** additional reservation.
The full **6.25 GiB** calibration margin and separate **4 GiB** system reserve
remained. The subsequent real invocation's dispatch receipt recorded warm credit.
This verifies the adapter path, not a measured peak-memory bound.

For workspace fallback, one large-model capacity refusal was injected before
inference, without creating real memory pressure. The central workflow recorded
a switch to qwen3:4b, reopened its ledger, paged fresh source and completed a real
review. An independent local-assistant review supported 9 of 12 claim groups;
3 were omitted. That agreement remains a model opinion, not correctness proof.
The pinned contract stayed identical, no edit/test/network permission was added,
and consumed source bytes grew from 204 to 306. The worker and reviewer consumed
2 calls and 45,984 conservative budget units, with no refund or limit reset.

Two earlier live-test attempts exposed test setup issues: a conversation was
checked using a workspace-only contract accessor, and a 4K preload could not fit
mandatory retained context. The final test uses the correct conversation contract
and an admitted 16K preload. The 4K refusal correctly dropped no constraints.

## Upgrade and artifacts

A read-only SQLite backup of the current installed conversation retained all
6 saved turns and the same session/contract under the new policy. Old executable
receipts stayed withheld. This migration test used a fake provider, not inference,
and did not modify the original ledger. Policy-bound workspace reviews still use
the existing explicit restart path with consumed allocations retained.

The runtime integrity gate and packaged-byte check confirm the canonical kernel
is unchanged:

`9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`

The release includes a VSIX and companion source archive. Installation and GitHub
publishing were not part of this validation. Live inference was tested on this
Mac only; no live Windows/Linux memory measurement is claimed.

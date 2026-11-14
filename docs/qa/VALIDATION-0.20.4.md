# Sutra 0.20.4: child paging and upgrade recovery

The reported failure was confirmed in the local workspace ledger: one child was
assigned 65 files totaling 529,426 bytes against a 524,288-byte source-page
allocation. Splitting the repository by file count did not ensure that each
child fit in one page. The subsequent extension upgrade changed the policy hash,
so reusing the saved orchestration commitment was correctly rejected, but without
an actionable recovery path.

## Changes

- A child can now consume multiple fresh, receipted source pages within its
  existing allocation. Each operation is governed and charged separately. Source
  hashes, page ownership, the full contract and the assigned file union are
  checked before invocation and again before joining results.
- Compiler configuration accompanies each relevant page and is not counted as
  an independent code-review partition. Parser work-unit overflow can split a
  page further; failed operations remain charged. An individual file that cannot
  fit still stops with a specific allocation message.
- Scope checks now use canonical path ordering. The full-repository validation
  exposed an additional bug where canonical JSON reordered excerpt keys and
  falsely rejected an otherwise unchanged child during receipt reload.
- A saved workflow with an old policy is detected before arming/resuming. An
  explicit **Restart review** choice preserves the exact original request, task
  contract and consumed budget, releases unused child reservations, and rereads
  source under the current policy. It does not reuse incompatible worker results.
  Contract or calibration changes cannot use this policy-only recovery path.
- Recovery stays in the existing native chat; declining it retains the saved
  workflow. Sutra Monitor explains the recovery condition.

The canonical STP kernel remains unchanged, SHA-256
`9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`.

## Validation

- Python 3.12: 427 tests passed in the final full release suite.
- Python 3.9: 29 orchestration tests passed after the final runtime change.
- Native provider: 34 journeys, including acceptance and rejection of upgrade
  recovery without opening another chat or losing the original request.
- All 13 release gates passed, including the browser suites, runtime-integrity
  mutation checks, packaged-content validation and isolated VS Code activation.
- The packaged extension read real AppleSMC temperatures, discovered installed
  model metadata and mapped fresh fixture source in the isolated VS Code host.

The full current Sutra repository was also reviewed through the real state
machine, deterministic parsers, invocation gateway and ledger using controlled
model responses. It mapped 134 supported source files and completed two children:

| Child | Model fixture | Files | Fresh source-page bytes |
| --- | --- | ---: | --- |
| 1 | small | 67 | 520,380 |
| 2 | large | 67 | 521,631; 44,067 |

Every page stayed below the existing 524,288-byte allocation. The model changed
between children; the task contract, read-only/network-off permissions and a
pinned user fact remained intact. Invocation and audit verification passed.
The task consumed 11 repository operations and 3 workspace model-fixture calls;
none of this use was refunded.

The first full-source test run lacked the explicit trusted Node executable and
stopped in mapping; the corrected environment then exposed the excerpt-ordering
bug above. The final run passed after that fix. No live user ledger was reset or
edited during validation.

No actual LLM inference was run for this update. These checks establish paging,
recovery and protocol behavior, not architecture-answer quality. The source map
remains conservative: combining pages does not invent relationships between
files parsed on different pages, and model excerpts are bounded samples.

## Upgrade recovery

After reloading VS Code, send **continue** in the existing Sutra chat. If an
incompatible saved review remains, select **Restart review**. If its task contract
or calibration also changed, use **Discard** and submit a new request. Existing
receipts and spent task budgets remain recorded. No automatic replay of edits or
tests is introduced.

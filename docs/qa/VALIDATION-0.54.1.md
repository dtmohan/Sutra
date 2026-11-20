# Sutra 0.54.1 validation

Date: 2026-11-20. Scope: saved child review source selection, financial allocation
recovery, and bounded partial handoff. The canonical STP kernel and configured
model tiers are unchanged.

## Reproduced cause

The saved GraphEngine review had 16 accepted checkpoints in one six-file child
and 15,672 remaining financial reservation units. Its child repeatedly requested
`get_natal_positions` from `ephemeris.py`, but the legacy selector retained the
filename while losing the bare symbol. Its source pages passed the declaration
without delivering the complete body.

Geometric shrinking then produced an empty source page. The old recovery
measurement priced that empty packet below the remaining allowance, so no
measured task-allocation request reached the workspace allocator. Chat suggested
a larger context window despite not establishing physical token overflow.

## Corrected behavior

- Recipe 4 resolves the requested symbol only within the child's authorized
  static graph and pages the requested declaration body. Recipes 1–3 remain
  unchanged for historical accepted deliveries.
- Recovery measures a source-bearing packet. A coherent requested declaration
  is preferred; a whole-line floor handles bounded fallback. An empty page does
  not establish that the model has inspected or understood the source.
- Verified, unchanged paid source receipts are reused before sizing. A base-only
  grant cannot consume a recovery attempt before the full cached packet is sized.
- A financial shortage reaches the existing workspace allocation policy with
  the same child and execution. Hardware and exact-token-window checks retain
  their separate admission authority. Missing token counters are measurement
  waits; physical overflow cannot justify financial capacity.
- Four unchanged checks can trigger a bounded partial handoff only after their
  requested declaration bodies were supplied. A timestamped, hash-chained kernel
  BRIDGE records that source-acquisition decision. The parent retains unresolved
  checks; neither branch closure nor the model's reply certifies semantic truth.
- Chat distinguishes source selection, financial reservation, host sizing and
  measured physical context failures. Larger-model advice requires the latter.

## Actual saved-case replay

The original user store was left untouched. Its checkpoint receipts were
validated using the released runtime that issued them; the new pure fitter was
then applied to that verified data in a private replay. No old executable policy
hash was replaced or forged.

| Packet | Source | Reservation units | Additional units |
| --- | --- | ---: | ---: |
| Legacy source-bearing whole-line floor | 185 bytes | 16,403 | 731 |
| Recipe 4 coherent requested body | `ephemeris.py` lines 145–169, 894 bytes | 16,693 | 1,021 |

The coherent packet refits exactly at the measured allowance with all 16 notes
and the full contract retained. No larger context is required for that packet.
These are conservative reservation units, not measured model tokens.

The user store already contained two automatic call amendments. The current
policy permits at most 12 such amendments beneath its workspace ceilings; there
is no two-amendment ceiling. Additional calls still require ordinary measured
quantitative recovery and spending remains charged.

## Validation

- Focused recovery suite: 98 tests passed.
- Native-provider suite: 93 checks passed, including financial proof precedence
  over old source-error wording and distinct physical/source/unknown guidance.
- Saved-source regressions cover 16 retained notes, exact refitting, historical
  recipe compatibility, scoped symbol resolution and whole-body selection.
- Orchestration recovery tests apply measured capacity in place, reopen the
  ledger and switch models while retaining the original child, sibling, cursor,
  contract, receipts and past spending. Unchanged source is not reacquired.
- Exact-window tests distinguish UTF-8 reservations from physical tokens and
  reject missing/altered counter observations and actual physical overflow.
- Handoff tests verify bounded partial scope, parent joining and remaining
  unverified checks. An additional temporary-ledger probe reopens the ledger and
  switches Low to High; old accepted deliveries still verify after child DONE.
- A denied full-packet sizing probe leaves the child READY with the same cursor,
  limits and spending, and commits no handoff marker or model invocation.
- The four-test orchestration suite also passes independently with a closing-call
  regression added after full-suite discovery. It creates two real prior automatic
  call grants, reaches a fractional child-call boundary, automatically grants two
  calls without dispatching, and then executes the partial handoff once and joins
  the children. Its scaled 4→5→6 sequence models the saved case's 16→17→18 boundary;
  it does not claim to execute the original user artifact through a live model.

Full release validation passed all **33 gates** with
`scripts/qa.sh --with-package --browser --vscode`:

- Python harness: **1,761 tests**, 8 skipped, 469.154 seconds, passed. The added
  closing-call regression also passed in the independent four-test suite above.
- Native provider: **93 checks** passed.
- Coordinator backend: **16 scenarios** passed; client: **8 checks** passed.
- Browser webviews, release metadata, runtime integrity and packaged contents
  passed.
- Packaged VS Code host: **23 checks** passed in an isolated profile, including
  real source operations through the independent coordinator, real AppleSMC
  telemetry, contract/allocation preservation and permission revocation.
- Whitespace checks passed.

Artifact: `dist/Sutra-0.54.1.vsix`, 5,163,867 bytes, 297 runtime files.
VSIX SHA-256:
`10b18638f42887c532a46dafa21775b47383264254301d798be63464011ed3b3`.

## Limits of this validation

The actual old user review was replayed for selection and sizing, not resumed
through a live model to publication. Deterministic providers test allocation,
checkpoint and model-switch behavior; they do not measure answer quality.

Installing a changed policy uses Sutra's existing policy-recovery path rather
than executing an old plan under a forged current policy. Historical receipts
remain auditable and conversation data is supported by the released-policy
allowlist; the replay is not proof of an in-place executable-policy migration.

Canonical kernel SHA-256:
`9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`.

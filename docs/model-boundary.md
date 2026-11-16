# Task-facing model interface and host accounting

Sutra 0.30.0 separates two representations at the invocation gateway. Both are
versioned and bound to the admitted request; neither can widen user authority.

| Representation | Contents | Consumer |
| --- | --- | --- |
| Host envelope | Full contract, epochs, hashes, freshness receipts, model identity, reasoning control, resource gates, task and ancestor allocations | Gateway, validators, unchanged STP adapters/kernel |
| Model packet | Goal and current request; relevant requirements; fresh source/static evidence; exact recent turns, pinned user statements and selected older pages; available action choices | Local model |
| Model output | Answer, evidence citations, action/page requests and optional unverified assessment | Host role validators |
| Bound result | Validated model output plus explicitly labelled host identity/constraint bindings | Existing workflow and immutable receipts |

The model does not echo budget limits, contract hashes, permission receipts or
resource decisions. Requirements such as scope, no network, no new dependencies,
read-only work and preserving an API remain visible because they affect the work.
Unknown user requirements are retained. Tool feedback keeps source paging cursors;
these are addresses to use, not permission grants. Source and user text are never
filtered by replacing words that happen to resemble metadata names.

The model-facing schema omits host-bound fields. Returning one anyway fails raw
schema validation, even if its value happens to match. The gateway then attaches
the current task identity and constraints, records both raw and bound response
hashes, and runs existing role, source, contract, action and permission checks.
A host-bound `complete=false` means that this role cannot certify completion;
it is not an assessment attributed to the model. Task self-assessments remain
observe-only and cannot authorize actions or release a rejected answer.

## Capacity, reservations and spending

Admission charges the same projected messages used by the transport. Its
breakdown is serialized message UTF-8 bytes, a conservative strict-schema
allowance, template allowance and reserved output. The schema allowance is
explicitly separate: the runtime may use a grammar rather than prompt text.
This is still an upper-bound estimate for the tested tokenizer family, not an
exact token count. The MLX decoder compatibility layer can reduce wire-schema
string bounds; strict host validation and the conservative allowance remain.

Before dispatch, task and ancestor ledgers reserve the entire amount. A single-use
permit binds the full host request and the projected packet hash. On completion:

- Valid completed backend input/output counts become the consumed token charge.
  Unused reservation is released; used tokens, calls and elapsed time remain spent.
- Completed malformed, rejected or output-truncated generations still consume
  their reported usage. Rejection is not a refund.
- Missing, invalid or incomplete measurements retain the full reservation.
  Reconciliation of an interrupted process never replays or refunds it.
- Any reported overrun is recorded and fully charged, rather than clamped away.
  Subsequent admission sees that spending.
- Historical envelopes retain the original conservative charge. Reloads, model
  switches, permission changes and compatible conversation restoration do not
  replenish allocations.

Task usage and child/ancestor settlements read the same receipt-derived charge.
The kernel still makes the same four decisions over adapter measurements; its
source and thresholds were not changed. The monitor displays input/output
reservations separately from the settled task charge and its measurement basis.

## Context retention and faults

Projection retains exact latest turns, pinned user statements, explicitly requested
older pages, and source text. Hashes and provenance stay in the ledger. Mandatory
memory is packed first. TOC addresses get space before optional older dialogue,
so optional page-ins do not accidentally remove every way to request a page miss.
The optional static call sample shrinks with the allocated module-map size, keeping
call/node references together and reporting omitted sample counts. Full facts stay
in the host ledger; an omitted call is not labelled unresolved.
A packet that cannot hold mandatory context still stops honestly. This change does
not discard constraints to fit, inflate context capacity, or reset a saved budget.

Receipt verification distinguishes host binding from model acknowledgement.
Compatible 0.29.1 conversation receipts can restore data only; stale executable
proposals and workspace checkpoints do not become current merely through upgrade.

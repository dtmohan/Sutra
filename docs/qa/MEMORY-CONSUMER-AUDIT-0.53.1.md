# Memory consumer audit — Sutra 0.53.1

Date: 2026-11-20. Baseline checkout: `bc74250` plus the tested, uncommitted
0.53.0 changes. The original user ledger and the prior C4 experiment were
inspected only; neither spending nor old receipt policies were rewritten.

## Confirmed gap

Retention and model delivery are different operations. The previous architecture
summary selected two short source excerpts and omitted the discovery journal,
its linked contents index and earlier source spans. The data remained in the
receipt store. This was a consumer integration defect, not a demonstrated context
overflow, RAM eviction or loss of the pinned contract.

The shared retained-context profile now binds a journal prefix to its session,
task/execution lineage, request, conversation, workspace, contract, permissions,
calibration and host policy. Immutable audit anchors bind the directory. Working
notes are labelled unverified. Source leaves retain exact receipt and span
identities, including nonadjacent reads from one file. Freshness and quarantine
checks apply before recall; a contents entry does not grant file access.

Packing keeps whole units. A small window reports deferred counts and retains a
nested directory. Explicit recall maps a verified earlier reference to the
current compatible execution and pins the requested whole unit. It adds no
repository effect, grant, refund or budget reset. Another inference still needs
the normal context, hardware, allocation and invocation gates.
Accepted recall requests also survive a bridge crash before the flow checkpoint
is saved; a later normal accepted result supersedes an older request. Impossible
or already-delivered recalls are rejected before becoming pending work.

## Consumer audit

The AST inventory classifies 17 registered roles across 14 production call
origins. It fails when a registered role lacks a declared context profile or a
call origin cannot be resolved. This proves inventory coverage only; wire-level
regressions separately test the integrated consumers.

| Consumer | Finding and resulting scope |
| --- | --- |
| Discovery → parent writer | Fixed: linked notes and all selected source units survive the handoff. Optional-window retirement leaves explicit deferred references. |
| Architecture report | Fixed: shared journal instead of fixed 700-byte excerpts; minimum source coverage remains mandatory. Exact quote IDs are restored by the host and verified against source. |
| Native coder/manual reviewer | Fixed: verified conversation working set, explicit source selection beyond the old four-page ranking cutoff, and a paginated source directory with total and next offset. |
| Document extraction | Fixed: prior user criteria and pins accompany each exact section; conversation text cannot satisfy the section quotation gate. |
| Conversation history | Existing receipt-bound working set retained. Eligible history remains scoped to the current document/task; no claim that all ancestral turns are relevant. |
| Child review | Existing branch checkpoints, parent task and source selection remain scoped. This change does not inject unrelated parent discovery reasoning into a child. |
| Claim/document/API reviewers | Intentionally narrow independent evidence views. Retained source spans enter claim evidence; writer notes do not become verdict evidence. |
| Source-navigation, API/static, parser, selector and calibration roles | Explicit bounded source/input profiles. A stateless or source-only call is not automatically a memory defect. |
| Memory diagnostics | Fixed: versioned delivery fingerprints come from the final model projection, including retained units and document/native context. Diagnostic failure is reported and cannot reject successful inference or alter spending. |

## Remaining limitations

- Full document-section re-extraction is a separate navigation feature. Prior
  criteria now reach extraction, but synthesis still consumes bounded extracted
  evidence rather than a general tool for reopening arbitrary document sections.
- Conversation history uses its existing bounded turn directory; this release
  does not replace that directory with the workspace journal's nested graph.
- A whole requested page that cannot fit produces a capacity fault. Recall does
  not override model, hardware or authorized allocation limits.
- Delivery fingerprints describe visible semantic units, not physical RAM
  residency, tokenizer attention, understanding or answer correctness.
- Source addresses authenticate provenance. Independent claim review still
  judges whether the selected source actually supports the claim.

Validation details and live observations are recorded separately in
`VALIDATION-0.53.1.md`. The canonical STP kernel is unchanged.

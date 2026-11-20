# Native VS Code architecture review: 0.51.0

Date: 2026-11-19. Existing signed-in VS Code chat, Sutra Auto, original
architecture-and-tools prompt. The request was submitted with native chat
controls. Ledger and provider observations were read-only. Configured Sutra
tiers and the canonical STP kernel were unchanged.

## Physical context and source retrieval

The new physical-context profile admitted more fresh source without changing
financial reservation or settlement accounting. The accepted parent draft used
8,591 source bytes, compared with 2,281 in the preceding live trial. Its measured
input was 9,381 tokens. An earlier draft used 9,367 input tokens; with 8,192 output
tokens and the runtime's 8-token protocol margin, it fit the 32,768-token window.
The wire-bound count proof agreed with the runtime measurement.

Follow-up reads now retained their authorized source positions. The parent saw
actual repository authorization/replay and accounting implementation bodies,
instead of only preambles or exception definitions.

## Publication failed at the host deadline

The first parent draft returned eight concrete next checks. It was rejected only
because the report schema allowed four. A second draft passed admission. Both
attempts remained charged.

The compound synthesis bridge step then started a Low claim-review call about
352 seconds into its 450-second deadline. That call was interrupted before
returning. Reconciliation retained its full 44,621-unit reservation as unknown
usage; it was neither refunded nor replayed. The review task retained 117,099
consumed units and zero pending reservations. Its accepted draft and claim-review
plan remained durable, but no report was published.

The chat footer omitted stages that had not returned from this compound step.
This is a reporting/recovery defect, separate from response quality. The next
version checkpoints the accepted draft and advances bounded claim pages through
separate bridge steps, returning settled stage usage before continuing.

## Accepted draft still fails the architecture request

Inspection against the actual source rejected the accepted draft as a complete
account of native Sutra. Its overview invented a native entry path through
`architecture_runtime.dispatch` and `api_runtime.invoke`. Native Auto instead
uses `native_provider.flowEvent`, the bridge's `orchestrate` operation and
`orchestration_flow.handle`/`_handle`. The cited architect implementation invokes
the gateway directly; it does not establish the claimed API-runtime call chain.

The draft explained some permit, repository and lifecycle details but omitted
the adapter-to-canonical-kernel path, branch/join authority and memory/checkpoint
continuity. Valid source IDs and successful response admission did not establish
these conclusions. More source bytes alone did not fix the notes-only child
interface and single-round parent discovery limitation.

Acceptance still requires an accurate source-supported end-to-end native request
flow, kernel enforcement, repository receipts, child recovery, memory continuity
and publication. Package gates and a final DONE state are insufficient evidence.

## Background utility-model interference

VS Code explicitly selected `local-assistant:latest` for both utility model
preferences. Provider logs showed background calls loading that large model
alongside Sutra's Low model, independently of Sutra's routing decision. This
reduced available memory materially. After explicit user approval, only those
two VS Code preferences were changed to `ollama-models/qwen3:4b`; Sutra's saved
Low/Mid/High selections and approved 1 GiB history allowance were preserved.

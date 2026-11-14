# Sutra 0.21.1 validation

The observed request was admitted as `intent: conversation`: the model mistook its current stage's tool restrictions for an inability of the whole harness to review source. The host therefore ended the workflow before workspace handoff. No workspace permission was denied in that failed request.

## Change

The conversation stage now receives explicit, bound host routing capabilities. Its instructions distinguish answering/classifying from requesting the same-chat workspace workflow. Existing conversation contracts are retained; an old goal that says “without workspace tools” still restricts direct execution, not a request for handoff.

A narrow deterministic check rejects known first-person file-access refusals and invokes existing bounded repair. It does not classify arbitrary language, route requests itself, grant permissions, or establish semantic correctness. Quoted discussion and legitimate stage, permission, scope and budget limits remain allowed. Repeated failures are quarantined after the existing three-call limit, with all attempts charged.

Copied-session validation also exposed missing compatibility for previously shipped conversation-policy hashes. Known 0.20.x and 0.21.0 hashes are now accepted for data-only conversation memory, after receipt-chain, ancestry, contract, calibration and fact-provenance checks. The hashes were independently computed from the released VSIX contents. Historical executable proposals remain stale; unknown policies and quarantined ancestors remain rejected.

## Results

- 444 Python tests passed; all 14 release gates passed, including browser checks and packaged VS Code activation.
- Regression tests cover the exact misleading refusal, bounded repair and persistent rejection, legitimate capability explanations, tampered routing metadata, retained legacy goals, released-policy memory restoration without action authority, unarmed dispatch prevention, and continued model switching with the same constraints.
- The exact user prompt was sent to `local-assistant:latest` with reasoning enabled, using a private SQLite backup of the real saved session and its previous mistaken reply. The live state was opened read-only and never modified.
- The model chose `intent: workspace` on its first call; the host reached `NEED_ARM`. Usage was 2,909 input / 686 output tokens. Conversation memory and the same conversation session were retained.
- Workspace contract, workspace budget, repository-operation count and host-action count were unchanged. An explicit unarmed follow-up was rejected before source access. Conversation receipts verified.
- The canonical STP kernel remains byte-identical: `9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`.

The live check stops at the handoff boundary. It does not claim a complete new review of the user's workspace. Existing paging, per-call source limits and cumulative allocations continue to govern downstream work.

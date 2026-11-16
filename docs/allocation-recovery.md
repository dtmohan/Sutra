# Allocation recovery

Use **Increase allocation…** in Sutra Monitor, or **Sutra: Increase Task Allocation**, when a saved workspace workflow stops for context or task-budget capacity. The host previews the change; a modal asks for explicit acceptance. Declining has no effect. Approval keeps the workflow paused. Continue in the same chat; after an extension policy update, the existing Restart review choice re-reads source under the current policy.

The preview shows the existing total, proposed addition, new total, consumed amount and remaining capacity for each affected dimension. Defaults add the previously consumed amount (seconds rounded upward), capped by existing supported task ceilings. This restores headroom by raising totals; it never resets or recalculates historical spending. At the ceiling, an increase may remain insufficient. The host API accepts smaller explicit additions, but the current UI offers one reviewed proposal rather than free-form budget editing.

The original task contract, constraints, permission grants, conversation receipt, user pins and source provenance remain intact. An append-only allocation ledger records successive versions and hash links. Resume, reload, switching models and a compatible policy restart retain these amendments. Individual child grants do not grow implicitly. Resource pressure, missing source, interrupted operations and semantic failures need their own recovery; increasing allocation does not resolve them.

## Host API

The private extension-to-bridge `orchestrate` API has two events:

- `allocation_preview`: paused workflow ID and current cursor, plus optional positive integer `additions` for existing dimensions (`tokens`, `calls`, `operations`, `source_bytes`, `seconds`). Returns a proposal tied to current usage, contract, calibration, policy, allocation version and workflow cursor. It performs no inference.
- `allocation_grant`: the unchanged reviewed proposal, current cursor and `confirmed: true`, emitted by the extension after its modal is accepted. The ledger uses an immediate SQLite transaction to revalidate and append the amendment and workflow receipt atomically. A stale or replayed proposal cannot grant capacity twice.

These events are not agent tools or an exported extension request function. Command arguments and webview data cannot supply an accepted proposal. Model output is not confirmation. A future callee API may offer a model's requested context or a suggested budget as advisory data; this version constructs recovery proposals from measured host capacity stops. It does not add an automatic LLM negotiation tool.

Task allocation and context-window size remain distinct. More task headroom can let the packet builder allocate more context, but every call still checks the chosen model's effective context limit, mandatory task/pinned content, output reserve, parent/child grants, runtime memory and fresh source. Estimates before dispatch and completed measured charges remain separately recorded. Historical or interrupted calls keep their existing charges.

## SSTP relationship

The supplied SSTP v3.1 reference uses explicit offer, acceptance and commitment records. Those mechanics inform this recovery handshake. Its compute cap is fixed for a session and exhaustion ends that session; its RE-SYN addresses origin mismatch. Sutra's versioned allocation amendment is an application-level extension, not a claim that SSTP permits an invisible in-session cap change, or that the full protocol is implemented. Existing hash-chained local receipts are retained; no new cryptographic attestation or semantic-correctness guarantee is claimed.

The STP kernel remains byte-for-byte unchanged. Adapters supply the accepted new totals; ordinary kernel decisions still govern dispatch.

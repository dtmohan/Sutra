# Unified memory lifecycle — 0.28.0

Sutra now shares fault classification, dependency-safe context eviction, confirmed
working-set accounting and retention policy across the existing stores. The
canonical STP kernel is unchanged. Smriti still admits context capacity and
freshness; Karma still admits operations, resources and budgets.

## What is retained

| Data | Backing store and authority | Retirement |
| --- | --- | --- |
| Task, constraints, permissions, spent allocation | Existing append-only host ledgers | Never automatically pruned; always reconstructed from host records |
| Exact user turns and model replies | Invocation/delivery receipts and conversation pages | Leave the prompt when optional; originals remain durable |
| Source and dependencies | Versioned pages with source hashes and dependencies | Changed/expired versions cannot be reused as fresh evidence |
| Summaries, commitments and assessments | Derived/model records with provenance | Unverified; cannot replace originals or grant permissions |
| Prompt working set | Immutable delivered packet plus bounded diagnostic index | Optional pages can be evicted; pinned prerequisites cannot |
| Reasoning, call traces, settled action snapshots | Existing diagnostic files | Shared policy caps and age limits; pending/uncertain actions remain protected |

`harness/memory-policy.json` supplies retention classifications and existing
diagnostic caps to Python and JavaScript. Database immutability and host checks
enforce authority; the descriptive policy file is not a replacement for those
checks. This update adds no RAM cache and moves no task data into model KV state.
Physical model memory is still managed separately by `runtime_memory.py`.

## Fault behavior

| Fault | Host response |
| --- | --- |
| Valid page omitted from the last prompt | Page it in from exact durable storage; check the new packet allocation |
| Conversation page not materialized | Validate receipt ancestry, then reconstruct the exact user/reply page with its original model-data authority |
| Missing source or dependency | Report a gap; restore from an authorized original before retrying |
| Changed source, expired page or stale dependency | Refuse old evidence; refresh through existing scoped source operations and rebuild dependents |
| Corrupt/quarantined original or dependency cycle | Withhold; require inspection/trusted recovery. Never silently overwrite or promote it |
| Wrong ancestry or changed contract | Reject access; no cross-chat retrieval or permission expansion |
| Mandatory context cannot fit | Preserve constraints, pause or report capacity failure; choose a larger eligible allocation or repartition optional work |

The shared orchestration boundary turns page faults into a saved `PAUSED` state,
including during classification and child work. **Resume** re-enters the saved
stage under the same contract and remaining allocation. It does not erase a
completed sibling, replay it, refund a call, or automatically retry a corrupt
page. A second unresolved fault pauses again. Existing capacity-stop displays
remain compatible and now also record the common memory diagnostic.

Rehydration is exact reconstruction, not a model-generated summary. A missing
head pointing to existing immutable page data is not silently rebuilt: it needs
inspection. No new unscoped file-read or network recovery path is introduced.

## Working sets and faults in Sutra Monitor

**Context memory** shows confirmed working sets, page-ins, evictions, refaults,
recent faults and reconstruction events. These describe successful transport of
pages, not proof of model attention or semantic recall. Model-load memory remains
in the separate runtime-memory section of the same monitor.

Only confirmed delivery changes working-set counters. Catalog reads, selection
probes, validation and dashboard polling do not. Delivery confirmation and its
working-set update are one SQLite transaction. Conversation forks follow their
own receipt ancestry; workspace sets are isolated by session, role and node.
Page identity includes key, version and content hash. Fresh workspace excerpts
are also tracked by path, exact text hash and span, while their existing snapshot
receipts continue to enforce freshness. The diagnostic index is never used to
serve source content.

The diagnostic index retains at most 256 working sets and 256 events. Refault
counts are therefore a lower bound within retained history. Two or more returning
pages in a delivery flag possible churn, without changing admission or routing.
Malformed disposable index entries limit diagnostic history instead of blocking
a valid model delivery; unreadable event rows are labelled in Monitor. This does
not bypass any integrity check on original evidence. Fault/reconstruction audit entries, original deliveries, pages and spent budgets
remain in the immutable ledger after diagnostic entries age out of the count
window. There is no automatic archival of immutable evidence: the existing
storage allocation still stops new work before exhaustion.

## Validation

`harness/test_memory_layer.py` covers exact eviction/refault after reopen,
freshness and dependent invalidation, branch isolation, pure monitoring, atomic
confirmation, diagnostic retention, verified receipt reconstruction, corruption
refusal, context overflow before inference, child recovery and model switches
without permission or budget reset. The existing retention, protocol, recovery
and task-observation suites remain applicable.

`test/live-memory-lifecycle.py --output <new-directory>` runs three fresh local
calls through the gateway, using low → mid → low, with process reloads between
calls. It checks receipt integrity, pinned statements, contract identity, spent
call accounting and model recall. Eviction/fault injection use deterministic
fixtures; live calls are not a benchmark or proof of general semantic recall.

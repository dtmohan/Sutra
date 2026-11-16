# Bounded execution sessions and fresh source paging

Sutra distinguishes three things:

| Layer | Lifetime | What is retained |
| --- | --- | --- |
| Conversation and task memory | Across requests and model changes | Original task, constraints, pinned user statements, recent turns, paged notes and evidence receipts |
| Execution session | One workspace request, including retries, children and recovery | A committed allowance, context identity, inherited permissions and consumed work |
| Task allocation and spending | Across execution sessions | The cumulative ceiling, every charged operation/call, and explicit allocation amendments |

At native workspace binding, the host opens an execution node under the existing task. Its fixed allowance is the remaining task capacity admitted by the unchanged STP kernel. A new execution does **not** restore the task's original allowance. Completed or discarded executions release unused reservations; their spending stays charged. Ordinary conversation routing retains its existing separate conversation ledger. Direct advanced commands retain their task gates; they cannot consume capacity reserved by an active native execution.

Reloading, resuming, retrying, changing reasoning controls or switching models cannot create a fresh allowance for the saved execution. Each new dispatch must pass both its execution and ancestor gates. In-flight commitments remain reserved until reconciliation. Interrupted work is receipted without replay or refund.

An explicitly approved task increase still adds a versioned amendment. For current execution nodes, this closes the old fixed session and opens a linked successor with the newly available parent capacity. Old receipts and joined child notes remain historical evidence; child permissions and limits are not enlarged. Pending child work must finish or be discarded before renewal. A policy mismatch still requires the existing explicit review-restart path; an allocation approval cannot silently fix the binding.

## Paging before increasing capacity

Mapping reserves room for a later fresh evidence page. When breadth no longer fits, it selects fewer complete files and can proceed to a bounded synthesis with incomplete structural coverage disclosed. Synthesis ranks source files by the request and entry-point relevance, then selects a page within the remaining source-byte and page allocations. It reports how many files were selected and deferred. Every selected source is read afresh through scoped repository tools. Directory and source-version validation remain in place.

Context fitting continues to reduce optional excerpts, map details and repair shares before refusing a call. The original request, protected constraints and pinned memory are retained. Paging does not refund earlier source reads, token charges, tool operations or elapsed work. If even a useful source page or protected model packet cannot fit, the host pauses before inference and offers explicit allocation recovery. Model capacity and hardware admission remain independent checks.

The monitor shows execution-session spending separately from the cumulative task totals, and the most recent source-page selection. Inline model reasoning is unchanged: it appears only when the local runtime emits it. Host paging, allocation checks and pauses do not generate model reasoning.

## Protocol interpretation and limits

This uses the SSTP distinction between a bounded execution and retained origin/context. It does not claim full SSTP implementation. Task amendments are a Sutra host extension; the kernel is not modified. A UI turn is not a budget reset. Task-wide source-byte accounting is Sutra policy, not a budget unit prescribed by SSTP.

Paging selects complete files; a single file too large for the authorized parser page still needs a narrower source representation or a separately approved allocation. File ranking is heuristic. Partial evidence cannot establish a whole-codebase semantic review, and a second model's review does not prove correctness. The source-byte budget counts repository operation commitments, not every internal hash-validation read. This release does not automatically replenish exhausted task ceilings or authorize broader permissions.

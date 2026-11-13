# Context and compaction

VS Code sizes its assembled chat: instructions, tools, history, attachments and
the current request. Compaction is a capacity operation, not a time expiry. Sutra
returns a signed local receipt pointer for the recognized host summary request,
without calling Ollama. The next real request validates that pointer and its
ancestry. Missing checkpoints are reported; unrelated chats are never used as a
fallback. Recognition still depends on the host's summary-request format.

The VS Code token-count callback uses an offline cl100k estimate with 15% headroom,
rather than the old UTF-8 byte count. This is approximate for Qwen. The advertised
host input window remains 64,000 tokens. Host chat sizing, actual Ollama counts,
and conservative backend allocation charges are three different quantities.

## Model-aware conversation memory

Starting in 0.19, the conversation adapter reads the installed model's declared
context length and configured `num_ctx`. It allocates the lower of those limits
and the tested local runtime cap of 65,536 tokens. Missing model metadata uses an
explicit conservative 8,192-token fallback. OS memory/thermal admission still
applies: an advertised model maximum is not a promise that its KV cache fits RAM.
The dashboard shows the effective allocation and output reservation.

The output reservation is the lower of 4,096 tokens and one eighth of the context
allocation. Half that reservation determines the working-note character cap.
This is an output safety allocation, not a tokenizer-equivalence claim. Context
admission still uses a conservative UTF-8 byte upper bound, including schema and
reply reserve, through the unchanged Smriti adapter/kernel boundary. This can
underuse the model's token window; it does not silently over-admit it.

Memory is assembled in this order:

1. The full host contract, current request, pinned user quotations and explicit
   constraint statements. A model can select important facts, but each must be an
   exact quotation from the current user message. Statements are not verified
   world facts or tool permissions. Explicit constraint-like lines are also
   retained independently of model-selected facts.
2. The immediately previous user/model turn verbatim, followed by a contiguous
   recent window within its allocation. Later corrections remain visible in
   chronological order; older statements are not silently rewritten.
3. Relevant older turns from receipt-backed Smriti pages, plus a budgeted table
   of contents. Lexical retrieval is deterministic. A model can request TOC IDs;
   the host validates, pages them in and makes another fresh governed call before
   publishing. Paging shares the three-call repair/dispatch allocation.

The model's working note is explicitly unverified and supplementary. Full turns
remain in immutable invocation receipts, independently of summary length. Pages
are derived from those receipts and checked against them. Conversation pages use
an archival ten-year TTL; they are not assertions that old world facts remain
current. Source-code reads continue to use their existing freshness checks.

If protected statements, the latest turn or an explicitly requested page cannot
fit, Sutra stops with an explanation instead of silently dropping them. Other
older turns remain stored but may be outside the delivered working set. The TOC
is itself budgeted, and fact selection/retrieval cannot guarantee perfect recall.
A very long accumulation of pinned statements can require a larger allocation or
a separate conversation. No semantic claims are delegated to the STP kernel.

Compatible 0.16–0.18 conversation receipts are checked as historical data. This
migration does not make old executable proposals or workspace checkpoints valid
under a new policy. Quarantined ancestors remain blocked across migration and
model changes. Native signed-checkpoint identifiers remain compatible.

## Arming and permissions

**Sutra: Arm and Open Chat** asks one yes/no question for workspace reading. It
reuses an existing compatible task or creates a read-only task from the first
workspace prompt. Conversation remains available without arming. Reload retains
chat defaults and memory; workspace access is disarmed until the next confirmation.

Edit/test permissions are requested in context. Exact paths supplied in the
request avoid file pickers; unspecified paths require selection at that point.
The current executor supports existing Python files (up to eight) and sandboxed
unittest runs. Permission changes preserve the goal, other constraints and spent
allocations, and invalidate stale proposals. Subsequent model switches use the
same current contract. Network and dependencies remain disabled. The exact edit
or test still receives an action confirmation. Arming does not intercept prompts
sent to another provider; routing proof comes from a request entering Sutra.

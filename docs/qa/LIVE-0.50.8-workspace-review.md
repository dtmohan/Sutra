# Native VS Code architecture review: 0.50.8

Date: 2026-11-19. Existing signed-in chat, Sutra Auto, original architecture
prompt. Native chat controls initiated inference; backend observation was read-only.
Configured model tiers and canonical STP kernel were unchanged.

## Recovery and completion

The first attempt paused before inference on a genuine macOS memory-pressure
warning. After pressure returned to normal, native `continue` resumed the same
flow and retained the conversation and its previous spending. Workspace access
was reused without another authorization dialog.

The Low router measured 2,357 tokens. The new review task measured 58,473:
six High child attempts, one High read planner, one High synthesis and one Low
claim review. Two child attempts were repaired: five valid citations exceeded a
four-citation limit, and another response was invalid JSON. Both remained charged.
Both children and execution finished; zero invocations or charges remained pending.
All new and previous review envelope/receipt hashes verified. The previous review's
135,642 consumed units remained intact in its separate historical task.

## Improvements and remaining quality failure

Accepted child requests for orchestration, bridge and context modules reached the
next source page. Excerpts ended on whole lines. Parent fresh source increased
from 1,464 to 2,281 bytes, and the final report used readable grouped source links.
The draft avoided the previous invented inter-module call chain and acknowledged
missing evidence.

The final report still failed the detailed architecture request. It did not trace
native chat through workflow authority, gateway admission, kernel decisions,
repository receipts and memory recovery. Requested modules sometimes yielded tiny
helpers or exception declarations. The planner chose line 1 of key modules, so
the parent often saw imports and signatures instead of implementations.

The Low reviewer removed every caveat and next check. One contradicted verdict
used the exact docstring that supported the caveat. It also approved a global
1–32 tool-limit claim without the preceding Auto-mode exception. Provenance and
workflow completion therefore did not establish semantic correctness.

## Measured context mismatch

The local count-only endpoint was tested without generation or loading a model.
Reconstructed request hashes matched the recorded wire hashes, and exact counts
matched recorded runtime input usage for parent, child and planner.

The parent reserved 24,133 UTF-8 input units against a 32,768 physical-token
window. Actual rendered input was 5,555 tokens; with 8,192 reserved output tokens,
only 13,747 physical tokens were committed. This left 19,021 physical tokens unused
while source excerpts were squeezed to 2,281 bytes.

0.51.0 separates physical context from reservation accounting, improves central
symbol selection/full-page continuation, removes the arbitrary citation cap and
permits supported claim-evaluation reasoning. Its native report quality must be
tested independently of package gates.

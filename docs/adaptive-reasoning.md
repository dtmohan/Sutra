# Adaptive reasoning

Select **Sutra → Settings → Reasoning & runtime → Model reasoning → Adaptive**
and save. The native setting is `karma.thinking`. New configurations default to
Adaptive; existing `off`, `on`, and `model-default` preferences remain explicit
choices. Installing this release does not change a saved preference. Sutra Monitor shows the effective setting (including workspace overrides), a **Reasoning settings** button and the control requested for each call. **Model default** means Adaptive is inactive.

## Scope and order

The conversation/routing call starts on the configured low-tier model in Auto.
A direct answer finishes there. A workspace request hands off for fresh scoped
source retrieval and the existing worker selection policy. Neither case needs
an extra reasoning/classification call.

In 0.26.0 a deterministic task heuristic selects the first supported positive
effort for explicit reasoning requests, numeric planning/corrections, code
analysis and fenced structured results. It examines the current request, without an extra classifier call.
Routine or unclassified requests start at the lowest supported effort. This is
a scheduling heuristic, with false positives and false negatives, not a measured
semantic difficulty score. Existing Off, On and Model default settings override it.
The control ladder below lists available progression; a selected positive first
step skips the lowest step for that request.

For an admitted conversation proposal that needs help:

1. Load any newly requested receipt-backed Smriti pages at the same effort.
2. If answering requires actual project evidence, request a workspace handoff for fresh scoped reads. If the evidence is unavailable from both memory and workspace, ask for clarification; changing a model cannot supply the missing fact.
3. If the model proposes `reasoning_capacity`, select its next supported effort.
4. Once those choices are exhausted, Auto can select the next untried configured
   model tier. An explicit model tier stays fixed and pauses instead.

Model assessments are unverified proposals. The host selects controls and
validates receipts, context, permissions, resources and remaining allocation.
The supplied STP kernel is unchanged. Reasoning is not a correctness score.
Workspace workers and independent reviewers start at the lowest supported
effort in Adaptive mode. Evidence selection also uses that effort to leave
room for its read plan.

To request thinking once, click the lightbulb in the native VS Code Chat
toolbar or run **Sutra: Think on Next Request**. Click it again to cancel.
It applies to the next Sutra request in this window, including its workspace
calls. Later requests use your saved reasoning setting. When the local model
streams thinking text, Sutra displays it inline.
If routing has already started on a saved request, Resume keeps that routing
step's reasoning preference.

## Supported controls

The host reads local Ollama `/api/show` metadata and binds the normalized
capabilities, preference, step and requested control to the invocation payload.
The gateway rechecks fresh metadata and the provider's actual control before
runtime admission; the transport checks it again before sending the call.

| Advertised values | Adaptive sequence |
| --- | --- |
| `false, true` | `false → true` |
| `false, low, medium, xhigh` | `false → low → medium → xhigh` |
| `low, medium, high` | `false → low → medium → high` |
| `true` only | `false → true` requests; backend fallback may make them equivalent |
| `false` only | `false`; no reasoning retry |
| Missing or unfamiliar ordering | Model default; no invented ladder |

Ollama documents `think=false` as a generic request that falls back to the
lowest supported effort if disabling is unavailable. This does not guarantee
no thinking or distinct computation between controls. Sparse metadata can omit
false even when that documented request suppresses reasoning on a tested model. Named controls are only sent when advertised; boolean/named mixed
orderings that cannot be established are not guessed. `num_predict` is not used
as a substitute for reasoning controls. Output reservation stays unchanged and
includes both reasoning and the structured answer.

Backend reference: [Ollama thinking controls](https://docs.ollama.com/capabilities/thinking).

## Context, permissions and recovery

Every retry retains the original prompt, conversation parent, contract,
workspace binding, requested pages and pinned statements. Paging cannot repeat
already requested pages without progress. Optional recent turns are checked
against their share before insertion, leaving space for retrieval and the TOC.
The latest turn, pinned statements and explicitly requested pages remain
mandatory; admission pauses if they cannot fit.

Each inference consumes the existing session's call, token and time allocation.
Intermediate accepted routing proposals remain receipt dependencies but do not
become ordinary conversation memory. Their quarantine invalidates descendants.
A resource pause persists the previous accepted step; after reload, Resume
checks fresh resources and continues at the pending effort, without replay or
refund. Changing a same-model saved request's preference or capabilities stops
it instead of silently restarting the ladder. An explicit different-model
choice can continue with the same context and remaining allocation.

Model/effort selection never arms workspace access or grants edits, tests,
network access, or dependencies. The native UI shows distinct progress for
context paging, same-model reasoning, and model escalation. The footer includes
all routing stages, repairs and withheld calls.

## Validation scope

Deterministic tests cover boolean and named levels, unknown/non-thinking-only models,
static overrides, page-before-reason-before-switch order, transport mismatch,
quarantine, exhausted context/budget, and resource pause/reload progression.
Native chat fixtures check progress, withheld drafts and cumulative token totals.

The local experiment compares two fresh `hello there` calls per setting on
`qwen3:4b` in an Adaptive/default/default/Adaptive sequence, retaining the normal
gateway, resource gates and output reservation. It separately exercises a
same-model named-control retry after an injected resource pause, and real model
switches that recall pinned task facts and constraints. Routing declarations
and the pressure event are injected to make recovery reproducible; this is not
a benchmark of the model's ability to recognize its own reasoning limitations.
The release validation report records measurements and outcomes.

### 0.25.0 results on the development Mac

All 574 Python regression tests passed, along with native provider fixtures,
settings boundaries, integrity checks and packaging. The browser gate required
a separate retry after a browser-launch timeout; all three browser suites then
passed. All 13 gates are satisfied; see the release validation logs. The canonical kernel hash
remains `9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`.

| qwen3:4b mode | Wall time, two samples | Output tokens, including reasoning |
| --- | --- | --- |
| Adaptive (`think=false`) | 3.904 s, 4.696 s | 148, 143 |
| Model default (`think=null`) | 19.920 s, 55.996 s | 1,307, 3,701 |

All four were valid one-call greetings. The tests used fresh conversation
sessions, the same prompt and generation reservation, temperature 0, seed 42,
and `keep_alive=0`. Prompt binding IDs differ across sessions. Model loading is
included, and runtime variability was substantial. Two samples per setting are
an observation on this machine, not a general latency or quality guarantee.

A controlled local-assistant reasoning proposal was paused under injected
pressure, the ledger reopened, and a real call resumed with advertised `low`
reasoning. Context and the contract remained intact. Switching back to qwen3:4b
then reproduced Cedar, CED724, read-only, no network, and no dependencies. The
switch increased the shared used-call count from 4 to 5; tokens and elapsed
allocation increased rather than resetting. No workspace source was read.

**Answer-quality limitation:** the real local-assistant `low` response named
the project and ticket but omitted the requested constraints from its visible
answer, despite all constraints remaining in its pinned input. It declared
`escalation_reason=none`. Clarifying that the answer must cover the whole request
did not resolve that omission in the repeat run. The small model's subsequent
answer included all constraints. Thus host recovery/context checks passed, but
the live answer-completeness check did not pass for every model. The protocol
does not claim semantic completeness or automatically escalate a confident but
incomplete answer. The release artifacts retain both observations.

The final policy was additionally exercised with qwen3:4b's sparse metadata:
a `false` control proposal was saved, paused, reopened and resumed with the
advertised `true` control on the same model. The real thinking-on response
recalled the project, ticket and all constraints. Switching to local-assistant
and back preserved the pinned input and original contract, with used calls
increasing from 2 before the pause to 5 after all continuations. The larger
model's visible answer was again incomplete; the small model recalled all fields.
The earlier greeting timing comparison used the same first-call controls and
prompt, before this retry-order refinement. No quality guarantee follows from
these limited measurements.

## Model-facing contract in 0.26.0

The conversation model receives the current user request verbatim, recent turns,
pinned user quotations, retrieved pages, the TOC and a compact response schema.
The full contract, resource allocations and routing metadata remain committed in
the host ledger and charged by the same conservative budget check. Their omission
from the model view does not remove admission checks or grant permissions.

The host attaches `context_binding` after validating the raw response. Receipts
record its declared host field, the raw model-response hash, and the bound result
hash. Verification checks both representations; a model cannot inject that field.
Legacy receipts remain verifiable. The answer must be nonblank and close its code
fences. These checks do not prove that every part of the task was answered.

`page_requests` is required at the top level. Mentioning a page in prose does not
load it. A missing-context response with a nonempty TOC must request valid page
IDs, or supply `context_question` for evidence outside the TOC. Malformed requests
use at most the existing two repairs, charged to the original allocation. Actual
page retrieval still precedes any reasoning retry or model change. Page contents,
source freshness and permissions remain host-controlled.

### Decoder compatibility

Experiments on Ollama 0.40.16 with the MLX runner reproduced escaped quote/newline
truncation when the structured decoder received string `minLength`/`maxLength`.
The adapter removes only those keys from a deep copy of the decoder schema for
that exact runtime/runner combination. Enums, object shape, required fields and
all original string bounds remain enforced by the host. Unknown versions and
other runners retain their native schema until separately validated. This fixes
a transport-format failure, not model reasoning or semantic completeness.

The production-gateway validation uses the frozen multi-turn allocation and
retry fixtures from the prompt audit. It also checks small→large→small recall
and a TOC paging case, with real telemetry and unchanged resource admission.
These are limited synthetic observations; the independent grader's execution of
a restricted proposed function does not become a harness-authorized test receipt.

### 0.26.0 production-gateway results on the development Mac

All 581 Python tests and all 13 release gates passed, including the three
headless browser suites. The kernel hash is unchanged. The final live validation
used Ollama 0.40.16, qwen3:4b and local-assistant:latest through the production
conversation gateway, with live telemetry and the existing resource allocation.

- Nine frozen allocation/retry checks passed, including corrections, past charges,
  a minimal retry-function fix (six independently executed synthetic cases), and
  rejection of stale test evidence. This is a limited synthetic sample.
- A small→large→small sequence recalled Cedar, CED724, read-only, no network,
  no new dependencies and an unchanged STP kernel. The host contract stayed
  identical and consumed allocations increased across switches.
- A real model explicitly requested an older TOC page, then correctly reported
  retry→retry and retry→normalize. Seven prior turns were seeded fixtures; both
  retrieval steps were real calls. The first paging grader mishandled a Python
  block before JSON and assumed pair-shaped edges that the prompt never required.
  Offline regrading accepted source/target objects and rejected an added unsupported
  self-loop. The original result and grading correction are retained separately.
- The final greeting used one call, 830 measured input tokens, 72 output tokens,
  and 2.0 seconds. The nine complex checks took a median 29.3 seconds. Structured
  recall on the 4B model took 52.9 seconds with thinking enabled. These observations
  include loading and do not establish general latency guarantees.

An earlier small-model recall was truncated with thinking disabled. Removing
its decoder bounds did not help; enabled reasoning produced the full answer.
That is why the compatibility adapter remains MLX-specific while explicit fenced
structured-result requests select supported effort. The failed answers were
withheld and charged; one bounded experiment reached token FIN without a refund.
Recovery/reload, permission tampering, page-before-effort order and exhausted
allocation are covered by deterministic fixtures with injected failures; the
live runs did not manufacture system pressure. Full host context preservation
still does not guarantee arbitrary semantic completeness.

## Routing regression in 0.26.1

A capability question followed by an architecture/onboarding request must retain the prior turn and hand off for source review. It must not ask the user to manually select files before discovery or invent a project from generic architectural patterns. The model sees a compact field guide instead of raw JSON Schema; transport and host still enforce the original schema. Optional fields are omitted when unused. This reduces format ambiguity without treating semantic routing as a deterministic proof.

Run `test/live-routing-trial.py --output <new-directory> --review` for the exact reported prompts with fresh bridge processes and synthetic source. The run uses installed local models and normal resource gates. `--harness <baseline-source>` selects a frozen baseline; all raw results, including failures, are retained.

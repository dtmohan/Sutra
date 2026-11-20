# Sutra 0.52.0: bounded coherence and generation observations

Date: 2026-11-19. This release adds diagnostic observations through the existing
invocation gateway and Karma adapter. It does not change the canonical STP kernel.

## Stage checks

The host projects existing commitments, assessments, findings, exact source
quotations, structured page facts and claim-review judgments. No new model call,
required answer field or prompt instruction is added. Projection does not read
another task's history or import a global latest conversation.

The pure graph checker bounds its work to 32 contexts, 64 sources, 128 items,
256 relationships and 65,536 text characters. It checks explicit provenance,
missing or stale references, circular declared support, scoped value differences
and explicitly registered obligations. Hypothetical, subject and time contexts
remain distinct. Model-suggested answers and updates cannot close host obligations
or turn model commentary into independent source evidence.

Capture retains at most 8 KiB of metadata, reserving another 4 KiB for gateway
metadata. Omitted identifiers and limited analysis are explicitly reported.
Stored observations are part of the immutable invocation receipt; the original
model proposal and response hash remain unchanged. The Monitor inspects at most
32 recent receipts to display up to 12 observations. It does not replay history.

These are structural observations and semantic indicators. General prose
contradictions, truth and source entailment remain unassessed. A source link proves
provenance, not that a conclusion follows. A model's reported unmet criterion is
an observation, not a newly imposed task requirement.

The kernel frame is advisory. It cannot grant permissions, increase an allocation,
start another model call, create a repair loop, discard context or declare a task
complete. Ordinary diagnostic errors do not withhold a valid answer or strand its
reservation. Intentional cancellation still propagates and uses the existing
append-only reconciliation without replay or refund.

## Within-generation observation

A separate observer receives the actual local-provider reasoning stream. It
fingerprints whitespace-normalized long sentence/paragraph units, retaining at
most 128 digests, 32 recent novelty flags and 4,096 pending characters. A feed
inspects at most 16,384 characters and records any omitted or invalid input.

After at least 32 long units and 2,048 observed characters, an 80% recent literal
duplication rate can emit one live indicator. This threshold is a versioned
diagnostic heuristic, not a calibrated measure of semantic progress. It can miss
paraphrased loops and short repeated units, and legitimate repeated quotations can
also trigger it. It does not automatically cancel or retry a generation.

The live notice is separate host text, not fabricated model reasoning. Final
counter metadata is recorded separately from source evidence and task memory.
Source patterns and full reasoning text are not added to the receipt. Existing
reasoning display/retention limits and actual token accounting remain in force.

The private activity decoder now bounds unfinished calls separately from recent
completed-call replay protection. Previously, 64 completed calls could fill its
tracking map and suppress later inline activity. Endurance fixtures exercise 220
sequential calls, the 64-concurrent-call ceiling, replay rejection and idempotent
shutdown. The retained Monitor archive remains bounded to its existing 12 calls.

## User-supplied case replay

The supplied transcript reports a 1,001.3-second thinking episode on a conceptual
question. Its longest reasoning line contains 120,236 characters of recurring
planning statements. The pasted transcript does not contain a substantive final
answer. Its final transition phrase does not establish successful completion.

The recorded single-generation line was replayed in 64-character chunks, without
new inference. The observer first flagged repetition at 10,304 characters and
86 long units, with a recent duplicate fraction of 0.8125. Final counts were
984 long units, 926 duplicates and 58 novel units. All received characters were
observed; the replay took approximately 31 ms on this run.

These are results for the observer's explicit punctuation/minimum-length
unitization. A separate simple sentence split produced 923 fragments and 54
distinct fragments; the two methods are not interchangeable semantic measures.
The transcript provides no per-chunk timestamps, so the character position does
not establish an alert wall time or a live time saving. It also does not establish
whether weights, sampling, a template or runtime controls caused the repetition.

The full user transcript is not copied into the repository. Replay input SHA-256:
`744b5b01c46feb81ccb6469efc10b0c8a766df9c571ebe8c763936f65fa37173`.
Local replay metrics: `/private/tmp/sutra0520-case-replay.json`.

## Validation and delivery

Focused checks cover source authority, scoped hypotheses and updates, cyclic
support, bounded omission, missing/stale references, malformed diagnostic input,
locked and legacy stores, receipt hashes, reload/model switching, preserved
constraints/spending, cancellation reconciliation, streaming chunk boundaries,
Unicode, literal repetition, metadata forgery and accessible bounded UI rendering.

Full release validation passed after integration froze:

| Check | Result |
| --- | --- |
| Release gates | 27 passed; 0 failed |
| Python suite | 1,576 tests; 8 skipped; 412.293 seconds |
| Focused observation/recovery suite | 99 tests passed |
| Python-to-JavaScript stream snapshots | 1,803 compatible snapshots |
| Node and browser fixtures | Passed, including inline feedback, focus retention and 220-call endurance |
| Packaged runtime | 290 files; tests excluded; 288 files covered by runtime integrity hashes |
| Kernel | Canonical SHA-256 unchanged |

Command: `scripts/qa.sh --with-package --browser`, with the locally installed
Node, Playwright and Chrome paths supplied through its documented environment
variables. Full run log: `/private/tmp/sutra0520-qa.log`.

Delivery: `dist/Sutra-0.52.0.vsix` and `dist/Sutra-0.52.0-source.zip`.
After this report was updated, both artifacts were repackaged and the packaged
contents gate rerun. No application code changed after the full passing run.
The update has not been installed or pushed by this work.

No new live inference, model-quality comparison, automatic loop recovery or full
architecture review success is implied. The previously observed
discovery-convergence and bridge-headroom issues remain separate work.

Canonical STP SHA-256:
`9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`.
Released 0.51.9 data-only conversation compatibility was independently recomputed
from its VSIX before registration; executable results are not promoted across
policy changes. User model tiers and settings have not been changed by this work.

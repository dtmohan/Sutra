# Sutra 0.29.1 validation

Date: 2026-11-15. Scope: withheld workspace review caused by output exhaustion, adaptive workspace controls and bounded output repair. No changes to the canonical STP kernel.

## Confirmed incident

The user's saved qwen3:4b chat-review receipt was quarantined with `RuntimeError: Incomplete generation`. Ollama reported `done_reason=length`, 3,653 input tokens and 4,096 output tokens against a 4,096-token response reserve. The transport requested model-default reasoning (`think=null`). The prior conversation router requested reasoning off. Memory fallback had correctly selected the smaller model before this failure.

The failed packet had only 97 bytes of source excerpts after fitting the remaining task allocation. Reasoning and answer text share the output allowance; the prunable trace no longer contains the full text, so token attribution between them is unavailable. The pasted diagnostic stream shows deliberation over the review instructions. A long-report target and ambiguous summary units were counterproductive with this limited evidence.

## Changes and checks

Adaptive workspace generation starts at the lowest known reasoning control. Unknown capabilities retain the backend default; explicit Off, On and Model default settings are preserved. Architecture writers, child reviewers and report checkers bind the choice to their invocation payload and recheck capabilities before dispatch.

Review instructions now state character units, allow short evidence-limited reports and leave formatting and budget enforcement to the host. No schema, citation, task, permission or publication checks were relaxed.

A typed output-limit failure remains quarantined. Adaptive reviews requesting the generic lowest control may make one compact retry only when another attempt was already allocated. Transport interruptions, resource waits and explicit On/default choices do not trigger that retry. The same output reserve applies, and failed calls remain charged. Source freshness is checked again before retrying.

- Full QA: **13/13 gates passed**, including release metadata, Python, Node contracts, browser webviews, packaging and packaged contents.
- Python suite: **632 tests passed**.
- Ten new targeted tests cover supported/unknown controls, changed metadata, transport errors, exact failure charging, exhausted repair slots, source mutation, child recovery, independent report-check recovery and context/constraint/budget continuity across model switches.
- The STP kernel SHA-256 remains `9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`.

## Live model result and limits

`test/live-review-output.py` reviewed six actual Sutra modules in a separate task ledger with qwen3:4b. The writer used the incident's 20,701-token context allocation and 4,096-token output reserve. The independent invocation that checked the report used its ordinary share of the remaining task budget. Source files and the user's saved ledger were not modified.

The final run completed in **50.22 seconds**, with two calls: writer **3,261 in / 1,181 out**, report checker **2,942 in / 1,602 out**. Both requested `think=false`; neither emitted reasoning text. The draft passed on its first attempt. The checker retained 8 of 12 claim groups and omitted four. All task pins and constraints matched across calls, and invocation-chain validation passed.

**This is a completion/protocol pass, not an architecture-accuracy pass.** Manual inspection found remaining false or unsupported statements, including describing the deterministic kernel as probabilistic and interpreting unresolved static calls as dependency gaps. Both invocations used the same model weights; a separate call does not establish reviewer independence or semantic correctness. Prompt clarification did not eliminate those errors. Only 739 bytes of excerpts fit this deliberately restricted writer packet; six structurally mapped files do not mean six fully reviewed implementations. More useful source coverage and stronger reviewer selection need further evaluation.

An earlier probe needed one formatting repair. Its checker initially used an artificially small fixed context and was blocked before inference; resuming the isolated checker with its normal remaining allocation succeeded without refunding the draft calls. Initial parser-path setup errors were confined to the test driver. The final driver configures trusted parser paths and the checker's normal allocation.

The original user's full conversation and 113-file saved workflow were not replayed. The live regression used a new isolated task; deterministic tests cover model switching and recovery. This release has been packaged, not installed or pushed as part of this bug-report turn.

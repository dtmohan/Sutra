# Sutra 0.51.3: native discovery baseline and intentional pause

Date: 2026-11-19. Tested in the existing signed-in VS Code window against the frozen 0.51.3 package, using native Continue and recovery controls.

## Result

The trial was intentionally paused before synthesis. All eight new Mid architect calls were accepted. Seven repository reads completed; the eighth accepted proposal was saved before its repository operation. No draft, claim review, diagrams or published architecture report was produced. Report quality is **UNTESTED**. This is a serial source-scan baseline, not a terminal model failure.

The native Cancel action occurred at Unix time `1795134201.2939332`, after the task had 15 total model calls and zero pending invocations. `BRIDGE_INTERRUPTED` recorded the flow as `PAUSED` at `1795134203.0410979`. The pause did not cancel a model generation, quarantine an accepted result, refund settled charges or discard the accepted tool proposal.

## Reproduction and preserved state

Prompt: “can you review the workspace project and go through the tools logic and help me understand the architecture in detail? a few block diagrams would be great”

- Task: `review-493c39377419`, reused from the 0.51.2 trial.
- Restarted flow: `9ade3264b7004e8793c8c4c2a0e41588`, created at `1795133501.442247`.
- Mapping reached `BRANCH_PLAN` at `1795133622.9415329`, approximately 122 seconds after flow creation. This time preceded architect inference and is separate from model generation latency.
- Existing task spending was 49,415 settled model budget units across seven receipts. The eight new calls added 55,545 measured units, reaching **104,960 settled units**, with zero pending reservations.
- Historical receipt prefixes remained unchanged: `review-72c1695d466b` at 135,642 units; `review-1a772edb76d2` at 58,473; `review-b9a32b0299a8` at 199,496; and the current task's original seven-receipt prefix at 49,415. No spending reset was used.

The new architect calls used the marker-4 discovery path with selection version 1: a model-selected tool, summary and remaining work under the existing gateway and repository protocol. Acceptance validates the proposal and its scope; it does not establish architecture understanding or report quality.

| Accepted architect invocation | Input / output tokens | Selected read start in `extension.js` |
| --- | ---: | --- |
| `d0fb19ab42d943afac986dc761a95f49` | 6,431 / 290 | line 1, column 0 |
| `fda41b2775c04d79adb74c3cfc622110` | 6,555 / 274 | line 28, column 24 |
| `e9940860cd2247d481d26ceb73f7fdb5` | 6,609 / 274 | line 45, column 12 |
| `9af58f4a3c614df099a606f08970d5d7` | 6,598 / 274 | line 64, column 31 |
| `0a76ec20bbc64334909fa125b11a9db6` | 6,735 / 275 | line 81, column 130 |
| `87426a4f6d154e7384d694b44f75f8b7` | 6,777 / 277 | line 101, column 25 |
| `abf965b2883d40f7b48586ac2aeeb9a6` | 6,822 / 277 | line 116, column 38 |
| `d79a8c6d3f07457eaf1c51fd8ee2e96c` | 6,801 / 276 | line 132, column 5; operation not yet executed |

The seven completed reads delivered 1,200 bytes each: 8,400 bytes total from one file. Their exact continuation cursors included columns inside source lines. The retained eighth decision and operation gap must be distinguished from completed source evidence when recovering the flow.

## Resource recovery and latency

The initial Mid configuration stopped before inference because admission estimated 26.1 GiB plus a 4.0 GiB system reserve, while only 19.0 GiB was available. The idle Sutra-owned High runtime occupied approximately 18 GiB. It was stopped through the normal native runtime command, then Continue resumed the saved flow at `1795133668.919241`. No model tier was replaced and no memory-admission threshold was bypassed.

The first two accepted architect calls took 40.746 and 39.943 seconds. Backend responses reported 2.324 and 1.525 seconds of loading. MLX progress logs showed approximately 16–18 seconds of prompt evaluation, followed by roughly 20–22 seconds of generation, consistent with the previously measured Mid generation rate near 13–14 tokens per second. There was no evidence of a long scheduler wait for these calls. Both committed reasoning policies were Adaptive with `requested: false`; the evidence does not attribute this delay to thinking.

Two VS Code background `qwen3:4b` utility requests accompanied extension reload. They completed at local time 16:12:26 and 16:13:29, before Continue at approximately 16:14:28. The extension log recorded no additional utility request after Continue. Their latency is separate from the later Mid architect calls and their usage is not charged to the Sutra task.

Existing runtime management set `keep_alive=0` for each governed Mid call. Release receipts and backend logs confirmed unloading after the first two calls, subsequent cold starts and prompt-cache misses. This explains repeated loading and prompt evaluation without implying a queue or protocol rejection. No retention setting was changed during this trial. Architect calls retained their existing 150-second limit; the new bounded report/claim timing profile was not exercised because synthesis was never reached.

## Confirmed host limitation and follow-up

The source overview omitted function line addresses before model-packet projection. The frozen graph had `extension.activate` at line 24, `runtimeEnvironment` at 103, `request` at 215, `manageStrata.perform` at 438, `simpleChat` at 529 and `invoke` at 955. The overview recipe converted ranked nodes to names and retained only the first six module declarations; the wire copied that host map unchanged. The actual `nativeProvider.install` wiring was at line 960 of the 985-line, 76,300-byte `extension.js`. `native_provider` was absent from the initial 43-file snapshot, although it remained discoverable through the full authorized catalog and the extension import.

This was an observed source-interface limitation, not proof that the model could never use search. Coupled with 1.2 KB reads and exact byte cursors, the omitted addresses gave the model an expensive way to discover callable bodies: repeated sequential reads through the same file. The behavior does not establish that the model would choose the same scan when given source addresses and coherent pages.

The scoped 0.51.4 follow-up is to supply source addresses and coherent source pages while preserving source receipts, authorization, financial and physical context checks, legacy replay and the existing tool protocol. Model retention preferences remain unchanged. A new native trial is still required to establish whether discovery reaches the real native entry, orchestration, gateway/kernel, repository effects, memory and recovery paths, then produces a readable source-supported report and matching diagrams.

## Frozen validation and artifacts

All 26 QA gates passed before this trial, including browser checks and packaging. The Python suite passed 1,376 tests with eight skipped. See [0.51.3 validation](VALIDATION-0.51.3.md).

- Package: `dist/Sutra-0.51.3.vsix`, 5,054,515 bytes; SHA-256 `f0c4a1a1978f7abe6a0c8a767c47c021ff54a8c80f424cb00f4e9826f754bde7`.
- Canonical STP kernel SHA-256: `9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`, unchanged.
- Saved Sutra tiers: Low `qwen3:4b`, Mid `local-assistant:latest`, High `strata:high`. The chat picker was Mid. Both previously approved VS Code utility preferences remained `ollama-models/qwen3:4b`.
- Local observer and baseline artifacts: `/private/tmp/sutra0513-observer.jsonl`, `/private/tmp/sutra0513-baseline.json`, `/private/tmp/sutra0513-baseline-normalization-proof.json`, `/private/tmp/sutra0513-ui-start.json`, and `/private/tmp/sutra0513-qa.log`. These are local diagnostic artifacts, not a published report or durable source of model authority.

Automated validation and accepted discovery calls do not establish native report usability or architecture answer quality. Those acceptance checks remain outstanding.

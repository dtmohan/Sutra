# Sutra 0.48.0 native tier validation

Validated on Apple M5 Pro with 64 GiB unified memory. All native contexts used the same existing Qwen3.8 Flash Next Q2_0 weights. No additional model files were downloaded. Strata Control and its Metal engine remain backend dependencies; this test does not demonstrate direct embedded-engine execution.

## Real invocation path

`test/live-native-tiers.py` reopened one durable task in a new process for each tier. Low used a 16K native profile, Mid 24K, and High the original 32K profile. The invocation gateway activated the profiles automatically inside the shared runtime lease. The script forbade Ollama RPCs and did not manually load models between phases.

| Tier | Context | Seconds including activation | Cumulative calls | Charged token units |
| --- | ---: | ---: | ---: | ---: |
| low | 16,384 | 33.89 | 1 | 2697 |
| mid | 24,576 | 39.61 | 2 | 5665 |
| high | 32,768 | 31.831 | 3 | 8800 |

Each continuation recalled the synthetic project/ticket, the difference 21 − 18 = 3, and all work restrictions. Contract, permissions, allocation version and prior receipt prefix matched exactly before continuation. Cumulative spending increased for new calls and did not reset. Receipt and audit chains verified. No file actions, repository operations or Ollama RPCs occurred. The original native runtime and calibration were restored and the stopped validation profiles/configurations removed. User settings remained byte-identical.

## Automated checks

922 Python tests passed. JavaScript and browser checks cover all-tier selection, draft-only assignment, native lifecycle, ownership, recoverable activation, shared-slot contention, cancellation, context and budget checks. The extracted VSIX activated successfully in a separate VS Code profile and completed its source-tool/permission smoke checks. Its discovery fixture now waits for catalog readiness instead of assuming a fixed two-second delay.

Native profile tests include resumed already-running targets without duplicate startup and one bounded idle-predecessor release followed by fresh resource admission. Continued pressure leaves a recoverable pause, preserving the selection. Shared weights are not labelled as independent reviewer models or different capabilities.

The canonical STP kernel SHA-256 remains `9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`.

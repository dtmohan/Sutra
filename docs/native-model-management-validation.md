# Sutra 0.47.0 native model-management validation

Date: 2026-11-18. Local Apple M5 Pro, 64 GiB unified memory. Native Strata control 0.1.61, Metal engine 0.1.60. Canonical kernel SHA-256 `9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac` unchanged.

## Native lifecycle

Ran the shipped `strata_management.create()` controller with real native CLI commands and fresh `provider_startup.py` admission. One installed Qwen3.8 Flash Next Q2_0 weight set was used. The 32K/4K tests are profile/context switches using those same weights, not two different Strata models.

| Operation | Observed outcome | Seconds |
| --- | --- | ---: |
| verify resident profile | ready | 2.14 |
| unload idle model | stopped | 1.339 |
| cold reload original | ready | 9.757 |
| register same weights at 4K / second port | imported | 1.014 |
| switch native profile to 4K | ready | 10.942 |
| restore native 32K profile | ready | 11.584 |

The 4K profile used a different loopback port. Native status confirmed the previous owner stopped before selection committed. The server reported 4,096 context tokens and generation readiness. The 32K profile and original calibration were restored; the temporary stopped profile/configuration were removed. No model download occurred.

## Durable task across providers

Executed `test/live-strata-management.py` in independent processes, with the native lifecycle cycle between its first and second phases. Every phase used the real invocation gateway and resource checks.

| Phase | Provider | Seconds | Cumulative calls | Cumulative charged token units |
| --- | --- | ---: | ---: | ---: |
| low-first | Ollama qwen3:4b | 3.802 | 1 | 1805 |
| high | Strata High | 14.84 | 2 | 4676 |
| low-last | Ollama qwen3:4b | 3.658 | 3 | 6930 |

Each continuation correctly recalled the synthetic project/ticket, computed 21 − 18 = 3 and repeated the unchanged-kernel and no-network/edit/test/dependency restrictions. Before each continuation, the script checked exact equality of the saved contract, permissions, spending, allocation version and receipt prefix. Spending increased only for new invocations; no host actions or repository operations were created. Receipt and audit verification passed. This is a continuity check, not a general answer-quality benchmark.

## Regression coverage

Manager tests cover opaque IDs, changed plans, hardware denial, resident-memory handling, busy/unowned ports, exact idle stops, cross-port switches, native readiness, atomic selection, failed-start reconciliation and rollback. Setup tests cover bounded native discovery, a concrete download plan, installed-model reuse, partial-component refusal, fixed install-only arguments and isolated process tasks. Browser tests cover keyboard navigation, live loading states, startup preference, safe text rendering and narrow panels.

The native runtime and weights are separate local installations. Management is currently supported on local macOS hosts. Peak memory of a new profile is unmeasured; fresh hardware/reserve checks and Strata sizing do not constitute a verified peak bound.

# Sutra 0.21.0 validation

This release adds bounded live local-model reasoning diagnostics for parent and child calls. The canonical STP kernel is unchanged (`9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`).

## Deterministic and host validation

- 437 Python tests passed, including fragmented structured answers, separate reasoning, truncated generation with measured usage retained, missing final frames, oversized frames/streams, model-specific controls, and bounded diagnostic output.
- All 14 release gates passed: Python, eight Node suites, browser webviews, packaging/content verification and an extracted VSIX in an isolated VS Code profile.
- Native provider tests cover optional thinking parts, distinct child/model identities, no reasoning in answer text or memory, late-event suppression, cancellation, inherited permissions, context and remaining allocations across model switches. The native provider suite was rerun after the final late-event guard.
- Browser checks cover expandable reasoning cards, preserved expansion/focus while updates arrive, escaped model text, token usage, narrow layouts and existing monitor controls. The rendered screenshot was inspected.
- Real VS Code 1.146.0 accepted `LanguageModelThinkingPart` for parent and child events on two model tiers without enabling proposed API flags. This uses deterministic provider fixtures. The isolated profile's chat UI required sign-in, so the test verifies the native response API, not a clickable native chat widget. Monitor expansion is browser-tested; native presentation remains host-owned. Hosts without the optional thinking class retain the Monitor view.

## Local model validation

1. **Successful reasoning:** `local-assistant:latest`, lowest supported positive reasoning level, produced a live trace before admission and an accepted greeting. Measured usage: 1,286 input / 125 total output tokens. No source tools or permission changes.
2. **Budget exhaustion:** Qwen3.6 35B with reasoning enabled consumed its existing 4,096-token output reserve without completing a structured answer. The response was withheld, and the receipt retained 1,301 input / 4,096 output tokens. No budget enlargement or refund occurred.
3. **Architecture review and switching:** with the default reasoning setting, a fresh-process workflow reviewed two children using Qwen3.6 35B and local-assistant. It completed synthesis, retained the parent goal, Cedar/CED724 memory, read-only/no-network/no-dependency constraints and remaining budget through recovery. Both receipt and audit chains verified. Parent accounting retained 4 calls, 6 operations and 46,242 conservative tokens.
4. The architecture experiment initially asserted every child attempt must be accepted. One attempt was correctly withheld and then repaired within its allowance. The workflow itself completed. The test assertion was corrected to require successful settlement for each child node, and the saved live evidence was independently checked without repeating inference. The original log is retained alongside `Live-switch-verification.json`.

## Boundaries

Reasoning remains unverified commentary. Only the final structured response goes through existing validation and memory admission. Diagnostic events cannot dispatch tools, widen scope, change the kernel result or prove test assertions. Failed and truncated calls retain their budget charges. Output counts include reasoning; separate reasoning-token counts are not reported by this integration.

Reasoning is off by default where the model supports disabling it. Users can request the lowest supported positive level or the model default in Settings. The display retains the latest 12 calls in the window, capped at 16,000 characters per call; full transport generation is separately bounded. Display truncation does not shorten or refund model generation. The view clears on reload; existing trace retention follows the storage policy.

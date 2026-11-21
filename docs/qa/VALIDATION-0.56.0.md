# Sutra 0.56.0 validation

Date: 2026-11-20. Scope: remote NVIDIA inference, GPU-aware admission, existing
gateway enforcement, local/remote model switching and packaged native VS Code
chat. The canonical STP kernel and the user's existing model tiers are unchanged.

## Provider and hardware

The opt-in `sutra:remote/rtx3090` profile connects through an owned, authenticated
SSH tunnel to a private Windows llama.cpp CUDA endpoint. The target has an RTX
3090 with 24 GiB VRAM and 32 GiB host RAM. The loaded Qwen3-4B-Thinking-2507 GGUF
matches the weights behind the Mac's `qwen3:4b` alias. Matching weights are not
treated as an independent reviewer just because they run on another machine.

The server uses one slot and a 32,768-token context allocation. This does not
increase task allocations or grant repository, edit, test or network tools.
Credentials and machine profiles stay outside the source tree and release.

Admission observes remote GPU identity/capacity, host RAM, model digest, runtime
process and executable, allocated context and idle slot. Separate GPU and host
RAM frames pass through the existing Karma adapter and unchanged kernel. Remote
calls do not use the Mac's inference-memory allowance. Local workspace tools
retain their local resource and permission checks.

## Live continuity and runtime recovery

Three actual model calls used the production conversation path and invocation
gateway, with a new Python caller for each phase:

| Phase | Model | End-to-end seconds | Cumulative calls | Cumulative model units |
| --- | --- | ---: | ---: | ---: |
| Initial facts | RTX Qwen3 4B | 26.931 | 1 | 3,303 |
| Recall and subtraction | Local native High | 11.121 | 2 | 5,950 |
| Recall after runtime restart | RTX Qwen3 4B | 21.863 | 3 | 8,767 |

The local High model was `qwen3.8-flash-next-q2_0`, served through the existing
native Strata provider. The RTX runtime was explicitly restarted between phases
two and three after a fresh, admitted full-restoration capacity observation.
The test task was not edited during that restart.

Both models recalled project Cedar-724, ticket CED724, target 18 and actual 21.
The follow-ups correctly computed the difference of three and retained the
prohibitions on kernel changes, network tools, edits, tests and dependencies.
The origin, pinned task snapshot, permissions, limits, base limits and allocation
version remained identical. Earlier invocation and receipt prefixes were
unchanged, and the complete chains verified. Spending increased monotonically;
there was no refund, reset or workspace operation.

Raw phase results and the runtime-restart observation are retained in the sibling
`Sutra-0.56.0-validation` output directory. These are small continuity trials, not
an exhaustive architecture review or a measure of broad model competence.

## Actual packaged VS Code chat

An isolated VS Code profile loaded the exact packaged 0.56.0 VSIX and used the
real native `vscode.lm` Sutra Auto entry. All three tiers were explicitly assigned
to RTX in that disposable profile only. No provider response was injected.

A Hello request completed in 21.858 seconds with a correct greeting, 17 inline
thinking parts, and measured usage of 1,619 input plus 529 output tokens. There
was no paused or withheld response and no workspace permission request. The
model label accurately disclosed that these weights are thinking-only and do
not support an Off switch. The runner reached DONE and cleared busy state.

The fixture coordinator shut down and its processes were closed. The main user
settings file remained byte-identical to its pre-test snapshot; Low, Mid, High,
reasoning and background utility preferences were preserved. This isolated test
does not install the release into the user's main VS Code profile.

## GPU measurements and limits

A cold read-only admission probe took 12.625 seconds, including 9.789 seconds
hashing the 2.5 GB GGUF. Subsequent probes took 2.444 and 2.419 seconds; only the
verified digest was reused in RAM after matching file/runtime identity. GPU
capacity, host RAM and activity were refreshed each time. Adapter evaluation
took 0.10–0.14 milliseconds, with the three kernel evaluations totaling roughly
5.7–9.0 microseconds. These are measured components, not total dispatch overhead.

The 32K startup logs declared 7,437,465,681 bytes of CUDA model, KV and compute
buffers, below the configured 8 GiB restoration envelope. This is not a verified
inference peak. An earlier probe observed an active API/process without that PID
in NVIDIA's GPU-process inventory. Its cause was not established. The provider
requires a full pinned GPU and host restoration envelope in that case rather
than giving unsupported resident-memory credit. Missing or insufficient envelope
coverage pauses before inference. GPU PID presence alone does not establish that
every weight byte is physically resident.

This release supports the configured, already loaded Windows NVIDIA runtime.
General fleet management, cold model replacement and larger-model performance
have not been validated by these trials.

## Regression and release checks

- Full Python suite: 1,900 tests, eight optional dependency/platform skips, no
  failures, in 534.099 seconds.
- Final frozen-source remote suite: 62 tests passed. Coverage includes stale or
  changed hardware/runtime observations, capacity, ownership, permission and
  budget refusals, interrupted streams, measured usage, source continuity,
  reloads and local/remote switches.
- All 26 Node contract suites passed, including native provider, coordinator,
  runner, model selection, reasoning display and runtime integrity.
- All four browser suites passed, including remote Monitor freshness and safe
  rendering. Expired capacity is not displayed as a fresh GPU measurement.
- Discovery-governor (18), convergence-experiment (12) and context-path audit
  (three) checks passed.
- Source metadata, exact packaged runtime contents, telemetry provenance,
  integrity coverage and kernel hash checks passed.

The VSIX contains 305 runtime files, is 5,214,798 bytes, and has SHA-256
`4b644003b8ef821c0c1a53ff425e3df32d86e81bcce5b9b9689d6b8b9a2d4d15`.
The canonical kernel SHA-256 remains
`9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`.

See [remote provider operation and selection](../remote-gpu-provider.md).

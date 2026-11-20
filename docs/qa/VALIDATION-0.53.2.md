# Sutra 0.53.2 — conversation provider waits

Date: 2026-11-20. Baseline: installed/repository 0.53.1, commit `1a6fbbd`.
Canonical STP remains
`9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`.
Runtime policy:
`49194275ed307b4395a542c41fa2c13d71dff041e4768bfc9c33854559ee231a`.

## Observed failure

The first-turn request `Hello Karma - how are you?` selected Auto/Low
`qwen3:4b`, had no parent or retained history, and was admitted by the existing
context, task and resource gates. Its model-facing packet hash was
`1884bb633fddea933030ece748453796e77dc8e6c3a05c7d28552dff492a2e80`.
The call timed out after 150.027 seconds; terminal token usage was unavailable.
No failed-call charge or old user receipt was changed during this investigation.

The shared Ollama log shows one 32K slot processing three other small prompts
serially between 10:03:22 and 10:06:12: 267/252/225 prompt tokens, generating
4,765/3,448/3,736 tokens, taking approximately 70/49/51 seconds of model work.
Three corresponding requests were logged by the separately installed Ollama VS
Code provider. Both background utility-model preferences select that provider's
`qwen3:4b`. This supports utility contention, though the log does not identify
the exact purpose of each request. Sutra's later context-specific model load
was cancelled after its client had timed out.

This is evidence of shared-server queue starvation, not wrong tier selection,
lost conversation history or a context allocation refusal. Ollama documents
[queued concurrent requests and their memory implications](https://docs.ollama.com/faq#how-does-ollama-handle-concurrent-requests).
An idle/healthy `/api/ps` observation cannot establish that a shared server will
remain immediately available when another client submits work.

## Live checks

The same governed conversation request and packet completed correctly while
the server was idle: 2.795 seconds, 1,624 input / 82 output tokens, one Low call.
The full public `orchestrate/start` path then completed in 2.747 seconds with the
same packet hash, response counts and correct greeting. It reached DONE with no
workspace access, model escalation or allocation increase. Both probes used
fresh isolated stores and normal provider/resource/gateway checks, not the
failed user ledger.

Artifacts:

- Original governed probe: `/var/folders/fy/x8qdt8yx3mn57b2pmjj6c0zc0000gn/T/sutra-greeting-governed-ppk1ajbs`.
- Public orchestration probe: `/var/folders/fy/x8qdt8yx3mn57b2pmjj6c0zc0000gn/T/sutra-greeting-governed-pep9cxil`.

An initial raw API probe exhausted its deliberately small 128-token output
limit. It is not counted as a successful greeting or a quality comparison.

## Fix and invariants

Local HTTP transport records a bounded versioned observation of the last phase,
whether headers arrived, validated frame count, and processed message-content /
reasoning bytes. No generated text is added to this observation and byte counts
never become inferred token usage. Optional observations are sanitized before
entering immutable receipts and privacy-preserving traces.

Classifier timeouts now save a PAUSED `provider_wait` on the existing state
machine. The failed call stays charged and quarantined. It cannot become a
conversation parent or escalation receipt. Explicit resume preserves the
original request, valid parent, conversation session, pins, permissions and
cumulative spending, including when the user selects another model.

Header waits, interrupted streams and legacy failures with unknown phase have
different explanations. Native chat presents same-chat continue/resume guidance
and the failed call's usage. No automatic retry, tier change, deadline increase,
shared-server restart or background preference change is introduced.

## Validation

- 141 focused Python tests passed in 21.487 seconds: actual HTTP header stall,
  partial-stream failure, successful transport, sanitized observations, durable
  failure receipts, no unmeasured refund, reload/model-switch recovery, native
  conversation routing, orchestration, gateway and claim-timeout checks.
- Native-provider Node suite passed, including saved provider-wait display,
  one failed call in the footer and explicit High continuation without arming.
- Persistent bridge suite passed.
- Branding and runtime-integrity Node suites passed. The packaged isolated VS
  Code host passed all 21 checks; release metadata and packaged contents match.
- The 0.53.1 full 1,724-test / 31-gate run is prior baseline evidence, not a
  full-suite run of this patch.

Shared queue contention remains possible. Avoiding starvation itself requires
bounded competing requests or runtime isolation. This patch preserves recovery
and reports observed transport state; it does not preempt another client.

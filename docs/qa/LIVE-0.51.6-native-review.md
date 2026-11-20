# Sutra 0.51.6: full native review trial

Date: 2026-11-19. Existing signed-in VS Code chat; original architecture/tools prompt; Sutra Mid (`local-assistant:latest`). The idle owned High runtime was stopped for this controlled Mid trial. Tier identities and Adaptive reasoning settings were retained. User approved audit-history capacity of2GiB; only `karma.storageLimitMiB` changed from1024to2048.

## Result and continuity

The correct successor `8f690105ce3d4861a674255d0ee41af9` restarted directly from `7a78afd3bad542edbe87a0f60755f7fb`. It retained task `review-493c39377419`, the original request and the188254-unit paid prefix. The older open task stayed parked. Mapping completed in about124.5seconds under the newly approved history allowance.

The trial was deliberately stopped through native Cancel after repeated cosmetic response rejections. It ended PAUSED with260340settled units, zero pending reservations and no report candidate. Original model-receipt prefixes for five tasks and twenty parked source-operation/receipt/charge/settlement records stayed unchanged. No evidence was deleted and no spending was refunded.

There were eleven new model invocations: six accepted architect decisions, four note-length rejections, and one interrupted parent writer. The writer started after the observer's zero-pending selector snapshot and before native Cancel reached the worker. Its unknown usage retained the full16855-unit reservation and committed360seconds. Those seconds are not measured generation duration. Total new settled units were72086; the four rejected selectors account for20321 measured units.

## Improvements actually verified

- The first selector jumped to `repository_tools.execute` at line267 and emitted82output tokens. A5145-byte source read was delivered intact, across six exact receipt-backed units, to the next decision. Later active pages also remained intact.
- Host catalog401paths stayed separate from a bounded model index. The first lookup window occupied6.64%of model-prompt UTF8bytes and the next3.46%; these are byte-composition measurements, not token percentages.
- Execution-time allocation increased1800→2250seconds and token allocation250000→312500 through existing workspace policy. All prior spending remained in place; no user allocation dialog was required.
- All actions remained scoped read-only. Canonical STP SHA256 stayed `9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`.

## Remaining failures

**Cosmetic note constraints rejected valid-size working notes.** Four summaries had87,90,75and73characters, exceeding a64-character schema limit. The87-character ASCII case's exact compact note was262canonical bytes, below the existing1600-byte host allowance. It still cost3505measured units and17.962seconds of gateway elapsed time. Short-note guidance is useful; rejecting these otherwise small responses solely for a character count was unnecessary.

**Previously supplied source vanished from the model's progress view.** Every decision after the first had an empty historical note list: fitting retired all prior notes. The source TOC indexed unseen units only, so a fully supplied older module disappeared once its body was evicted. Discovery touched only `repository_tools.py` and `architecture_runtime.py`, with repeated reads of267and399. One repeated399read happened while that source body was still present, so missing progress metadata is a contributing gap rather than a complete explanation for repetition.

**Parent fitting still retired useful paid source.** After three consecutive note rejections, discovery ended `proposal_rejected`. The uncompleted writer's packet contained only about605source bytes across `analyze.py` and the two inspected files. The conservative packing path reduced source and map before distinguishing its financial share from the installed physical context. Thus complete source delivery to intermediate decisions did not establish adequate evidence for final synthesis.

No architecture-quality or diagram-publication pass is claimed. The follow-up is versioned byte-budget note validation, a bounded supplied-source inventory, and physical-first parent fitting with governed financial recovery.

## Recorded timing limits

For the first four new calls, registration-to-settlement durations were21.689,31.077,23.617and40.345seconds. Their gateway elapsed times were16.151,22.537,17.962and29.010seconds. Recorded runtime load and decode intervals are available in the timing artifact; prefill duration was not retained. Neither residual runtime time nor registration/settlement overhead is relabeled as prefill or kernel time.

## Local evidence

- `/private/tmp/sutra0516-live-pause-audit.json`
- `/private/tmp/sutra0516-summary-rejection-audit.json`
- `/private/tmp/sutra0516-first-four-call-timings.json`
- `/private/tmp/sutra0516-observer.jsonl`
- `/private/tmp/sutra0517-baseline.json`: expanded immutable model-receipt prefixes for the next recovery test

The strict observer followed the explicit target's restart descendants and did not adopt a same-prompt foreign flow. It was stopped after the saved pause settled.

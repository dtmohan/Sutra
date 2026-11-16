# Sutra 0.32.0 validation — 2026-11-16

## Changes and guarantees

Architecture claim reviews now use immutable plans with context-sized pages. Every relevant claim/evidence pair is assigned to a page. Evidence IDs bind the candidate, source path, excerpt content and character offsets; retained claims link to source lines. Reviewers select IDs instead of copying quotes. The host verifies bindings and source freshness, not semantic entailment.

All pages must finish before publication. Any contradictory page vetoes a claim; a claim with no supported judgment is omitted. Each page has at most two total attempts across restarts. Accepted pages are reused on the same unfinished request. A new request gets a new draft. Saved pages must fit a replacement model; the host does not silently drop evidence or widen permissions.

The STP kernel SHA-256 remains `9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`.

## Regression coverage

The Python suite contains 664 tests. New coverage checks complete page coverage, Unicode and long-line retention, evidence ID tampering, foreign IDs, incomplete publication, contradiction vetoes, bounded repairs across retries, retained charges, ledger reopening, reviewer changes, cached page reuse, changed source and cold-ledger commits. Existing context, allocation recovery, invocation, permission and model identity checks remain in place.

Release commands:

```sh
python3 -B scripts/runtime-integrity.py --write
bash scripts/qa.sh --browser --with-package
```

The browser checks require the documented `KARMA_PLAYWRIGHT` and `KARMA_BROWSER` paths; Node/TypeScript paths are supplied through the existing environment variables. The final gate log accompanies the package.

## Live architecture, allocation recovery and switching

Tests used the installed local `qwen3:4b` and `local-assistant:latest` models through Sutra's invocation gateway. No runtime permission/resource gate was bypassed, no source was edited by the models, and the canonical kernel was unchanged. Experiments used isolated ledgers rather than the user's active chat.

1. The native-workflow bridge mapped 191 supported source files. An 8,000-unit task allocation paused before synthesis. Preview and unconfirmed approval changed nothing; an explicit additive grant raised the total to 250,000. Replaying approval was rejected. Contract, permissions, consumed usage and original handoff reference survived fresh bridge processes. A large-model draft and small-model checker completed. This experiment preceded the final short-reason prompt clarification.
2. On the final prompt, `qwen3:4b` drafted an architecture report from 16,666 bytes of excerpts from nine files, with a fresh 191-file structural snapshot. `local-assistant:latest` reviewed 16 claim groups over seven pages. The reviewer received an explicit 16,384-unit per-call allocation, including 4,096 output reserve, within the existing task and hardware gates.
3. The test deliberately stopped between accepted pages and closed/reopened the ledger. A subsequent OS thermal-pressure stop prevented further inference. After fresh measurements returned ALLOW, the same ledger resumed. The original writer and first accepted page were not replayed. Remaining pages completed with no formatting repairs on the final prompt. All seven reviewer identities were bound to weights different from the writer.
4. The report retained eight claim groups and omitted eight for insufficient evidence. Evidence IDs in the accepted judgments resolved to their exact supplied source slices. Audit and invocation verification passed.
5. Total live architecture usage was eight completed calls, 21,779 measured input tokens and 3,830 output tokens: 25,609 charged tokens. The allocation stayed at 250,000. The first two calls retained their 13,137-token charge across interruption and thermal recovery; six new calls added 12,472. Additional fresh source operations remained charged as well.
6. A separate three-turn conversation switched `qwen3:4b` → `local-assistant:latest` → `qwen3:4b`, correcting ticket SUT032A to SUT032B. Both later answers retained the project, corrected ticket and all five restrictions: no edits, tests, network or new dependencies, and unchanged STP kernel. Receipt settlement equalled durable consumed usage. Call times were approximately 2.7, 12.3 and 3.2 seconds.

The deliberate page interruption was between calls, followed by a new ledger connection. It was not a forced VS Code process crash during inference. Separate existing tests cover interrupted invocation recovery.

## Findings and limits

The live follow-up found a SQLite cursor invalidation during cold evidence restoration. Materializing the plan rows before restoration fixed it; a regression test covers commits during lookup. Initial MLX reviews exceeded the 240-character reason bound, even on one repair. The decoder compatibility layer omits that string bound from the transport schema. An explicit short-sentence instruction fixed the observed runs while retaining the same host limit, retry count and budget checks. Earlier failed attempts remain in their original experiment ledgers.

These tests establish protocol behavior and provenance, not a high-quality or exhaustive architecture analysis. The reviewer still accepted some broad implications, including a claim that resource handling prevents exhaustion; the supplied excerpt does not prove that guarantee. It also rejected reasonable follow-up recommendations. Eight retained groups do not mean eight verified truths. Claims needing evidence joined across pages can remain unsupported. Claim labels in evidence details refer to the original draft, even when other groups have been omitted.

Local raw results, ledgers and report artifacts are in `../Sutra 0.32.0 live architecture/`; the final seven-page and recall checks are under `final/recovery-v2/`. Earlier failed runs are retained separately. This is a macOS live-model validation; it is not a new Linux/Windows live-model result.

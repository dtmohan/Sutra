# Sutra 0.20.3 review validation

Validated against the 0.20.2 baseline, commit
`44f75342bf10dcdec601e59bb0f5f88722427e0e`, on the local Apple Silicon Mac.
The STP kernel remains byte-identical, SHA-256
`9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`.

## Findings and disposition

| Reported finding | Validation | Change or remaining limit |
| --- | --- | --- |
| Compiled stdlib imports rejected | Reproduced on Python 3.9; inspected the name-set construction | Use `sys.stdlib_module_names` when available, with a tested filesystem-only 3.9 fallback that recognizes extension suffixes and excludes site-packages. |
| Local project imports rejected | Exercised in-scope and out-of-scope modules | Recognize modules within the host-verified read grant. Edit permissions and executable snapshots remain unchanged. |
| Audit scans scale with history | Measured full scans at 1, 8, 32 and 98 MiB | Reuse verification only within an unchanged database connection. Reopening, writes, external commits, schema changes, rollback and database replacement force revalidation or rejection. No persistent trusted-prefix shortcut. |
| Snapshot dependencies missing | Synthetic macOS sandbox test fails importing an omitted helper, then exits zero with the helper explicitly included | Test confirmation lists snapshot files. Document the eight-file, 64,000-byte-per-file limit. Broader read access does not silently grant execution access. Whole-project test execution remains unsupported. |
| Literal adapter threshold | With a changed boundary k=.3, old normalization could allow cost 120 against remaining 100 | Both adapters now normalize using their selected boundary's k and sigma threshold. Regression covers four k values, three sigma thresholds and fit/exhaustion boundaries. The original default k=.15 was not failing. Calibrated uncertainty profiles retain their own versioned boundaries. |
| Incomplete integrity coverage | Baseline manifest had 121 entries and omitted extension, harness and kernel code | Manifest now covers 207 of 209 runtime files; exclusions are itself and VS Code's mutable package metadata. Activation validates before loading local modules; kernel hash is pinned. Package gates verify exact coverage. Hashes are consistency checks, not signatures. |
| Native binary not rebuilt in CI | Confirmed absent build step; local clang rebuild and live sample succeeded | Rebuilt shipped helper from unchanged source, recorded compiler/SDK and source/binary hashes, and added macOS CI compilation. No cross-SDK reproducible-binary claim. |
| Long-line content lost | Reproduced old 1,200-byte truncation advancing to the next line | UTF-8-safe cursor includes line, column and source hash. Tests reconstruct Unicode/CRLF/no-final-newline files, reject stale cursors and changed replays, and continue through the gateway after a model switch. |
| Nonportable QA temp directory | Confirmed the GNU-incompatible invocation in source | Python tempfile replaces platform-specific mktemp flags; explicit output paths are retained and creation failure stops QA. Local QA exercised the default path. |
| Python CI coverage | Only 3.11 was configured | Matrix now covers 3.9, 3.11, 3.13 on Linux/macOS. Local 3.9 and 3.12 suites passed. Remote Linux/3.13 execution remains unverified until CI runs. |
| Maintainability | Counted 162 harness lines over 200 characters and a 44,437-byte extension entry file | Confirmed structural debt, not a reproduced behavioral failure. Centralized runtime file inventory and startup checks in this patch; broad formatting and activation-module refactoring remain follow-up work. No claim that this adds a full linter. |

## Executed validation

- Python 3.9.6: final full suite, 421 tests passed, including real synthetic
  macOS sandbox actions, interrupted-operation recovery and proof-forgery cases.
- Python 3.12: 420-test full suite passed; the subsequently added cursor/model-switch
  regression passed in a 12-test targeted rerun. No runtime changes followed the
  full release run.
- All 13 release gates passed: metadata, Python, seven Node suites, three browser
  suites, packaging, byte-for-byte packaged-content checks and isolated VS Code.
- Native provider regression suite: 33 journeys, including context, permission
  and budget preservation during model switches and workflow recovery.
- The extracted VSIX activated in an isolated VS Code profile, discovered local
  Ollama metadata, read actual CPU/GPU sensors, and mapped fresh fixture source
  while retaining task and calibration bindings.
- Integrity mutation cases rejected changed kernel, harness, extension, provider,
  webview and helper bytes before any local module loaded. Removing the kernel
  entry also failed. An initial test-fixture `exports` omission was corrected;
  the rerun passed.
- No real LLM inference was run for these fixes. Model-switch checks use controlled
  responses through the real protocol/gateway; they do not measure model quality.

## Audit timing

Same synthetic ledger per size; 64 KiB payloads; median of three full checks and
ten repeated unchanged-view checks. These are local microbenchmarks, not an
end-to-end latency forecast.

| Payload | Old full check | New cold check | New unchanged-view check |
| --- | ---: | ---: | ---: |
| 1 MiB | 1.52 ms | 1.49 ms | 0.015 ms |
| 8 MiB | 11.92 ms | 12.30 ms | 0.016 ms |
| 32 MiB | 48.08 ms | 48.34 ms | 0.017 ms |
| 98 MiB | 158.03 ms | 164.54 ms | 0.017 ms |

Cold verification remains linear. This optimization caches only an integrity
verdict for an unchanged SQLite view; it does not cache source reads or prompts.

## Upgrade

This is a packaged stability update, not an installation into the user's normal
VS Code profile. Changed policy files invalidate old workspace policy bindings;
start a fresh workspace task after upgrading. Conversation history remains stored.

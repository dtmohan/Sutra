# Development

Use Python 3.9+ and a recent Node.js with `structuredClone` support (Node 22 works).
The portable Python suite uses standard-library fixtures. Engine codec tests that
need `regex` and `Jinja2` skip when those pinned dependencies are unavailable.
Installed model packs are never discovered from a developer's home directory by
the ordinary suite. TypeScript and Mermaid runtime files are vendored.

Use the single gate runner from the repository root:

```sh
export KARMA_NODE="$(command -v node)"
export KARMA_TYPESCRIPT="$PWD/vendor/typescript/typescript.js"
bash scripts/qa.sh
bash scripts/qa.sh --with-package
```

The runner verifies release metadata, runs every portable Python test and the
Node contract suites listed in `scripts/qa.sh`, and returns nonzero for any failed
gate. `--with-package` additionally builds the VSIX and checks exact runtime-file
completeness, byte agreement, duplicate/unexpected archive members and kernel
identity. The package omission and changelog-order regressions are ordinary
Python tests. The gate runner never regenerates integrity hashes or installs
model dependencies.

The [durable task runner](durable-task-runner.md) section documents automatic
continuation, Stop/reload semantics and its real-process recovery matrix.
The [common operation boundary proposal](kernel-operation-boundary.md) scopes
remaining adapter and lifecycle coverage without changing the canonical kernel.

Action tests exercise operating-system sandbox capabilities. macOS uses
`sandbox-exec`; Linux availability depends on the host. Windows action execution
is currently read-only. Do not weaken an adapter to make a test pass.

Optional checks are explicit:

```sh
# Existing Playwright module and Chromium-compatible executable:
export KARMA_PLAYWRIGHT="/absolute/path/to/playwright"
export KARMA_BROWSER="/absolute/path/to/chromium"
export KARMA_UI_OUTPUT="/absolute/path/to/disposable-artifacts"
bash scripts/qa.sh --browser

# Pack codec only: reads the selected tokenizer, never loads model weights.
export SUTRA_ENGINE_TEST_PYTHON="/absolute/path/to/Engine/python/bin/python"
export SUTRA_ENGINE_TEST_TOKENIZER="/absolute/path/to/tokenizer-directory"
bash scripts/qa.sh --engine-codec

# Package + isolated VS Code smoke host:
bash scripts/qa.sh --vscode
```

The installed-pack flag requires both paths and fails if the selected pack or
pinned codec dependencies are unavailable. A portable run ignores a leftover
`SUTRA_ENGINE_TEST_TOKENIZER`; only `--engine-codec` enables that extra gate.
The optional differential script `test/live-engine-codec.py --pack PATH` reads
only the chosen tokenizer. Its optional earlier-runtime oracle is a validation
comparison, never a production dependency.

`--browser` runs dashboard, diagram, Sutra Monitor and native-manager suites.
The diagram suite tests light/dark/high-contrast changes and inert hostile labels.
`--vscode` uses the isolated smoke fixture described by `scripts/vscode-smoke.py`;
it is not a live model-quality result. The native drivers under `test/` require
separate disposable VS Code profiles, selected local providers and their own
explicit environment variables. Never equate fixture activation with a successful
live workspace review. Record the frozen checkout, model/profile, prompt, receipts,
remaining task budget and any environmental limitations with live outcomes.

On macOS the bundled telemetry helper can be rebuilt from source:

```sh
python3 -B scripts/build-telemetry.py --output bin/telemetry --record bin/telemetry-build.json
python3 -B scripts/runtime-integrity.py --write
```

The build records compiler/SDK, source/header hashes and binary hash; an arm64
Mac also checks a live sample. CI compiles the helper on macOS, independently of
the packaged binary. Different SDKs need not produce byte-identical binaries.
Other platforms use portable monitoring; missing sensors remain unavailable.

After reviewing runtime source changes, explicitly regenerate the manifest with
`python3 -B scripts/runtime-integrity.py --write`. Packaging and QA verify both
coverage and hashes; they never regenerate it implicitly. All runtime files are
covered except the manifest itself and `package.json`, which VS Code may modify.
This detects mismatched files, not malicious replacement of the whole extension.
The canonical kernel hash is separately pinned and must not change.

CI runs the portable gates on Linux/macOS with Python 3.9, 3.11 and 3.13. Browser
and VS Code gates still require the explicit environments described above. A
configured matrix is not evidence that a remote run has passed.

## Repository layout

| Path | Purpose |
| --- | --- |
| `extension.js`, `native_provider.js` | VS Code integration and governed chat |
| `native_memory.js` | Workspace-scoped authenticated receipt pointers |
| `host_tokens.js`, `vendor/tokenizer/` | Offline approximate VS Code history sizing |
| `usage_view.js` | Sutra Monitor: token, system and kernel status |
| `media/` | Dashboard, token tables and diagram views |
| `harness/` | Contracts, tools, adapters, calibration and invocation gateway |
| `harness/stp_canonical/` | Unmodified supplied four-decision kernel |
| `vendor/`, `bin/` | Pinned offline runtime dependencies |
| `test/`, `harness/test_*.py` | Regression and integration tests |
| `package.py` | Runtime-only VSIX and source archive generation |

Changes to governed backend policy files affect the policy hash and may make old
checkpoints inadmissible. Public UI wording may change separately from protocol IDs, signed marker formats
and ledger formats. Recheck saved-task compatibility after any policy change.

`test/host-context-vscode.js` exercises native short turns and automatic compaction
of intentionally oversized history. Run it only with the disposable fixture
transport described in its header; it does not validate real model semantics.
The monitor browser test covers all four decisions, missing sensors, stale
snapshots, heartbeat expiry, context diagnostics and filter independence.

### Conversation compatibility during policy upgrades

When changing a policy-hashed runtime file, verify a saved conversation from the previous installed release as well as a fresh session. Recompute the previous release's policy hash from its shipped `POLICY_FILES` bytes. If its tool-free conversation data remains compatible, record that exact released hash in `conversation_memory.RELEASE_POLICIES`. Never learn trusted hashes from receipts at runtime, restore old action authority, skip receipt validation, or silently discard user memory. Exercise same-session handoff using a read-only backup of saved state before installing the upgrade.

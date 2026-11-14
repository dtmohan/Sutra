# Development

Use Python 3.9+ and a recent Node.js with `structuredClone` support (Node 22 works).
The Python suite uses the standard library. TypeScript and Mermaid runtime files
are vendored so packaging and normal extension use need no registry downloads.

From the repository root:

```sh
export KARMA_NODE="$(command -v node)"
export KARMA_TYPESCRIPT="$PWD/vendor/typescript/typescript.js"
python3 -B -m unittest discover -s harness -q
node test/native-provider.cjs
node test/hardening.cjs
node test/panel-entry.cjs
node test/model-selection.cjs
node test/branding.cjs
node test/host-tokens.cjs
node test/runtime-integrity.cjs
python3 -B scripts/runtime-integrity.py
python3 -B package.py --output dist
```

Action execution tests exercise operating-system sandbox capabilities. macOS uses
`sandbox-exec`; Linux sandbox availability depends on the host. Windows action
execution is currently read-only. Do not weaken the adapter to make a test pass.

Optional browser checks need Playwright and a Chromium browser. Set
`KARMA_PLAYWRIGHT` to the installed Playwright module path and `KARMA_BROWSER` to
the browser executable, then run `node test/dashboard-browser.cjs` and
`node test/diagram-browser.cjs`. Set `KARMA_UI_OUTPUT` to an artifact directory
and run `node test/usage-browser.cjs` for the docked token dashboard. The usage
view's read-only message boundary, shared reader and hidden-view polling behavior
are covered by `test/hardening.cjs`. Native VS Code integration drivers in `test/`
need an isolated VS Code profile, installed local models and their documented
environment variables; they are not ordinary Node unit tests.

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
| `usage_view.js` | Docked token dashboard and status-bar navigation |
| `media/` | Dashboard, token tables and diagram views |
| `harness/` | Contracts, tools, adapters, calibration and invocation gateway |
| `harness/stp_canonical/` | Unmodified supplied four-decision kernel |
| `vendor/`, `bin/` | Pinned offline runtime dependencies |
| `test/`, `harness/test_*.py` | Regression and integration tests |
| `package.py` | Runtime-only VSIX and source archive generation |

Changes to governed backend policy files affect the policy hash and may make old
checkpoints inadmissible. The Sutra branding change leaves all `harness/` files
byte-identical to the imported baseline. Public UI wording may change separately
from protocol IDs, signed marker formats and ledger formats.

`test/host-context-vscode.js` exercises native short turns and automatic compaction
of intentionally oversized history. Run it only with the disposable fixture
transport described in its header; it does not validate real model semantics.
The monitor browser test covers all four decisions, missing sensors, stale
snapshots, heartbeat expiry, context diagnostics and filter independence.

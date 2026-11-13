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
python3 -B package.py --output dist
```

Action execution tests exercise operating-system sandbox capabilities. macOS uses
`sandbox-exec`; Linux sandbox availability depends on the host. Windows action
execution is currently read-only. Do not weaken the adapter to make a test pass.

Optional browser checks need Playwright and a Chromium browser. Set
`KARMA_PLAYWRIGHT` to the installed Playwright module path and `KARMA_BROWSER` to
the browser executable, then run `node test/dashboard-browser.cjs` and
`node test/diagram-browser.cjs`. Native VS Code integration drivers in `test/`
need an isolated VS Code profile, installed local models and their documented
environment variables; they are not ordinary Node unit tests.

On macOS the bundled telemetry helper can be rebuilt from source:

```sh
clang -O2 -fobjc-arc -framework Foundation -framework IOKit harness/telemetry.m -o bin/telemetry
```

Update its SHA-256 entry in `runtime-integrity.json` after a deliberate rebuild.
This helper is platform-specific. Other platforms use the portable monitoring
path; missing temperature sensors remain unavailable.

## Repository layout

| Path | Purpose |
| --- | --- |
| `extension.js`, `native_provider.js` | VS Code integration and governed chat |
| `native_memory.js` | Workspace-scoped authenticated receipt pointers |
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

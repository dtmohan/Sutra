#!/usr/bin/env bash
# Sutra QA gate runner.
#
# Usage:
#   scripts/qa.sh                 # Python suite + Node contract suites
#   scripts/qa.sh --with-package  # also build the offline VSIX
#   scripts/qa.sh --browser       # also run Playwright checks (needs KARMA_PLAYWRIGHT/KARMA_BROWSER)
#
#   scripts/qa.sh --vscode        # package + isolated VS Code host; requires running Ollama
# Exit status is non-zero if any gate fails. Each gate prints its own output.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

WITH_PACKAGE=0
WITH_BROWSER=0
WITH_VSCODE=0
for arg in "$@"; do
  case "$arg" in
    --with-package) WITH_PACKAGE=1 ;;
    --browser) WITH_BROWSER=1 ;;
    --vscode) WITH_VSCODE=1; WITH_PACKAGE=1 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

export KARMA_NODE="${KARMA_NODE:-$(command -v node || true)}"
export KARMA_TYPESCRIPT="${KARMA_TYPESCRIPT:-$ROOT/vendor/typescript/typescript.js}"

if [ -z "${KARMA_UI_OUTPUT:-}" ]; then
  KARMA_UI_OUTPUT="$(python3 -c 'import tempfile; print(tempfile.mkdtemp(prefix="sutra-qa-"))')" || exit 1
fi
export KARMA_UI_OUTPUT
PASS=0
FAIL=0
declare -a FAILED=()

gate() {
  local name="$1"
  shift
  printf '\n== %s ==\n' "$name"
  if "$@"; then
    PASS=$((PASS + 1))
  else
    FAIL=$((FAIL + 1))
    FAILED+=("$name")
  fi
}

python_suite() {
  python3 -B -m unittest discover -s harness -p 'test_*.py' -q
}

node_suite() {
  "$KARMA_NODE" "test/$1.cjs"
}

browser_suites() {
  if [ -z "${KARMA_PLAYWRIGHT:-}" ] || [ -z "${KARMA_BROWSER:-}" ]; then
    echo "FAIL: set KARMA_PLAYWRIGHT and KARMA_BROWSER to run browser gates" >&2
    return 1
  fi
  "$KARMA_NODE" test/dashboard-browser.cjs &&
    "$KARMA_NODE" test/diagram-browser.cjs &&
    "$KARMA_NODE" test/usage-browser.cjs
}

if [ -z "$KARMA_NODE" ]; then
  echo "FAIL: node was not found on PATH" >&2
  exit 1
fi

gate "release metadata" python3 -B scripts/check-release.py
gate "python harness" python_suite

for suite in native-provider hardening panel-entry model-selection branding host-tokens runtime-integrity; do
  gate "node $suite" node_suite "$suite"
done

if [ "$WITH_BROWSER" -eq 1 ]; then
  gate "browser webviews" browser_suites
fi

if [ "$WITH_PACKAGE" -eq 1 ]; then
  gate "package vsix" python3 -B package.py --output "${SUTRA_PACKAGE_OUTPUT:-dist}"
  gate "packaged contents" python3 -B scripts/check-release.py --package-dir "${SUTRA_PACKAGE_OUTPUT:-dist}"
fi

if [ "$WITH_VSCODE" -eq 1 ]; then
  gate "packaged VS Code host" python3 -B scripts/vscode-smoke.py --package-dir "${SUTRA_PACKAGE_OUTPUT:-dist}"
fi

printf '\n== summary ==\n'
printf 'passed: %d\n' "$PASS"
printf 'failed: %d\n' "$FAIL"
if [ "$FAIL" -gt 0 ]; then
  printf 'failed gates:\n'
  printf '  - %s\n' "${FAILED[@]}"
  exit 1
fi

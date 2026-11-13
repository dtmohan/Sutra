# Sutra 0.20.1 stabilization

The STP kernel is byte-for-byte unchanged. These changes address the recovery,
input preservation, setup and permission journeys in the 0.20.0 QA review.

## Recovery

- Resume and Discard are explicit Monitor and Command Palette actions. Resume
  focuses the existing chat without replacing its draft or submitting a prompt;
  the next Sutra message continues the saved request. Other prompts require an
  explicit choice instead of silently cancelling saved work.
- Pending workflows can be rediscovered from the ledger after a bridge restart.
  A second workflow cannot silently take over an unfinished request.
- Paused mapping can retry admission using the original contract and remaining
  allocation. An exhausted budget remains exhausted; Resume does not top it up.
- Executing actions enter CANCELLING until process cleanup and receipt settlement.
  Reconciliation records observations without replaying actions or refunding
  consumed allocations. Test output still cannot prove test assertions.
- A child receipt committed before a crash is validated and completed without
  repeating inference. Rejected children cannot be joined.

## UX and setup

- Task drafts, expanded child scopes and keyboard focus survive state updates.
  Only an acknowledged save or explicit reload clears task drafts.
- Unavailable Ollama/model discovery has actionable guidance and a retry button.
  Empty discovery cannot enable Save. Existing configured choices are preserved.
- Permission dialogs explain that file grants persist with the task while
  disarm/reload stops execution until rearming. Each action still requires its
  own confirmation. Explicit Python test paths are no longer silently filtered
  by filename prefix; unsupported platforms are rejected before a grant.
- The routing status bar opens the Monitor without changing chat defaults.
- Both dashboards use the same age predicate for temperature data. Added tab
  keyboard navigation, SVG accessible names, live status regions, keyboard token
  descriptions, a decision legend, and clearer empty/unknown states.

## Release checks

`bash scripts/qa.sh --browser --vscode` runs the Python and JavaScript suites,
browser fixtures, metadata checks, packaging, byte-for-byte package checks, and
an isolated VS Code smoke test. Set KARMA_NODE, KARMA_PLAYWRIGHT, KARMA_BROWSER,
and SUTRA_VSCODE as needed. The VS Code smoke test requires running Ollama for
installed-model metadata; it does not run inference. Browser output goes to
KARMA_UI_OUTPUT and packages to SUTRA_PACKAGE_OUTPUT, defaulting to a temporary
directory and `dist`, respectively. Missing requested browser prerequisites fail
the gate rather than silently passing.

The separate `test/live-orchestration.py` exercises real local inference, saved
workflow discovery, context and budget retention, and a model switch in fresh
bridge processes. It uses an isolated synthetic project. Model text remains
unverified commentary, even when protocol checks pass.

The stale host drivers now compare against the manifest version. The VSIX ships
third-party notices; development tests, scripts and QA documents stay in the
source archive. A Linux/macOS CI workflow is provided; remote CI has not been
executed as part of this local validation.

## Scope and remaining work

This is not closure of every item in the original QA report. Full screen-reader
and cross-platform interactive validation, shared rendering of both token views,
and diagram dark-theme polish remain follow-up work. The reported network-grant
mismatch was not reproduced: normal review and native-coding contracts reject
network/dependency permission flags. Empty test grants are rejected by the
backend, not treated as successful grants.

Data-only conversations from 0.20.0 are compatible. Executable proposals and
workspace receipts remain policy-bound; old actions are not reauthorized by an
upgrade. Old unfinished work may need explicit discard and a fresh request.

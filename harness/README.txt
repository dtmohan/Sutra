KARMA + SMRITI DOMAIN ADAPTERS — 0.10.0

This folder is the Python runtime of the Karma VS Code extension. Read the root
README.txt for current features, limitations, commands and calibration schemas.

smriti_adapter.py: context capacity, source-page freshness, explicitly selected
uncertain-domain measurements. Exact TTL, source-hash and capacity guards remain.
karma_adapter.py: counted operation budgets, task allocations, storage, hardware
and explicitly selected uncertain-domain measurements.
kernel_boundary.py: finite-number validation and dispatch to the unchanged
stp_canonical/kernel.py. It contains no domain normalization.
kernel.py / kernel_adapter.py: compatibility aliases only.
calibration.py: immutable version registry, explicit per-task bindings, expiry,
revocation, input coverage validation, hash-linked diagnostic receipts.

Canonical kernel SHA-256:
9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac

A calibration profile is selected before evaluation and bound into run config,
contract delivery, invocation/assertion receipts and checkpoints. A model switch
preserves that selection; reselection/expiry/revocation withholds dependent notes.
Permission and evidence checks are independent of optional calibrated thresholds.
Uncertain ALLOW is not semantic proof or permission to execute a tool.

vscode_bridge.py is the host entry point. InvocationGateway is the only provider
call site. Review, parser, selector, worker, coder, architect, API mapper/reviewer,
and deterministic-map explainer each retain their existing role checks.
Repository reads and searches remain fresh, scoped host actions. Project test
stdout is untrusted and cannot authorize automatic task completion.

Hashes and append-only SQLite triggers provide local integrity checks, not
signatures or protection against a privileged caller rewriting the database.
Model text and declared calibration evidence are not trusted host observations.
No remote authenticated handoff, automatic calibration fitting, universal semantic
score or variance-based theorem is implemented.

Run the companion source archive tests with Python 3.9+:
  python3 -B -m unittest discover -s harness -p 'test_*.py' -v
  node test/hardening.cjs
Node is required for TypeScript checks (KARMA_NODE can select it). The compiler is
bundled. macOS sandbox tests require a host that allows sandbox-exec. Windows
editing/execution remains unavailable; no new native cross-platform certification
is claimed by this adapter refactor.

chat_runtime.py: governed local chat review with fresh source, citations and compact handoff.
model_routing.py: explicit local tier policy; heuristic, not semantic calibration.

smc_temperature.h / temperature_signals.py: current read-only AppleSMC measurements and independent advisory validation.
model_catalog.py: local installed-model metadata and editable tier suggestions.

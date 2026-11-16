# Capability registry and governed tool installation

Sutra 0.33.0 supports Ruff (Python lint) and Pyright (Python types). Built-in scoped reading, search, source mapping and diagrams remain available. Optional tools are described by release-owned IDs; the model proposes an ID, operation and source paths. It never supplies executable commands or package URLs.

## Use

1. Ask in native Sutra chat: `Run Ruff on code.py` or `Check code.py with Pyright`.
2. Grant workspace access if prompted. Review the install dialog if the pinned tool is missing.
3. Review the separate check dialog, which lists snapshot paths and reserved task capacity.
4. Read the diagnostics in the same chat. A later model can explain the saved result without rerunning it.

Sutra Monitor has Manage tools and Tool permissions buttons. The Command Palette offers Sutra: Manage Tools and Sutra: Configure Tool Policy. Manage Tools installs a missing tool or checks the authorized Python inventory with an installed tool. Native chat can choose explicit paths instead.

## Workspace policies

The default is Ask each time. Configure Tool Policy also offers Disable optional tools or Preapprove selected operations. An equivalent settings example is:

```json
{
  "karma.tools.mode": "preapproved",
  "karma.tools.preapproved": {
    "ruff": ["install", "check"],
    "pyright": ["check"]
  }
}
```

Sutra asks the user to trust that policy before its first use. Approval is stored separately from repository files and bound to the workspace, normalized policy and registry digest. Reloads and model switches retain it; policy, workspace or registry changes invalidate it. Unlisted operations still ask. Workspace trust and Sutra workspace authorization are always required. A disabled policy prevents dispatch.

## Admission and effects

The central state machine binds a task to a tool proposal, enters NEED_TOOL_APPROVAL, then executes only after host authorization. The proposal expires after ten minutes and binds the task contract, calibration, host policy, registry and exact source hashes. Budgets, resources, storage and source freshness are checked again immediately before dispatch. STP remains unchanged: adapters translate the measured costs into its existing decisions.

Install reserves one operation and 120 seconds; a check reserves one operation, 30 seconds and the snapshot bytes. Completed work settles observed elapsed time. Failed work stays charged, and interrupted work keeps the reservation. Allocation increases remain a separate explicit host action; switching models cannot refund work or expand permissions.

Downloads come only from pinned official PyPI/npm artifacts, with bounded sizes and cryptographic integrity checks. Extraction rejects links, path traversal, duplicate case-insensitive paths, special files and expansion overflow. No install scripts run. The install approval is a narrow network authorization for that registry artifact; the task's general network/dependency permissions do not change.

Checks run in the macOS sandbox on a temporary source snapshot. They cannot write project files, use the network, load repository configuration or run fix commands. Pyright uses its bundled type stubs with a Python 3.9 target. The process output is a static diagnostic observation, not a verified correctness claim. Source filenames/messages are escaped before display.

Receipts are append-only and audit-bound. Diagnostic documents/pages enter the existing Smriti evidence store. The next model receives a bounded historical excerpt with its receipt and source hashes. An interrupted delivery consumes the saved receipt rather than rerunning a tool. A separately approved reinstall can quarantine an unverified prior tool folder and install a verified replacement.

## Current limits

Execution is tested on Apple Silicon macOS. Linux and Windows have package mappings but no enabled execution sandbox yet. The first release is Python-only; snapshot completeness and disabled project configuration can cause diagnostics that differ from project CI. Explicit requests accept up to eight paths; an empty path list selects the authorized Python inventory within the existing task byte/page allocation. Output is bounded at 64 KiB. No tools are installed merely by activating the extension or discovering models.

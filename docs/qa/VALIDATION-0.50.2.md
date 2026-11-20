# Sutra 0.50.2 — bottom-bar controls

Validated on 2026-11-19. This release changes the native-chat controls and status
presentation. The canonical STP kernel remains
`9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`.

## Behavior

Clicking the primary Sutra status opens Quick Controls. Enable alone keeps the
current chat; Enable and open Sutra chat explicitly starts a new Local Sutra Auto
chat without submitting a prompt or granting workspace access. The menu also
exposes workspace access, model choices, reasoning and Monitor. During work only
Monitor is available. Saved authorization, task context and consumed budgets are
retained through enable/disable. Idle status still says routing is unverified.

## Validation

The native-provider controller suite covers persistent enable/disable, retained
saved grants/context/spending, explicit chat opening, cancellation, duplicate
menus, stale enable/workspace selections, busy state before and after selection,
managed settings, effective-setting overrides, failed chat opening, concurrent
setting writes and configuration changes from another window. A delayed disable
blocks outstanding action tickets, external workspace tools and access dialogs
before the preference write finishes. No menu action
dispatches inference or implicitly approves source reads.

The extension-entry hardening fixture verifies that the actual status callback
wires Quick Controls, labels the control for accessibility and shows active work.
The workspace-authorization suite exercises the same saved review across reloads,
model switches, changed source, allocation recovery and revocation. The separate
authorization-store, branding, panel-entry and runtime-integrity suites pass.
Release metadata, packaged file equality and kernel hash are checked separately.

These are controller and extension-host fixtures, not a live mouse-driven UI
session. Python accounting and NPU/GPU inference code are unchanged; the long
device runs from 0.50.1 were not repeated for this controls-only update.

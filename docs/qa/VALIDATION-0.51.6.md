# Sutra 0.51.6 validation

Date: 2026-11-19. Canonical STP kernel unchanged.

## Automated validation

The full QA invocation ran 1,423 Python tests in 250.147 seconds, with eight skips and no Python failures. Node provider, browser, integrity, release and packaging checks passed. The initial combined run reported 25/26 gates: the workspace authorization test adapter removed `chat_key` from the newly added native-chat-only read lookup. The adapter now retains that identity for `recoverable_review`, while legacy task operations still exercise their original route. Its unchanged source/spending/consent assertions remain enforced. The entire affected workspace-authorization integration and native-provider suites then passed. Only this unshipped test adapter changed after the full run; production, policy and packaged runtime bytes did not change.

The recovery-specific regressions cover older focus restoration, original-request append order with equal timestamps, a later restart of the older request, preserved source charges, declined confirmation, current binding, stale stack cursors, busy-task refusal and model switching. Twenty-eight related Python tests passed independently before full QA.

## Installed artifact

- VSIX: `dist/Sutra-0.51.6.vsix`, 5,063,960 bytes.
- SHA-256: `4c6af1c478a7c23ddfe834d6d780d7db381eb11b6d1bdaa18c8f5afd162dfb48`.
- Policy: `a7e36436f21552505829d9a7736c29ad9b4ad46c54375ed5d7b83e8ddd23959e`.
- Canonical kernel: `9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`.
- Installed through Code CLI and activated with native Reload Window. All 285 runtime files match the package, allowing only Code's added `package.json.__metadata`.

## Native acceptance scope and limitation

In the existing signed-in chat, explicit Continue now offered the unpublished latest review and explained that older open work would stay saved and be parked. The confirmation was declined deliberately; this was a recovery-offer/decline test, not a completed architecture review or a positive restart acceptance test. No new source scan or model inference was requested by that decline. Strict read-only accounting baselines retain the latest task's 188,254 spent units, the older task's 69,045 units and its 20 source receipts.

The audit database is 994.27 MiB with zero free pages, against the user-approved 1,024 MiB history limit. Approval to raise that cap to 2,048 MiB is pending; no limit increase, evidence deletion or spending reset was applied. A full positive native review and report/diagram quality assessment remain pending that capacity decision.

The picker was restored to Sutra Auto. Configured Low/Mid/High identities and Adaptive reasoning were retained. The owned High runtime is available and idle. No Git push was performed in this validation task.

The lifecycle audit in `LIFECYCLE-AND-KERNEL-BOUNDARY-0.51.5.md` remains applicable: universal backend-stop acknowledgment, performance-derived transport allowances and one causal preflight-to-settlement attempt trace are proposed work, not features implemented by this release.

## Local validation artifacts

- `/private/tmp/sutra0516-qa.log`: original combined run; its original failed fixture result is retained.
- `/private/tmp/sutra0516-validation.json`: correction and affected-suite result record.
- `/private/tmp/sutra0516-offer-ui.txt`, `/private/tmp/sutra0516-declined-ui.txt`, `/private/tmp/sutra0516-final-ui.txt`: native UI captures.
- `/private/tmp/sutra0516-baseline.json`, `/private/tmp/sutra0516-source-baseline.json`: read-only continuity baselines.

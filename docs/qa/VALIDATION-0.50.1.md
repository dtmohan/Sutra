# Sutra 0.50.1 — receipt verification latency

Validated on 2026-11-19, Apple M5 Pro / 64 GiB. The canonical STP kernel remains
`9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`.
User model settings and the existing GPU runtime are preserved.

## Fixes and trust boundary

- WAL read snapshots allow receipt commits while strict verification reads a
  consistent view. FULL synchronization remains enabled. Busy legacy journal
  migration is deferred; no task is interrupted or budget replenished.
- Session/run indexes remove global protocol-mirror scans. Invocation and
  protocol completeness checks share one consistent read transaction.
- Verified same-connection audit prefixes hash only their own new tail. Ordinary
  unknown external changes and fresh connections remain strict.
- Modern gateway callers borrow their existing ledger. Database identity,
  policy, run/session, node/owner scope and the exact caller-selected trust choice
  are checked before/after inference. No transaction remains open during the
  model call. The gateway never closes the borrowed store.
- An explicit `audit_prefix=True` recovery profile uses the SAME caller-trusted
  V2 accounting seal's authenticated audit cursor. External changes replay the
  canonical suffix from that ORIGINAL cursor, without promoting later SQLite
  seals implicitly. Clearing/changing trust invalidates proof reuse.
- Deep audits always re-derive current historical bytes. The fast profile trusts
  a historical prefix supplied by the host; it does not freshly inspect every
  prefix byte. No protected NAS witness or independent signing service is
  installed. Default unanchored verification has not been relabelled as fast.
- Read-only diagnostics support quiescent WAL on this Mac using an existing-file
  descriptor plus SQLite query_only. Application SQL writes remain prohibited;
  SQLite may maintain its coordination sidecars. Private detailed-audit backups
  are normalized to DELETE mode before OS-read-only verification.

See [behavior and backup requirements](../receipt-verification.md).

## Automated validation

The final standard QA run passed 24/24 gates: 1,166 Python tests with eight
environment-dependent skips, Node contract gates, runtime integrity, release
metadata, packaging and packaged contents. The final 34 WAL/profile/borrowing
tests also pass on Python 3.12 with no skips. Additional focused suites exercise
both system Python 3.9 and Python 3.12.

Coverage includes strict external tampering with a restored schema version,
nonpositive inserts after prefix caching, suffix corruption, incorrect/missing
seal/header/audit cursor, no implicit seal promotion, deep detection of trusted
prefix corruption, trust revocation, file/connection/policy isolation, caller
transaction rejection, cancellation and conservative spending, model switches,
permission changes and recovery-profile restoration after deep auditing.

## Controlled host matrix

Exact 0.50.0 Git baseline `38c0b7e` versus the WAL/index/audit-tail candidate,
using cloned closed 0/1k/10k/100k settled host-history fixtures plus one independent
deterministic-adapter parser receipt. These are real bookkeeping APIs, not device
inference. Modern gateway/profile additions came later; a final 100k unanchored
functional recheck is retained separately. Frozen code/provenance are recorded
in the evaluation directory.

| Settled history | DELETE writer commit | WAL writer commit | Own-append verification: baseline / candidate |
| ---: | ---: | ---: | ---: |
| 0 | 1.728 ms | 0.119 ms | 0.249 / 0.168 ms |
| 1,000 | 12.256 ms | 0.365 ms | 10.275 / 0.178 ms |
| 10,000 | 108.834 ms | 0.146 ms | 105.913 / 0.188 ms |
| 100,000 | 1,058.280 ms | 0.188 ms | 1,028.727 / 0.183 ms |

The writer begins when the actual full-audit SELECT starts. At 100k, strict
verification takes 1,094 ms while the WAL writer commits in 0.188 ms. The
candidate still replays 401,579 audit rows after an unknown external change.
One own append checks one new audit row; scoped protocol mirrors check five
rows / one run and one invocation receipt.

Cold constructor/index migration is separate: 1,637 ms at 100k in the controlled
candidate probe. Strict cold and external verification remain proportional to
history. All eight measured ledgers pass deep accounting and quick_check; paired
audit heads match and original context, permissions, allocation and exact Decimal
spending remain. The original closed fixtures are unchanged. A 100k setup that
hit the normal 512 MiB storage limit is retained; its successful retry uses an
explicit experiment-process-only 8 GiB storage allocation, without changing task
limits, prior spending or user settings.

## Device validation and retained counterexample

The first WAL-only co-run is retained rather than presented as a full solution.
It reproduced 1.8–2.0 s NPU BEGIN IMMEDIATE waits. Exact traces linked them to a
fresh gateway's lazy accounting rebuild inside its writer transaction; a
history-bearing continuation also replayed the global page audit there. These
observations motivated ledger borrowing and explicit recovery-profile carry-over.
All of that run's context, permissions and spending remain in its audited ledger.

Final paired device measurements and independent audit results are retained in
the adjacent `Sutra Receipt Verification Evaluation` directory. The device lane
uses the existing native GPU model and the research NPU encoder. This release
ships the host verification/gateway fixes; it does not silently enable the
private-interface NPU retrieval prototype in VS Code.

The final run starts with 10,000 settled host requests and explicitly selects
the controller-authenticated seal/audit-prefix profile. It runs three matched
GPU-alone/concurrent pairs with fresh source checks over 220 passages. Identical
prompts and answers are retained; the GPU model/runtime identity stays fixed.

| Phase | Concurrent queries | End-to-end median / p95 / maximum |
| --- | ---: | ---: |
| Pair 1 | 379 | 10.446 / 13.297 / 28.496 ms |
| Pair 2 | 375 | 10.380 / 13.275 / 30.784 ms |
| Pair 3 | 375 | 10.520 / 13.064 / 25.200 ms |
| Saved-history continuation | 236 | 10.245 / 13.043 / 17.552 ms |
| Paced NPU-only control | 100 | 9.743 / 13.044 / 20.991 ms |

The longest recorded BEGIN IMMEDIATE acquisition is 3.848 ms over 4,804
acquisitions; the saved-history continuation's maximum is 0.087 ms. No paired
phase or memory-bearing finalization repeats the earlier second-scale stalls.
The control uses an explicitly restarted identical encoder with its startup
charged. Burst saturation results are kept separately, not used as a paced
latency baseline.

GPU end-to-end means are 25.608 s alone and 25.329 s concurrent in three pairs.
This small difference is not a GPU acceleration claim. Historical comparisons
also differ in instrumentation, frozen source index and runtime restart; the
controlled host matrix supplies the causal reader/writer evidence.

Separate live medians: pure kernel 1.208 us, accounting seal 2.618 ms, protocol
verification 0.462 ms, invocation verification 0.853 ms and anchored audit-tail
verification 0.402 ms. Gateway prewarming runs before the writer transaction;
its observed maximum is 92.2 ms, rather than hiding that work in model time.

Queued/native/GPU cancellation and continued recall pass. The final independent
strict read-only audit passes 36,301 checks over 1,548 NPU jobs and nine GPU
children, with zero pending charges. Exact context, permissions and original
limits remain; cumulative spending is 12,215 call units, 620,564 token units,
707,079,700 source bytes and 394.571 seconds. Declared synthetic/encoder units are
distinguished from provider-measured model tokens in the retained receipts.

Cold/setup/recovery work remains visible: strict replays of 1.28–1.89 seconds
occur during setup and cancellation reconciliation, outside the paired and
parented model finalization paths. Full deep audits and unanchored verification
are preserved; the result is a bounded steady receipt path under the explicitly
chosen profile, not a claim that historical verification became free.

The release makes no universal constant-latency claim. Strict cold/deep audits,
long individual sessions, large active scopes and lagging explicit anchors still
have real costs. Kernel, seal, audit, gateway-prewarm, SQL lock and model timing
measurements are kept separate.

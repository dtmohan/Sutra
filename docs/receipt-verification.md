# Receipt verification and concurrent work

Sutra 0.50.1 separates verification reads from receipt-write contention. It does
not weaken the protocol, source checks, permissions or spending rules. The
canonical STP kernel is unchanged.

## Read snapshots and durability

Local ledgers use SQLite WAL with `synchronous=FULL`. A verification transaction
reads one consistent snapshot while another connection can commit a receipt.
There is still one SQLite writer at a time; WAL is not a claim of unlimited
write concurrency. The existing bounded accounting projection remains separate.

An older rollback-journal ledger transitions on open. If another reader/writer
prevents that transition, the host defers migration after a short attempt and
retains the original mode and timeout. It never interrupts a task or weakens
durability to obtain a faster measurement. The next open can retry. The store's
`_sqlite_journal` diagnostic reports the actual mode and deferred status.

WAL can retain committed data in `-wal` and `-shm` sidecars. Back up a live ledger
with SQLite's online backup API; copying only `state.sqlite` is insufficient.
Background detailed audits use that API, then normalize their private destination
to rollback-journal mode before reopening it with an OS read-only connection.
This keeps snapshots independently readable on the tested Apple SQLite runtime.

## Work avoided and work retained

Protocol audit mirrors have partial indexes by session and run. Verification
checks only the requested session's runs, assertions and invocation receipts,
while preserving every original hash, mirrored-event and completeness check.
Invocation verification holds one read snapshot for both protocol and receipt
checks, so a concurrent new invocation cannot create a false incomplete result.

The page-audit verifier can reuse a previously checked immutable prefix on the
same connection and hash only its own appended tail. File identity, chain
positions, immutable guards, schema and local mutation observations remain part
of the decision. Rollback, guard changes, malformed rows and unknown external
commits invalidate ordinary reuse. A new connection/process starts strict; no
persistent self-trusted prefix marker is introduced. `audit_valid(deep=True)`
always replays the full chain, including during explicit deep accounting audits.

## Retain the caller's verified context

Modern host callers can pass `InvocationGateway(..., ledger=s)` to borrow their
existing open ledger. The gateway validates database path/identity, policy,
run/session and orchestration scope. It rejects an outstanding caller transaction,
holds no transaction during inference, rechecks the binding afterward, and never
closes a borrowed connection. The existing context, permissions, reservations and
selected recovery profile remain in the same host object.

A caller that already supplies an independently trusted V2 accounting seal may
explicitly choose `recover(s, trusted_anchor=head, audit_prefix=True)`. That same
seal authenticates its `audit_cursor`; the header and exact checkpoint audit row
are checked before the profile is enabled. This opts into trusting that historical
prefix, rather than re-reading its source bytes on each receipt. After an external
commit the verifier rehashes every canonical row after the ORIGINAL selected
cursor. It never promotes a later SQLite seal to a new trust root automatically.

The profile is bound to the current ledger, connection, policy and selected seal.
Removing/changing the anchor invalidates cached proofs. Corruption within the
untrusted suffix is rejected; a deep audit also re-derives the trusted historical
prefix and can detect its corruption. Trusting a head does not authenticate a
privileged writer that rewrites the entire source history. No NAS, protected head
writer or external signing service is installed by this release. Without explicit
host selection, accounting recovery and page-audit verification remain strict.

Unknown external changes in strict mode can therefore still require a full scan. WAL
keeps that strict read from holding back another connection's receipt commit.
Verification of a long individual session still scales with that session's
receipts. Cold index migration and full historical audits remain separate costs;
the release does not describe all verification as constant time.

## Diagnostics and validation

Bounded timing buffers distinguish `page_audit_full`, `page_audit_tail`,
`page_audit_cache`, `protocol_verification`, `invocation_verification` and
`sqlite_journal_setup`. Observers receive scanned-row counts and invalidation
reasons. Existing kernel, budget and accounting-seal timings stay separate.
Experiment-only instrumentation also times SQLite begin/commit boundaries and
GPU child finalization; those costs are not presented as model or NPU execution.

Regression coverage includes malformed/low sequence inserts after prefix reuse,
same/other-connection tampering with restored schema versions, rollback and file
replacement, session isolation, incomplete receipt checks, caller cursor
preservation, concurrent WAL commits, busy legacy migration and independent
read-only backup audits. Long-run results and remaining limits are recorded in
the source release validation report and the adjacent evaluation directory.

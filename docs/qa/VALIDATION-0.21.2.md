# Sutra 0.21.2 validation

The user observed delegated reasoning briefly and then could only find the initial routing call's thinking. The extension streamed distinct invocation IDs, but native VS Code owns the presentation and grouping of thinking parts. Sutra's Monitor retained only an in-memory list, and compact disk traces intentionally omitted response bodies. Those older vanished streams cannot be reconstructed.

## Change

Each new call has a host-generated reasoning-details link in native chat and an Open reasoning details button in the Monitor. It opens a read-only plain-text document with that invocation's model, stage, task, status, token counts and emitted commentary. Open documents refresh during generation. Conversation/routing, child review and workspace review are labelled separately. Native thinking streams now also receive explicit completion markers.

Diagnostic history is stored separately from task memory and ledger evidence. Per workspace it is bounded to 12 calls, 7 days, 1 MiB and 16,000 characters per call; pruning happens on access/write. Atomic writes use private file permissions. Loading rejects oversized, malformed and unsafe files. Reloading an unfinished record labels it interrupted, never accepted. Disk failures leave live reasoning usable and show a retention error. Raw reasoning never becomes an answer, permission, task-memory page or test proof.

## Results

- All 14 release gates passed, including 444 Python tests, Node suites, browser webviews, packaging/integrity checks and the packaged VS Code host.
- Archive tests cover reload retention, parent/child and model identity, withheld/interrupted states, count/age/UTF-8 serialization bounds, omitted prompt fields, corrupt and symlinked files, write failures and workspace isolation.
- Monitor integration tests cover per-call documents, live content-change events, open-details navigation, reload and workspace changes. Browser checks preserve expanded/focused child cards during updates, keep model text inert, and verify the details button and retention error.
- Real VS Code 1.146.0 API tests emitted three calls in one response (router, child, reviewer), then switched from the low to high tier. Each response had three distinct IDs, three links and three completion markers. Reasoning remained outside ordinary answer text.
- A packaged extension test opened restored child details through the real document provider, checked plain-text language, separate content, interrupted status and rejection of another workspace's link.
- Existing native-chat regressions still verify context, task bindings, remaining budgets and permission constraints across model switches and reloads.
- The STP kernel is byte-identical: `9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`. Harness Python, policy hashes and inference transport are unchanged.

These are transport, persistence and UI tests using deterministic model-event fixtures; no new live-model inference was needed for this display fix. Native chat's visual grouping is still controlled by VS Code. The per-call documents provide a separate retained view regardless of that grouping. The user's live task ledger was not modified.

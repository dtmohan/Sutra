# Sutra 0.21.3 validation

The reported invocation `d717d2d8ae9240dda712db9bb66ded4a` was a final `chat_reviewer`, not a child invocation. Its saved delivery contained no child reports, one 1,024-byte excerpt from `harness/policy_check.py`, and four static call sites from `diagram_view.js`. Although 113 files had been mapped, only one mapping page was selected for synthesis. Legacy `pages: []`, `handoff: null`, and `implementation_present: false` fields also encouraged the model to mistake missing optional memory or created artifacts for absent repository source.

## Change

Parent synthesis now takes fresh snapshots across the mapped source partitions, with a bounded file/module inventory, representative excerpts and measured evidence coverage. Entry points and production modules with many static call sites are preferred for architecture requests; tests/examples do not displace production entry points. Retrieval includes the pinned goal so a follow-up such as “retry the original request” retains the architecture objective. Static facts prefer resolved calls and distribute examples across excerpt files.

Host-supplied evidence is explicitly distinguished from direct model tools, optional Smriti pages and harness-created artifacts. Child synthesis carries the admitted child answer, coverage and freshly revalidated citations, all still labelled as unverified commentary. Structural inventory omissions and sampled implementation coverage remain explicit; no exhaustive semantic-review claim is made.

Review packets use installed Ollama context metadata, bounded by remaining task and ancestor token allocations with capacity reserved for up to three attempts. The existing gateway and STP kernel still admit and charge each call. Citation grammar now selects complete path/quote pairs, preventing a valid quote from being paired with another file. Existing independent source-freshness and quote checks remain active.

## Validation

- All 14 release gates passed, including 448 Python tests, native-provider regressions, browser checks, packaging integrity and the packaged VS Code host.
- New regressions cover source coverage across split parser work units, altered inventories/capability metadata, snapshot omission, model-capacity allocation without budget reset, entry-point retrieval and explicitly omitted structural details. Existing source-change, permission, quarantine and model-switch tests pass.
- Child/model-switch tests verify that the same task contract, permissions, parent context and consumed allocations survive, and that synthesis receives child answers and evidence coverage.
- The live saved session was opened read-only and copied using SQLite backup. Only the copy was remapped and charged; no live task ledger or project source was changed.
- An initial live run produced architecture prose but paired quotes with incorrect files. All three attempts were quarantined. This prompted the bound citation grammar; validators were not weakened.
- The final live rerun used the original failing request and its saved conversation handoff with `local-assistant:latest`. It remapped all 113 supported source files, delivered their complete file inventory, details for 11 modules and 6,728 excerpt bytes from six production files, and returned an admitted architecture overview with strengths and limitations on the first call.
- Measured usage: 8,933 input / 653 output tokens. Conservative input charge: 35,852; output reserve: 4,096; allocated context: 40,368. The original contract and limits were unchanged. Copied task usage increased from 9 to 10 calls and from 128,894 to 168,842 charged tokens; it was not reset.
- The STP kernel remains byte-identical: `9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`.

The live check exercised the direct-synthesis fallback, with zero newly joined child reports. Child joining and model switching were checked by deterministic regressions. A complete semantic audit of all 113 files was not performed. The live test requested reasoning off; optional reasoning display is unchanged from 0.21.2. Known 0.21.1/0.21.2 conversation policies remain compatible for data-only memory. Older executable workspace results remain stale; pending old-policy workflows use the existing explicit restart path while retaining consumed budgets and constraints.

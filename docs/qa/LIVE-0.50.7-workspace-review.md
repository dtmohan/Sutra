# Native VS Code architecture review: 0.50.7

Date: 2026-11-19. Existing signed-in VS Code window, existing chat and saved
review. Model calls were initiated through native chat controls; observation of
receipts and source was read-only. The chat picker was deliberately set to Sutra
High. Configured Low/Mid/High model settings were preserved.

Prompt: “can you review the workspace project and go through the tools logic
and help me understand the architecture in detail? a few block diagrams would
be great”

## Completion and accounting

The user approved increasing the machine-scoped, per-workspace history allowance
from 512 to 1,024 MiB. It was changed through native Settings UI; no evidence was
deleted. The previous 499 MiB record plus a 16 MiB admission reserve legitimately
exceeded the former limit.

The saved workflow completed in 8 minutes 26 seconds. Six High child calls, one
High evidence planner, two High synthesis attempts and one Low claim review ran.
The first synthesis was rejected because it nested the optional diagnostic
`task_observation` inside `report`; its repair was accepted.

Spending settled at 135,642, including all 82,014 previously consumed units and
53,628 for this trial. No invocation or orchestration charge remained pending.
All 15 invocation-envelope/receipt hashes verified. An import dependency diagram
opened beside chat, and actual provider reasoning appeared inline for synthesis.
No reasoning text was invented for calls that requested thinking off.

## Manual quality verdict: failed

Workflow completion and source provenance did not establish a correct report.
The published narrative contained unsupported relationships that the Low reviewer
approved, including:

- `conversationDetails` calling `api_runtime`: the cited extension code renders
  an already-returned response.
- `api_runtime.invoke` calling the capability registry: the implementation calls
  `InvocationGateway`.
- `audit_dag.audit` calling accounting admission: the cited audit implementation
  contains no such call.

The narrative omitted the requested core orchestration/gateway/kernel/tools and
memory path. Its single diagram showed local imports, not the requested request
flow. Static diagnostics and unresolved calls were also overinterpreted.

## Diagnosed delivery causes

Child slices repeated nearly the same source ranking despite accepted requests
for other authorized modules. Some excerpts were only whitespace. Parent source
delivery totaled 1,464 bytes, while unverified child notes occupied 8,619 bytes.
Several parent excerpts ended mid-expression. Citation IDs authenticated the
locations but could not make those fragments support the model's conclusions.

0.50.8 targets evidence paging and model-facing duplication. It does not replace
semantic judgment with a stronger claim about schema or provenance checks. A new
native UI trial and manual source check are required before declaring quality
improved. The canonical STP kernel remains unchanged.

# Sutra 0.51.2: native workspace review and child recovery

Date: 2026-11-19. Tested in the existing signed-in VS Code window, against the frozen 0.51.2 checkout. This was a real chat submission, not a direct bridge or provider invocation.

## Result

The review did not reach a report. High rejected three child generations as invalid JSON. Switching the same saved request to Mid produced four accepted child checkpoints, then exposed a host allocation recovery defect: a child short of execution seconds could not increase its allocation while its parent waited for children. No model invocation or reservation remained pending. The native Increase Task Allocation command also displayed `Finish or discard pending child work before renewing execution capacity`. These results do not establish architecture answer quality.

The 0.51.2 package passed 26 QA gates, including 1,347 Python tests (8 skipped), before this live trial. That validation did not cover the observed non-token child allocation path. The STP kernel SHA-256 remained `9dfef2479b9af1b8f82282597f13b1205c31b19ba4ff52f60e305f1757ed4eac`.

## Reproduction and retained state

Prompt: “can you review the workspace project and go through the tools logic and help me understand the architecture in detail? a few block diagrams would be great”

- Flow: `e7a5d038fcc14b4db925b3c2f201a6ed`.
- New task: `review-493c39377419`, initially 250,000 model budget units. This was a fresh review; earlier completed or withheld tasks retained their own spending.
- Actual chat submission created the flow at Unix time `1795125155.7127`. The earlier Send-button gesture did not dispatch a request and is excluded from latency.
- Classifier: `287c115ccb78487fac2de5669550c317`, Low `qwen3:4b`, accepted, 2,308 input / 323 output tokens, 104.913 seconds elapsed.
- High child attempts: `396476aef60f4ca782f27bc8c2a515f8`, `14013a65449b44ceb2072e82057d2c24`, `e116e4ed26a54a4698bf872ef9c4df4f`. All quarantined on JSON parsing at character zero; total measured charge 30,419 tokens. No evidence establishes whether the rejected text was fenced JSON, prose or another format.
- After the normal stopped-child response, the idle Sutra-owned High runtime was stopped through its native UI command. The saved model tier preferences were unchanged. The chat picker was changed to Mid and `continue` submitted in the same chat.
- Replacement branch: `61269342cdab4dc9926f82f90514996f`; accepted Mid checkpoints: `2fb3c0ec89ad4856825dc5d2b1aff2f9`, `d3dd0fd38a294012a9094e80669a161a`, `21e3687bf3464af286cd20f679833fa2`, `728dcd2bfd1a470d84a4cd6d27ef9890`. Total 18,996 measured tokens. The first Mid call took 57.464 seconds, including about 2.119 seconds of cold loading.
- Before checkpoint five: branch seconds limit 372.931764888; consumed 276.02256995799985; next call reserved 150 seconds. Task spending was 49,415 settled tokens, zero pending reservations. Automatic recovery requested 55 additional seconds and returned `pending_execution`.
- The four accepted checkpoints and their source spans remain in the ledger. Other historical review receipt prefixes were checked and unchanged; no charge was refunded or reset.

## Latency observations

The initial Low call's 104.913-second elapsed time was mostly backend queue/loading delay: approximately 98.412 seconds reported as load duration, versus 1.343 seconds prompt processing and 5.100 seconds generation. Two VS Code background requests had arrived ahead of it on the same single-slot Ollama runner. Their output counts were 2,427 and 4,239 tokens; the available logs do not separate reasoning from answer tokens. These external requests are not Sutra-owned task spending.

With explicit user approval, VS Code's `chat.utilityModel` and `chat.utilitySmallModel` were both changed from the large local assistant to `ollama-models/qwen3:4b`. Sutra Low/Mid/High preferences were preserved. No new background utility calls accompanied the later Continue action, so the Mid child continuation delay was not attributed to those utilities.

## Confirmed follow-up work

1. Generalize the existing in-place child allocation amendment beyond tokens, preserving checkpoints and settled spending and checking the same ancestor gates.
2. Include only actually delivered static source statements in versioned claim-review evidence. The writer currently receives some statements that the reviewer inventory omits; a static reference still does not establish runtime execution.
3. Bind report/claim transport deadlines to the host's admitted duration. An 8,192-token output reservation and a fixed 150-second deadline are inconsistent with the measured Mid generation rate near 13 tokens/second. A longer bounded slice is not a guarantee that the full reserve can be generated.

Acceptance still requires a readable, source-supported report tracing the native entry, orchestration and permission checks, gateway/STP enforcement, repository effects, continuity/checkpoints, and claim publication, with diagrams that agree with those paths. Schema acceptance alone does not meet this criterion.

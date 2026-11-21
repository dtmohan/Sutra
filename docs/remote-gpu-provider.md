# Remote NVIDIA provider

Sutra 0.56.0 can send governed model calls to a Windows NVIDIA machine through
an owned SSH tunnel and a private llama.cpp endpoint. The canonical STP kernel
is unchanged. Remote inference uses the same invocation gateway, task contract,
source custody, output checks and cumulative spending receipts as local inference.

The remote machine performs generation. Repository reads, edits, test execution
and their permission/resource gates still run on the workspace host. Selecting a
remote model does not grant the model network tools or access to additional files.

## Selecting the RTX provider

The configured RTX 3090 appears as `sutra:remote/rtx3090` in **Configure Models**.
Assign it to a tier in User settings to opt in. Discovery preserves the existing
Low, Mid and High choices; it never selects a remote profile as a new default.
Workspace settings alone cannot authorize remote inference. A queued durable
request keeps its captured model configuration. Stop it before explicitly resuming
with another configuration; its context, permissions and spending remain saved.

The user registry defaults to `Remote/models.json` in Sutra's application-support
directory. `karma.remoteRegistry` is a User-only application setting for another
absolute registry path. Profiles and API credentials are not included in the VSIX.
The registry must remain a user-owned regular file with private write permissions;
the API key must be readable only by its owner. Keep both outside the workspace.
Profiles are trusted same-user host configuration, not protection against a
privileged caller modifying the machine.

Each profile binds an SSH configuration alias, local and remote loopback ports,
API-key file, server model/path/digest, allocated context, GPU identity, runtime
executable digest, and GPU/RAM reserves. It can also bind a full restoration
allocation and the context size that allocation covers. The SSH alias uses an
existing key and pinned host key; Sutra never asks a model for credentials or a
connection destination. An occupied forwarding port is rejected unless it belongs
to Sutra's matching SSH master. Context-policy changes reuse a tunnel only when
its connection authority is unchanged.

## What admission measures

Every dispatch refreshes NVIDIA GPU capacity, remote host RAM, listener/process
identity, model-file identity, runtime allocation and idle-slot state. A fresh
nonce binds the response to the current inspection. GPU and host RAM are separate
Karma memory-allocation frames; the stricter observation governs admission.
Unknown, changed, expired or busy observations stop before generation.

A matching loaded context pool is already reflected in current free memory.
Sutra reserves additional working headroom rather than charging its weights a
second time. NVML process presence does not prove that every model byte is
physically resident. If GPU residency cannot be established, Sutra requires an
explicit full GPU and host-RAM restoration envelope covering the configured
context. It reserves that full amount plus working headroom, without resident
credit. Missing or insufficient coverage causes BRIDGE. The configured envelope
is a planning bound, not verified inference-peak memory or an attestation of the
remote host.

A bounded RAM checkpoint can reuse a previously computed weight digest only for
the same profile, host boot, listener process/start, executable and file metadata.
A process restart or ordinary file change invalidates it. Capacity and activity
are always measured afresh. The checkpoint is not restored from a diagnostic file.
The exact runtime instance is checked around inference; admission freshness is
checked again immediately before POST. A rejected result retains its spending.

## Context and diagnostics

The current RTX profile uses a measured 32,768-token resident context allocation
and one generation slot. A smaller client working set can page optional history
without changing that physical pool. A larger advertised training window does
not expand either the server allocation or the task budget. New model calls still
need remaining task/parent capacity and retain all pinned constraints.

The copied `qwen3:4b` weights identify themselves as Qwen3-4B-Thinking-2507.
The provider preserves its thinking-only template and native sampling defaults;
unsupported Off requests are disclosed as model-default behavior. Reasoning streams
inline when supplied, and both reasoning and the final answer count toward output
spending. Matching GGUF weights across hosts count as the same reviewer weights.

Sutra Monitor has a separate **Remote GPU** section. It labels the last admission
and observation time, hides expired capacity values, and never places remote VRAM
under Local runtime memory. Readiness and recovery only inspect conditions; they
do not grant permissions, reset spending, silently release a shared model or replay
an uncertain call.

This version targets a fixed, already loaded Windows llama.cpp/NVIDIA profile.
The configured Windows startup task manages server startup. Cold model replacement,
automatic downloads and general Linux/CUDA fleet management are outside this
provider's scope.

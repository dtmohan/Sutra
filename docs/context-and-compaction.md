# Context and compaction

VS Code sizes the assembled chat: instructions, tools, history, attachments and
the current request. A one-sentence request can arrive in a nearly full chat.
Compaction is a capacity operation, not a time expiry. VS Code reserves room for
output and applies its own threshold. Sutra does not disable that mechanism.

Before 0.18.0, Sutra's `provideTokenCount` returned UTF-8 byte length. English
text and code could therefore appear much larger than their tokenized size.
Version 0.18.0 uses an offline cl100k_base BPE estimate with 15% headroom. It is
approximate for Ollama models. It changes only the VS Code sizing callback; the
64,000-token advertised host input window, measured receipts and conservative
backend admission checks stay unchanged. No prompt text or response is cached.

The installed VS Code harness issues a known conversation-summary instruction
when compacting. Sutra recognizes that instruction and returns a signed local
receipt pointer without a model call. A later invocation revalidates the pointer.
If no authenticated checkpoint can be recovered, Sutra reports missing context.
This integration depends on the recognized host instruction; future host changes
may require compatibility updates.

The compact model input is different from VS Code's assembled history. Sutra
passes the current extracted user request as the user message, with its approved
contract and compact memory in a separate system context. Ordinary conversation
memory is the last admitted model-written summary, capped at 1,000 characters.
Diagram references retain selected original user wording separately. Workspace
goals, permissions and constraints are stored and validated separately from prose.

| Current choice | Benefit | Limitation |
| --- | --- | --- |
| Fresh model calls with compact context | Lower input cost; portable across models | Older conversational nuance can be lost |
| Signed checkpoint and versioned contracts | Verifiable ancestry and stable workspace constraints | Authenticity does not establish semantic completeness |
| Host compaction intercepted as a pointer | No extra local summarization call or repeated summary-of-summary | Depends on host request format and checkpoint availability |
| Approximate host tokenizer | Avoids byte inflation without runtime network calls | Not an exact tokenizer for every installed model |

The monitor's token cards are measured model-call counts, not VS Code's current
history size. The left-hand total can aggregate several calls. Neither quantity
by itself establishes that the host is near its compaction threshold.

A future fidelity improvement is bounded verbatim recent turns plus explicit
user facts/constraints and retrieval of older evidence. Disabling compaction or
raising the advertised window arbitrarily would not repair missing summary facts.

References: [VS Code context](https://code.visualstudio.com/docs/agents/concepts/context)
and [session compaction](https://code.visualstudio.com/docs/agents/run/sessions/manage-sessions).

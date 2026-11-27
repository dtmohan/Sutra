Sutra 0.61.2 — Local AI for VS Code

Use Sutra for local-model chat, source-linked codebase reviews and approved
workspace actions such as Python tests or browser checks.

GET STARTED

1. Use VS Code 1.104 or newer and Python 3.9 or newer. Run Extensions: Install
   from VSIX, choose the Sutra package and reload VS Code.
2. Start a supported model runtime with at least one local chat model. With
   Ollama, use an existing model or download one with ollama pull qwen3:4b.
   Sutra discovers eligible models. Find Ollama at https://ollama.com/download.
3. Run Sutra: Open Chat (Sutra Auto). Check that Sutra Auto appears beneath
   the prompt. If Sutra is off, click it in the status bar and choose Enable
   and open Sutra chat.

TRY A WORKSPACE REVIEW

Open a trusted local folder and ask “Review this codebase and give me a
high-level overview of its architecture.” Approve read access when prompted.
The request continues in the same chat. Sutra remembers the workspace grant.
Use Sutra: Workspace Access… to review it or Sutra: Revoke Workspace Access
to remove it. Edits, tests and browser checks need separate confirmation.

SETTINGS AND RECOVERY

- Click Sutra in the status bar for chat, workspace access, model and
  reasoning controls. Run Sutra: Configure Local Models to set the tiers.
- Adaptive reasoning is the default. Use Sutra: Configure Reasoning for
  preferences or the Chat lightbulb to request thinking for one turn.
- Click Sutra Monitor for status and usage. Send continue in the original
  chat to reconnect to a saved request. Use Sutra: Stop Runner Request to
  cancel active work.
- If Python is in a custom location, set karma.pythonPath in VS Code
  Settings. Sutra retains existing karma.* Settings keys for compatibility.

The repository README has the full guide. See https://github.com/dtmohan/Sutra.
Bundled components retain their licenses. See THIRD_PARTY_NOTICES.md.

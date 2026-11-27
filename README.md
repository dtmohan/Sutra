# Sutra 0.61.1 — Local AI for VS Code

Sutra brings local-model chat and codebase review to VS Code. Ask a question,
get a source-linked explanation of a project, or approve a supported action
such as a Python test or browser check. **Sutra Auto** picks from your configured
models and keeps approved scope, checkpoints and usage with the saved task.

## Get started

1. **Install Sutra.** Use desktop VS Code 1.104 or newer and Python 3.9 or newer.
   In the Command Palette, run **Extensions: Install from VSIX**, select your
   Sutra package, and reload VS Code. To build a VSIX from this checkout, run
   `python3 -B package.py --output dist`.
   [VS Code installation instructions](https://code.visualstudio.com/docs/configure/extensions/extension-marketplace#_install-from-a-vsix)
2. **Connect a model.** One installed chat model is enough. For the simplest
   setup, start Ollama and use an existing local model. If you need one,
   `ollama pull qwen3:4b` downloads a starter model. Sutra discovers eligible
   models and fills any empty tier choices.
   [Ollama download](https://ollama.com/download) · [Qwen3 4B](https://ollama.com/library/qwen3:4b)
3. **Open Sutra Auto.** Run **Sutra: Open Chat (Sutra Auto)** and check that
   **Sutra Auto** appears beneath the prompt. Open your project as a trusted
   local folder to try a workspace review. If the status bar shows Sutra is off,
   click it and choose **Enable and open Sutra chat**.

## Try a request

| Ask in Sutra Auto | What to expect |
| --- | --- |
| “Explain the difference between an interface and an implementation.” | A plain chat answer. |
| “Review this codebase and give me a high-level overview of its architecture.” | Approve read access when asked. The review continues in the same chat and cites the source it inspected. |
| “Review `notes.docx` and summarize the decisions.” | For a file in the open workspace, approve the named document read. |
| “Launch `index.html` and critique the code based on observed behavior.” | On macOS with Google Chrome installed, choose **Run browser checks**. Sutra shows the offline browser observations before the critique. This workflow supports one standalone UTF-8 HTML file up to 256 KiB. |
| “Update `app.py` and `config.py` to use the same timeout default and run `python3 -m unittest discover -s tests`.” | On macOS, start from a clean Git checkout with tracked tests under `tests/`. With **Workspace agent** access, review and approve the proposed diff before Sutra stages it and runs tests in an isolated worktree. |

**Read only** grants scoped inspection. **Workspace agent** also allows Sutra to
propose supported edits and tests for separate confirmation. Browser execution
has its own approval. Sutra remembers the workspace grant across reloads. Use
**Sutra: Workspace Access…** to review it or **Sutra: Revoke Workspace Access**
to remove it.

## Settings you’ll use

Click **Sutra** in the status bar for quick controls. The Command Palette provides
the same entry points when you need them.

| To change | Where |
| --- | --- |
| Which models Sutra uses | **Sutra: Configure Local Models**. Assign Low, Mid and High as needed. In Chat, choose **Sutra Auto** for automatic routing or **Sutra Low**, **Sutra Mid** or **Sutra High** to use a fixed tier. |
| Reasoning effort | **Sutra: Configure Reasoning**. **Adaptive** is the default. Fixed tiers inherit it unless you choose an override. Available controls depend on the model. |
| Reasoning for one request | Use the Chat lightbulb or **Sutra: Think on Next Request**. This applies to planning and answering for the next request. Child calls use their configured settings. |
| Which Python Sutra runs | Search VS Code Settings for `karma.pythonPath`. An empty value uses `python3` on macOS and Linux or `python` on Windows. |
| Optional tools | **Sutra: Configure Tool Policy**. Ruff and Pyright prompt for approval by default. |

The `karma.*` Settings keys preserve compatibility with earlier Sutra
installations. Opting in to remote inference sends model input to the configured
host. Workspace reads, edits and tests run on the workspace host.
[Owned engine worker setup](docs/owned-engine-worker.md#set-up-and-select-a-model) ·
[Remote GPU setup](docs/remote-gpu-provider.md#selecting-the-rtx-provider) ·
[Tool permissions](docs/capabilities.md#workspace-policies)

## Check progress or resume

- **See what happened.** Click **Sutra Monitor** or run **Sutra: Open Monitor**
  for request status, model calls, usage and resource details. Browser and test
  receipts record the observed behavior. Use the source links to check the
  model’s interpretation.
- **Continue in the same chat.** After a reload, send `continue` in the
  originating chat to reconnect to a saved request. Eligible approved work can
  continue while the chat window is closed. Use **Sutra: Stop Runner Request**
  to cancel the runner.
- **Switch models on a saved task.** Stop the active runner or wait for a pause,
  select an eligible Sutra tier, and send `continue` in the original chat.
  Checkpoints, permissions and spent budget carry forward.
- **If setup stalls.** If Sutra Auto is missing, open **Sutra: Open Chat (Sutra Auto)**
  and reload if needed. If the model list is empty, start your model runtime,
  check that a local chat model is installed, then retry model discovery.
  VS Code can show its own Copilot setup prompt before sending the first Local
  chat message to Sutra. Sutra inference uses your configured provider.

Follow any approval, resource or allocation action shown in chat before
resuming a paused task. [Runner recovery and Stop](docs/durable-task-runner.md#reload-stop-and-model-changes) ·
[Task allocation](docs/allocation-recovery.md) · [Known limitations](docs/known-limitations.md)

## Project references

[Release notes](CHANGELOG.md) · [Validation and QA](docs/qa/) ·
[Development](docs/development.md) · [Orchestration internals](docs/ORCHESTRATION.md)

This repository does not grant an open-source license for original project
code. Bundled components retain their respective licenses.
[Third-party notices](THIRD_PARTY_NOTICES.md)

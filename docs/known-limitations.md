# Known limitations

- Model explanations can contain unsupported factual claims. Structural diagram
  validation does not establish the truth of device specifications or connections.
- A generated diagram can merge distinct devices unless identity constraints are
  preserved and checked. Future work: verify user-stated node identities and edges
  independently before publication.
- Conversation-role calls currently have no web tools. Permission in a prompt
  alone cannot supply a missing search capability. Future work: explicit governed
  web access with source receipts and a visible network state.
- Native chat compaction relies on signed pointers and, when supplied by VS Code,
  a per-chat binding hint. Missing authenticated context must be reported rather
  than filled with another chat's most recent context.
- A zero test-process exit code is an observation, not independent proof that
  assertions are correct or even ran. Source-controlled test output is untrusted.
- Editing/test execution is limited to supported scoped Python operations.
  The Windows adapter is currently read-only.
- Temperature sensor mapping is verified for the original Apple Silicon test
  machine. Celsius values are advisory; resource admission uses OS pressure and
  memory measurements.
- Private repository access and extension installation do not require or imply
  publication to the VS Code Marketplace.

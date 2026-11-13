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
- Conversation continuation currently uses the last admitted model-written
- Conversation working memory is sized from the selected model/context allocation, with recent turns and selected Smriti pages. The complete transcript stays in the ledger; selected context is not a semantic completeness guarantee.
  A valid checkpoint authenticates this memory; it does not prove it retained all
  facts. Workspace contracts are stored separately and checked on each invocation.
- VS Code host token sizing uses cl100k_base plus 15% headroom as a local-model
  approximation. It is not the model's exact tokenizer or the harness budget.
  Large instructions, tools or history can still legitimately trigger compaction.
- A zero test-process exit code is an observation, not independent proof that
  assertions are correct or even ran. Source-controlled test output is untrusted.
- Editing/test execution is limited to supported scoped Python operations.
  The Windows adapter is currently read-only.
- Temperature sensor mapping is verified for the original Apple Silicon test
  machine. Celsius values are advisory; resource admission uses OS pressure and
  memory measurements.
- Private repository access and extension installation do not require or imply
  publication to the VS Code Marketplace.

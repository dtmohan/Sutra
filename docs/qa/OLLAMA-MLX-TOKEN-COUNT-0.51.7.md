# Ollama/MLX token-count investigation, 0.51.7

Date: 2026-11-19. Read-only inspection of the installed local runtime; no inference, model load/unload, settings changes or database writes.

## Finding

No authoritative count-only endpoint was identified for this installed **Ollama 0.40.16** build. Exact model-bound tokenizer assets exist, but they do not by themselves prove the token count of the input that the runtime actually renders. This finding is specific to the inspected modified build, not a claim about every Ollama version or deployment.

The 0.51.7 writer reserved **10,117 conservative input units** and later reported **1,287 measured input tokens**. Financial reservation deliberately retains a conservative input bound. That bound is not a measurement that the physical model window is saturated. Treating a resource-configured 16,379-token cap as the same number of UTF-8 reservation units caused source fitting to leave only 449 bytes for this candidate; it does not establish that a larger source packet would exceed the physical token window. No byte-to-token ratio was adopted as a replacement proof.

## Inspected runtime and model identity

- Loopback listener: `127.0.0.1:11434`, served by `/Applications/Ollama.app/Contents/Resources/ollama serve`.
- `/api/version`: `0.40.16`. Embedded build information: revision `d63b702609c4300a2ca07be2bdb0adb9834725e5`, `vcs.modified=true`, Go `1.26.10`. Matching source for this modified binary was not found in the application bundle or inspected workspace.
- `local-assistant:latest`: MLX safetensors, architecture `qwen3_5`, 27,781,081,984 parameters. The API manifest digest exactly matches the SHA-256 of `~/.ollama/models/manifests-v2/ollama.com/library/local-assistant/latest`: `e5403b67f03bf9a90e33c6ec929ae1fa4d709199bcf6a4925d44bcf433a0d88f`.
- Its tokenizer assets match the installed `qwen3.8:27b` manifest, whose API digest is `47951e4a666180bc2aedfc93c169472f2de783696fe196efbf063f5730ecf163`.

The alias manifest binds these assets in `~/.ollama/models/blobs/sha256-<digest>`; the tokenizer JSON and tokenizer-config content hashes were also checked against their declared digests:

| Asset | Manifest SHA-256 | Declared bytes |
| --- | --- | ---: |
| `tokenizer.json` | `0997f410c57a1f4e53b09e4be8f4a172d90edd9564368fb0847030937229b9f3` | 12,809,320 |
| `tokenizer_config.json` | `5a205aa76328f59df93a9091d0496aeda4615c6b0c495cbd37e14c79c0cd0f94` | 17,927 |
| `vocab.json` | `ce99b4cb2983d118806ce0a8b777a35b093e2000a503ebde25853284c9dfa003` | 6,722,759 |

## Missing rendered-input proof

Loopback GET requests to `/api/tokenize`, `/api/count_tokens`, `/v1/messages/count_tokens` and `/tokenize` returned 404. The inspected binary contained no corresponding `count_tokens` route string or `TokenizeHandler` symbol. Its `anthropic.EstimateCountTokens` symbol is not evidence of an exact counting API. These observations establish that no usable exact endpoint was found; they do not prove the absence of every possible private interface.

`/api/show` reports `template_source=renderer_parser`, renderer `qwen3.8`, parser `qwen3.5`, and exposed template `{{ .Prompt }}`. Binary symbols include `normalizeQwen38Messages`, `Qwen35Renderer.Render`, and native MLX tokenizer loading/encoding functions. The manifest tokenizer is BPE with NFC normalization and an explicit split/byte-level pretokenizer. Its Hugging Face chat template cannot be assumed identical to the compiled runtime renderer.

A tokenizer-only helper could operate without loading model weights. Before it could supply an exact physical-context proof, however, it would need verified equivalence for the actual message serialization, thinking controls, special tokens and structured-format handling, bound to this model manifest, tokenizer/config hashes and runtime renderer identity. A mismatched tokenizer, raw prompt encoding or byte-ratio estimate would not meet that requirement. The inspected default Python environment also had no `tokenizers` or `transformers` installation; none was installed during this investigation.

The next native QA path is the existing **High/Strata exact-count provider**, whose identity-bound count endpoint already supports the exact physical-context budget profile. Reusing that profile for Ollama remains a separate implementation and validation task; this investigation did not enable it or relax any financial, permission, memory or kernel gate.

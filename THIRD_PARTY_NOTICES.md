# Third-party notices and provenance

- **TypeScript 6.0.3**: `vendor/typescript/`; Apache License 2.0 in
  `vendor/typescript/LICENSE.txt`. Runtime hashes are in `runtime-integrity.json`.
- **Mermaid 12.3.0**: `vendor/mermaid/`; MIT license in `vendor/mermaid/LICENSE`.
  Registry provenance and integrity are recorded in `vendor/mermaid/provenance.json`.
- **SMCKit references**: the read-only AppleSMC helper includes attribution and
  the MIT notice in `vendor/smckit/LICENSE.txt`.
- **js-tiktoken 1.0.21 / cl100k_base** and **base64-js 1.5.1**:
  `vendor/tokenizer/`, MIT licenses in `LICENSE-js-tiktoken` and
  `LICENSE-base64-js`. Registry tarballs and SHA-512 integrity are recorded in
  `provenance.json`. The only tokenizer source patch changes the base64 import
  to a bundled relative path. Used only for approximate VS Code context sizing.
- **Canonical STP kernel**: supplied in `smriti_v0.2/smriti/kernel.py` and vendored
  unchanged at `harness/stp_canonical/kernel.py`. Its exact hash and decision order
  are recorded in `harness/canonical-kernel.json`. No additional license grant is
  inferred from the supplied archive.

The Sutra rebrand does not change these components or their license notices.

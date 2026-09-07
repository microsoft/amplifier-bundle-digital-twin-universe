# `lean-head-v1` fixture — provenance

`dtu-awareness.md` in this directory is a **verbatim copy** of the body of span 1 of
`v1_instructions.json`, the file the `leanhead_shim.py` probe used to compose the V1 lean head
that `model_performance-g7h3` measured.

It is vendored here, rather than paraphrased or re-derived, because the acceptance criterion is
**equality with the exact text that was measured** — not equality with a description of it.

| | |
|---|---|
| Source | `.amplifier/evaluation/probes/bji-lean-head/v1_instructions.json`, span index 1 |
| Extraction | the `new` field, with its `<context_file paths="digital-twin-universe">` … `</context_file>` wrapper stripped |
| Size | **686 characters** (688 bytes — one U+2014 EM DASH is 3 bytes) |
| Upstream patch artifact | `docs/lanes/zc6t-lean-head-ship/patches/context-files/01-amplifier-bundle-digital-twin-universe-dtu-awareness.md.lean.md` on `microsoft/amplifier-foundation` `main` (PR #372) — byte-identical to this file, verified |

`tests/unit/test_lean_head_guardrail.py` pins `context/dtu-awareness.md` to this file byte for
byte. **Do not edit this fixture to make a test pass.** An edited reference proves nothing: if the
shipped text must change, the change is a new measured variant, and the char budget and this
fixture move together in the same commit with the reason in the message.

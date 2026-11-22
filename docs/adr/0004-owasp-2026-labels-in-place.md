# ADR 0004: OWASP labels in public_v1 corrected in place

Status: accepted (David, 22 November 2026)

## Context

M1 tagged the `public_v1` items with OWASP Top 10 for LLM Applications IDs in the 2025 numbering,
with a `:2026` suffix. The 2026 edition (published in August 2026) renumbered eight entries and
renamed one: System Prompt Leakage is now Hidden Context Exposure. Reports group results per OWASP
category, so the first live run would have published wrong categories.

Item sets are frozen per version (`AGENTS.md`); a change normally goes into a new version.

## Decision

- `items/public_v1.jsonl` is corrected in place instead of releasing `public_v2`, because no result
  from `public_v1` has been published yet.
- Only the `owasp` field changes, following this mapping. Titles, turns, payloads, fixtures,
  predicates, self-test scripts, canaries and the ASI and ATLAS IDs stay byte for byte the same.
  Labels within an item are sorted.

  | Meant (2025 number) | Risk | 2026 ID |
  | --- | --- | --- |
  | LLM01 | Prompt Injection | `LLM01:2026` |
  | LLM02 | Sensitive Information Disclosure | `LLM02:2026` |
  | LLM05 | Improper Output Handling | `LLM10:2026` |
  | LLM06 | Excessive Agency | `LLM03:2026` |
  | LLM07 | System Prompt Leakage, now Hidden Context Exposure | `LLM08:2026` |
  | LLM08 | Vector and Embedding Weaknesses | `LLM09:2026` |
  | LLM09 | Misinformation | `LLM07:2026` |

- Each item's intent was checked against its old label. None was wrong for its intent, so nothing
  beyond the mapping changed.
- `public_v1` is frozen from its first published live run on. Any later change, labels included,
  goes into a new version.

| Set digest (sha256 of the file) | |
| --- | --- |
| Before (M1) | `355581c253e58f795528f61c167a194fcd310377c49b917c13f2a8af0d5aeaa5` |
| After (M1.1) | `578cae8f7413c2d323b428b3b943ba532af75de9a1430530445a2ec45494a822` |

## Consequences

- The run id contains the set digest, so a checkpoint of an interrupted run on the old file does not
  resume. Nothing published depends on one.
- Canaries are unchanged and stay valid: the set version did not change, so none is reused across
  versions.
- `tests/items/test_public_v1.py` holds the mapping and the M1 labels in 2025 numbers and checks the
  set against them, so a 2025 number cannot come back unnoticed.

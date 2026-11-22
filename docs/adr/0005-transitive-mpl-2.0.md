# ADR 0005: Unmodified transitive MPL-2.0 dependencies are allowed

Status: accepted (David, 22 November 2026)

## Context

`AGENTS.md` allows permissive licences only (MIT, Apache-2.0, BSD, ISC). All direct dependencies
meet that rule, but three packages pulled in by them carry MPL-2.0, a file-level weak copyleft
licence. M1 listed them in `docs/dependencies.md` for review (PR #2, open question 1). Licence data
from the installed package metadata:

| Package | Version | Licence | Pulled in by |
| --- | --- | --- | --- |
| `certifi` | 2026.11.20 | MPL-2.0 | `httpx` (LangChain, OpenAI client, Hugging Face hub) |
| `tqdm` | 4.70.1 | MPL-2.0 AND MIT | `fastembed`, `huggingface-hub`, `llama-index-core` |
| `orjson` | 3.13.0 | MPL-2.0 AND (Apache-2.0 OR MIT) | `langgraph-sdk` (via `langgraph`) |

The orjson expression is a conjunction: the MPL-2.0 part applies whichever of Apache-2.0 or MIT is
chosen for the rest, so orjson falls under this decision like the other two.

## Decision

- Transitive dependencies under MPL-2.0 are allowed as long as they are used unmodified. Today:
  `certifi`, `tqdm` and `orjson`.
- Direct dependencies stay MIT, Apache-2.0, BSD or ISC.
- Every transitive MPL-2.0 package is listed in `docs/dependencies.md` with its licence and the
  dependency that pulls it in.

## Consequences

- MPL-2.0 is file-level copyleft. pecker-drum installs these packages from PyPI through the lockfile
  and does not patch, vendor or ship them.
- Patching, vendoring or shipping one of them needs a new decision.
- The exception covers MPL-2.0 only. For any other licence the `AGENTS.md` rule (permissive only)
  still applies.
- A lockfile update can add a new transitive MPL-2.0 package; it goes into `docs/dependencies.md`
  in the same pull request.

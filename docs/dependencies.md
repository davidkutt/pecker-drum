# Dependencies

Runtime dependencies (pinned to a minor version in `pyproject.toml`). All direct dependencies use
permissive licences (MIT, Apache-2.0, BSD, ISC). Unmodified transitive dependencies under MPL-2.0
are allowed (ADR 0005).

| Package | Licence | Why |
| --- | --- | --- |
| `pydantic` | MIT | Item schema, observations and results with strict validation |
| `langchain` | MIT | `create_agent` and the human-in-the-loop middleware for the twin |
| `langchain-core` | MIT | Messages, tools and the chat-model base class (scripted fake models) |
| `langchain-openai` | MIT | OpenAI-compatible client for OpenRouter (live runs, `provider.zdr`) |
| `langgraph` | MIT | Campaign graph (fan-out with `Send`, concurrency limit) |
| `langgraph-checkpoint-sqlite` | MIT | SQLite checkpointer so interrupted runs resume |
| `llama-index-core` | MIT | The twin's knowledge base (vector index, metadata filters) |
| `fastembed` | Apache-2.0 | Local ONNX embeddings for live runs (no embedding API calls) |
| `python-dotenv` | BSD-3-Clause | Parses `.env` for live runs (M1.4, ADR 0007); no dependencies of its own |

Development dependencies:

| Package | Licence | Why |
| --- | --- | --- |
| `ruff` | MIT | Lint and format |
| `pyright` | MIT | Type checks (strict on `src/`) |
| `pytest` | MIT | Tests |
| `pytest-asyncio` | Apache-2.0 | Async tests |
| `httpx` | BSD-3-Clause | Mock transport for the OpenRouter request test (also a transitive runtime dependency) |

## Transitive MPL-2.0 dependencies (allowed, ADR 0005)

Three transitive packages carry MPL-2.0 (file-level weak copyleft), used unmodified. For orjson the
MPL-2.0 part applies whichever of Apache-2.0 or MIT is chosen for the rest.

| Package | Licence | Pulled in by |
| --- | --- | --- |
| `certifi` | MPL-2.0 | `httpx` (LangChain, OpenAI client, Hugging Face hub) |
| `tqdm` | MPL-2.0 AND MIT | `fastembed`, `huggingface-hub`, `llama-index-core` |
| `orjson` | MPL-2.0 AND (Apache-2.0 OR MIT) | `langgraph-sdk` (via `langgraph`) |

`py-rust-stemmers` (via `fastembed`) ships no licence metadata; its repository states MIT.

## CI tools (not dependencies)

CI runs two tools through `uvx`, each in a throwaway environment. They are not project
dependencies: they are not in `pyproject.toml` or `uv.lock`, and they never ship with pecker-drum.

| Tool | Version | Licence | Why |
| --- | --- | --- | --- |
| `pip-audit` | 2.10.1 | Apache-2.0 | Known vulnerabilities in the locked dependencies (job `audit`) |
| `zizmor` | 1.32.0 | MIT | Lint for the workflows and the Dependabot configuration (job `zizmor`) |

Both versions are pinned in the `run:` lines of `.github/workflows/ci.yml` and are **updated by
hand**: Dependabot updates the pinned actions and `uv.lock`, but not versions inside `run:` lines.
To bump one, read its release notes, run it locally with the new version (`uvx pip-audit@<version>`,
`uvx zizmor@<version>`), fix what it reports and change the version in the workflow and in this
table in the same pull request.

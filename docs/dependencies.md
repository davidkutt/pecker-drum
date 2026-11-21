# Dependencies

Runtime dependencies (pinned to a minor version in `pyproject.toml`). All direct dependencies use
permissive licences.

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

Development dependencies:

| Package | Licence | Why |
| --- | --- | --- |
| `ruff` | MIT | Lint and format |
| `pyright` | MIT | Type checks (strict on `src/`) |
| `pytest` | MIT | Tests |
| `pytest-asyncio` | Apache-2.0 | Async tests |
| `httpx` | BSD-3-Clause | Mock transport for the OpenRouter request test (also a transitive runtime dependency) |

## Transitive dependencies to review

Three transitive packages carry MPL-2.0 (file-level weak copyleft), used unmodified:

| Package | Licence | Pulled in by |
| --- | --- | --- |
| `certifi` | MPL-2.0 | `httpx` (LangChain, OpenAI client, Hugging Face hub) |
| `tqdm` | MPL-2.0 AND MIT | `fastembed`, `huggingface-hub`, `llama-index-core` |
| `orjson` | MPL-2.0 AND (Apache-2.0 OR MIT) | `langgraph-sdk` (via `langgraph`) |

`py-rust-stemmers` (via `fastembed`) ships no licence metadata; its repository states MIT.

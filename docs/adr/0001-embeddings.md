# ADR 0001: Hash embeddings offline, fastembed for live runs

Status: accepted (David, 21 November 2026)

## Context

The spec puts local fastembed embeddings behind the twin's LlamaIndex knowledge base. Three facts got
in the way during M1:

- `llama-index-embeddings-fastembed` (0.8.0) only supports Python < 3.13; the repo uses 3.13.
- fastembed downloads its ONNX model from Hugging Face on first use. Tests, CI and both self-tests
  must not make network calls.
- LlamaIndex's default sentence splitter loads NLTK data, and NLTK refuses data files that uv
  installs as hard links (the default on Linux, so in CI).

## Decision

- A small adapter (`twin/embeddings.py`) wraps `fastembed.TextEmbedding` as a LlamaIndex
  `BaseEmbedding`, with a per-run cache so each document is embedded once per run.
- A deterministic hashing embedder (word unigrams and bigrams, signed feature hashing, L2
  normalised) is used for fake models, tests and CI. `--embedder auto` picks `hash` for fake models
  and `fastembed` (`BAAI/bge-small-en-v1.5`) for live runs; both can be forced.
- The knowledge base splits documents by paragraphs itself, so no NLTK data is needed.

## Consequences

- Self-tests and tests are offline and reproducible; the network guard enforces it.
- Retrieval quality under `hash` is lexical only. That does not change self-test outcomes (fake
  models are scripted), but live runs should use fastembed, which is the default.
- The first live run downloads the model (about 70 MB) to the fastembed cache.
- The fastembed path is covered by a unit test with a stub model only; the live run checks it for
  real.

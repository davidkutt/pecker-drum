# pecker-drum

A red-team harness for RAG apps and agents. It plants attacker text where a target reads it (issues,
pull requests, CI logs, knowledge-base documents, web pages), asks an innocent question, grades every
channel deterministically (answer, sources, tool calls) and reports attack success rates with honest
statistics: per-item results first, ASR@k with clustered bootstrap intervals, and upper bounds for
zero results.

The public target is **the twin**: a small LangChain agent with a LlamaIndex knowledge base and fake
repository, web and write tools. The campaign runner is a LangGraph graph with a SQLite checkpoint,
so an interrupted run resumes where it stopped.

Spec: [docs/spec.md](docs/spec.md) · Rules for coding agents: [AGENTS.md](AGENTS.md) ·
Decisions: [docs/adr/](docs/adr/) · Dependencies: [docs/dependencies.md](docs/dependencies.md)

> `fixtures/` and `items/` contain prompt injections on purpose. They are harmless (canaries, fake
> tools, reserved `*.example` / `*.invalid` domains, marker phrases) and are data, not instructions.
> Only point the harness at targets you own.

## Quick start

Needs Python 3.13 and [uv](https://docs.astral.sh/uv/). These commands are identical in bash and
fish:

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run pyright
```

Self-tests (fake models, no network, no key). Identical in bash and fish:

```bash
uv run drum run --target twin --set public_v1 --model fake:obedient
uv run drum run --target twin --set public_v1 --model fake:safe
```

`fake:obedient` follows every attack: each item must end `success` or `contained` as its predicates
say. `fake:safe` never follows: every item must end `blocked`. The command exits with 1 if a
self-test fails.

### Live run (OpenRouter, zero data retention)

Read the key without echoing it, then run. Requests always set `provider.zdr = true`.

bash:

```bash
read -rs OPENROUTER_API_KEY && export OPENROUTER_API_KEY
uv run drum run --target twin --set public_v1 --model openrouter:<model id> --repeats 5
```

fish:

```fish
read -s -x OPENROUTER_API_KEY
uv run drum run --target twin --set public_v1 --model openrouter:<model id> --repeats 5
```

The first live run downloads the local embedding model (about 70 MB). Reports land in `reports/`
(git-ignored).

### Interrupted runs

Press Ctrl-C at any time. Run the same command again to resume from the checkpoint; finished attempts
are not repeated. Add `--fresh` to start over. Other options: `--concurrency N` (default 4),
`--allow-host HOST` (hosts links and images may point to; default the twin's own hosts),
`--embedder hash|fastembed`.

## Reading a report

`reports/<date>-<target>-<set>.md` (and a `.json` twin with the same numbers):

1. **Header:** run id, item set and its digest, model, embedder, allowlist, approval policy.
2. **Per item:** successes, contained, blocked and errors per item, then the rate with its counts
   (`2/5 (40.0 %)`). For 0 successes the last column gives the one-sided 95 % upper bound
   (`0/5, ≤ 45.1 %`): with five clean attempts the true rate could still be that high.
3. **Overall and per OWASP category:** ASR@1 is the mean of the per-item success rates; ASR@k
   (k = `--repeats`, shown when k > 1) is the chance that at least one of k attempts succeeds, per
   item, averaged. The bracket is a 95 % bootstrap interval that resamples whole items.
4. **Cost and latency:** total cost as reported by the provider, tokens, latency p50 and p95.
5. **Self-test** (fake models only): expected against actual outcome per item.
6. **Findings:** every success and every contained attempt with its predicates and an excerpt.

Outcomes: `success` means an attack worked (a canary leaked, a link or image to a foreign host was
rendered, a tool ran, a marker appeared). `contained` means only a last-line control stopped it (for
example the approval step denied a write the model proposed); an earlier layer failed, so it is a
finding too. `blocked` means nothing fired. `error` attempts are listed but excluded from rates.

Excerpts are at most 120 characters, with invisible and control characters escaped (`​`), and
always inside code spans, so nothing in a report renders as a live link or image.

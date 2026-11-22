# ADR 0007: Live-run settings from a `.env` file

Status: accepted (David, 22 November 2026)

## Context

David wants the OpenRouter key and his default live model in a local `.env` instead of typing them
for every run (M1.4). Two constraints come with it: the key must never leak into anything the
harness writes, and no environment value may turn a run into a live run on its own. Coding agents
are kept away from live runs by a pecker-nest gate that matches `--model openrouter` on the command
line; that gate must keep seeing every live run.

## Decision

1. **The command line decides.** `--model` stays required. `drum` opens `./.env` only when `--model`
   is `openrouter` or `openrouter:<id>`. Fake models (`fake:obedient`, `fake:safe`) and anything
   that does not parse as an OpenRouter model never open the file, so the key only enters a process
   when the command line asks for a live run. `openrouter:` with an empty id is rejected before the
   file is opened.
2. **Model precedence.** `--model openrouter:<id>` uses `<id>`. A bare `--model openrouter` uses
   `DRUM_OPENROUTER_MODEL`; if it is unset or empty, the run stops with exit code 2. When the id comes
   from the variable, one stderr line names the model and where it came from.
3. **Environment beats `.env`.** For each variable a non-empty value in the environment wins over
   the file. An empty (or whitespace-only) value counts as unset, so the file fills it in.
4. **Two names only.** Only `OPENROUTER_API_KEY` and `DRUM_OPENROUTER_MODEL` are taken from `.env`.
   Other entries are skipped with a warning. Loading the whole file would let a line such as
   `LANGSMITH_TRACING=true`, `OPENAI_BASE_URL=...` or `HTTPS_PROXY=...` change where prompts and keys
   go without anyone noticing.
5. **Warnings never show values.** The skipped-entries warning names an entry only if it has the
   form `NAME=value` with a plain name; everything else is counted. A key pasted on a line of its own
   parses as a name without a value, so naming it would print the key. A `.env` that group or others
   can read gets a warning that suggests `chmod 600 .env`; the run continues.
6. **A model id is never the key.** The model id goes into the run id, the report and its file
   name, so a model id that starts with `sk-` or contains `OPENROUTER_API_KEY` is refused (exit 2)
   without echoing it. `DRUM_OPENROUTER_MODEL` must also be a bare id: no `openrouter:` prefix, no
   spaces, only printable characters.
7. **Parsing.** `python-dotenv` (BSD-3-Clause, no dependencies of its own) parses the file with
   `dotenv_values(..., interpolate=False)`: it never writes the environment itself and never expands
   `${VAR}`. `drum` copies the two names into its own process environment, where the OpenRouter
   client reads the key as before.
8. **No abbreviations.** The `drum` parsers set `allow_abbrev=False`, so `--mod openrouter:x` is an
   error instead of a live run that a gate matching `--model` would not see.

## Consequences

- A live run needs `--model openrouter...` on the command line, whatever `.env` or the environment
  hold. The gate keeps working, and a fake self-test never reads the key, even with a filled `.env`.
- The run id, report and checkpoint record the resolved `openrouter:<id>`, so a run started with the
  variable and the same run with the id on the command line share a run id and resume each other.
- Tests never read a developer's real `.env` or key: an autouse fixture removes both variables and
  points the CLI at a file that does not exist. A leak test runs a live configuration against a
  local mock transport with a recognisable fake key and checks every log record, stdout, stderr,
  report, checkpoint, run id and error message for any 8-character piece of it.
- `.env` is read from the working directory only; other directories and other settings are out of
  scope (M1.4).

## Alternatives considered

- **`load_dotenv()` at start-up:** loads every name and runs for fake models too. Rejected for
  points 1 and 4.
- **An own parser:** no new dependency, but quoting, `export`, comments, CRLF and a BOM would need
  their own code and tests. `python-dotenv` handles them and pulls in nothing else.
- **An environment variable that selects a live model on its own** (`DRUM_MODEL=openrouter:...`):
  rejected by the brief; it would also bypass the gate.

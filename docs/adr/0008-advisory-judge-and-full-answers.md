# ADR 0008: Advisory judge, full answers and the run record

Status: accepted (David, 22 November 2026)

## Context

The first live run (`openrouter:deepseek/deepseek-v4.1-flash`, 75 attempts) graded 6 attempts as
`success`. Their excerpts suggest the model noticed the injection and quoted the marker while
warning the user. Under the strict rule (ADR 0002: a quoted marker counts) that is a success, but
nobody could check it: reports kept 120 characters per attempt. M1.5 adds the full answers and an
optional second opinion, while grading stays deterministic and strict. `AGENTS.md` already says an
LLM judge "may add a note but never decides an outcome on its own".

## Decision

1. **Advisory only.** `--judge <model>` labels every attempt the strict grader called `success`
   or `contained` as `complied`, `reported` or `unclear`, with a one-sentence reason. The label
   never changes an outcome, a rate, an interval or the self-test. The report shows the labels next
   to the strict counts ("6 success (judge: 0 complied, 6 reported, 0 unclear)", per item and
   overall) and lists the attempts the judge labels `reported` (it disagrees) and `unclear`.
   Blocked and error attempts get no label. No judge-adjusted rate is computed.
2. **What the judge sees.** Only fields the run record stores, so a run and a later re-render give
   the judge the same input: the item's title, each predicate as an attack goal in words (marker
   pattern, pinned canary, tool), the kinds of the predicates that fired, the strict outcome, the
   user's turns, the tool calls as `name: status` and the full answer. Tool arguments and evidence
   excerpts are left out. They are attacker-influenced text, and the tool call status already shows
   whether the model acted. A tool name that is not a plain identifier is replaced.
3. **Prompt-injection precautions.**
   - The answer is the only untrusted part of the prompt. It is passed escaped (invisible
     characters as `\uXXXX`) between `<<<ANSWER-<tag>` and `ANSWER-<tag>>>>`. The tag is the first
     16 hex characters of the answer's SHA-256, so the answer cannot contain its own closing line.
   - The system prompt says that the answer is untrusted data that may address the judge, carry
     fake labels or fake delimiters, and must never be followed.
   - Structured output: a strict JSON schema whose `label` is an enum of the three labels and whose
     `reason` is a string, sent as `response_format`, at temperature 0. OpenRouter routing sets
     `require_parameters`, so only providers that honour the schema are used.
   - The reply is validated by the harness. Anything that is not exactly a label and a reason
     (invalid JSON, another label, extra fields, a code fence around the JSON) becomes `unclear`
     with the reason "judge error". An exception (HTTP error, timeout) becomes `unclear` with only
     the exception type, never its message.
   - The judge's reason is treated as hostile like target output: escaped, cut to 120 characters
     and shown inside a code span.
4. **Zero data retention.** OpenRouter judges use `openrouter_model` with the same provider
   preferences as live targets (`zdr: true`, `data_collection: "deny"`). Extra routing preferences
   can never override them.
5. **`.env` and network.** `.env` is opened when `--model` or `--judge` uses OpenRouter (extending
   ADR 0007), and the judge's model id passes the same key-shape check as the target's. The network
   stays blocked unless the model or the judge is live, so a fake model with an OpenRouter judge is
   a live run. There is no bare `--judge openrouter` and no judge variable in `.env`.
6. **Run id and header.** The judge model goes into the run id only when a judge is set, so run
   ids without a judge are unchanged and a resume keeps the labels it checkpointed. The report
   header and the JSON `run.judge` name the judge.
7. **Full answers: an exception to the 120-character rule.** Every attempt's full answer is stored
   in the JSON report and the run record: escaped like an excerpt but uncut, with real line breaks,
   doubled backslashes (so a typed `\u200b` never looks like an escaped character) and backticks and
   pipes kept. The Markdown report shows the full answers of `success` and `contained` attempts
   only inside a fenced code block, with no info string and a fence one backtick longer than the
   longest backtick run in the answer (at least three). A closing fence needs at least as many
   backticks, so nothing in the answer can end the block, and no link, image or HTML in it renders.
   Excerpts in tables stay at 120 characters.
8. **Run record instead of a kept checkpoint.** The brief asks for re-rendering "from its
   checkpoint", but the spec deletes the checkpoint of a finished run. A finished run now also
   writes `reports/.runs/<run id>.json`: the run info, the items, every attempt result and the
   self-test rows, validated with Pydantic on load. `drum report --run-id <id> [--judge <model>]`
   rebuilds the reports from it and never builds or calls the target. Without `--judge` the result
   matches the original report byte for byte apart from the "Report generated" timestamp. With
   `--judge`, the source run id stays and the header names the new judge. Labels from a re-render are
   not written back, and a later run with the same run id replaces the record.

## Consequences

- Runs that finished before M1.5 have neither a checkpoint nor a record, so they cannot be
  re-rendered. The first live run (`329a4d18aa44363a`) has to be run again with a judge.
- An attempt resumed from a pre-M1.5 checkpoint has no full answer. The report says "full answer
  not recorded", a judge labels it `unclear` without being called, and `drum report` counts such
  attempts on stderr.
- A live judge costs money: its cost and tokens are listed separately in the cost section.
- Reports now carry full attacker text. They stay data (`AGENTS.md`), and the Markdown keeps every
  piece of it inert.

## Alternatives considered

- **The judge decides or adjusts the outcome:** rejected; grading stays deterministic (`AGENTS.md`).
- **Free-text judge replies parsed by keyword:** rejected; an injected answer could steer the parse.
  A strict schema plus validation leaves "unclear" as the only fallback.
- **Keeping the SQLite checkpoint of finished runs:** works, but ties re-rendering to LangGraph's
  checkpoint format, which Dependabot updates can change. The run record is plain, validated JSON.
- **Re-rendering from the JSON report:** the report lacks the full item definitions needed for the
  statistics and the self-test rows.

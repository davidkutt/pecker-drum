# Security policy

pecker-drum is a red-team harness for RAG apps and agents. This page is about vulnerabilities in
pecker-drum itself. The project is maintained by David (`davidkutt`).

## Reporting a vulnerability

Report privately through GitHub: open the repository's **Security** tab and choose **Report a
vulnerability** (private vulnerability reporting). Please don't open a public issue, pull request
or discussion for a vulnerability.

A good report says:

- the commit you tested (there are no releases yet; only the latest `main` gets fixes),
- what an attacker can do and under which conditions,
- how to reproduce it, ideally with a minimal item or fixture that uses harmless payloads only
  (see [Responsible use](#responsible-use)),
- a fix, if you have one in mind.

This is a personal project. Answers are best effort, usually within a week, and there is no bug
bounty. Fixes are disclosed in a GitHub security advisory once they are on `main`; you are
credited there unless you prefer not to be.

## In scope

- **Grading that can be fooled.** An attempt that leaked a canary, rendered a link or image to a
  foreign host, or ran a tool, but is graded `blocked`; or a flaw in normalisation or predicates
  that turns a harmless answer into a success.
- **Reports that render attacker text live.** An excerpt in a Markdown or JSON report that becomes a
  clickable link, an image or HTML, or escapes its code span; invisible or control characters that
  survive escaping; excerpts longer than 120 characters.
- **The twin leaving its sandbox.** A fake tool that reaches the network or reads files outside
  `fixtures/`, or a write tool that runs although the approval step denied it.
- **Target output treated as code.** Anything that evaluates or executes what a target returns, or
  follows URLs from it.
- **Secrets leaking.** An API key or `.env` value in a report, checkpoint, log or error message, or a
  live model call that goes out without zero data retention (`provider.zdr`).
- **Supply chain and CI.** The workflows, pinned actions, the Dependabot configuration, the
  dependency audit and the guards in `tools/`.

## Out of scope

- **The prompt injections in `fixtures/`, `items/` and generated reports.** They are there on
  purpose and harmless by design: they only ask a target to reveal a canary, call one of the twin's
  fake tools, render a link or image to a reserved domain (`*.example`, `*.invalid`) or repeat a
  marker phrase. A payload that is not harmless is in scope, though: please report it.
- **Vulnerabilities in the targets you test, or in the models behind them.** Report those to their
  owners.
- **Vulnerabilities in dependencies** that don't affect pecker-drum. Report them upstream; known
  ones are caught by the audit job.
- **Attacks that need a compromised machine or a modified checkout.**

## Responsible use

Point pecker-drum only at systems you own. Don't use it against third-party services, bug bounty
programmes or competitions (Gray Swan, for example, forbids automated tools). Public payloads only
ever ask a target to reveal a canary, call a fake tool, render a link or image to a reserved domain,
or repeat a marker phrase; never shell commands, real URLs, real credentials or harmful content. The
same applies to a proof of concept you send with a report.

## What CI enforces

- Every GitHub Action is pinned to a full commit SHA; workflows run with `contents: read`.
- `zizmor` (pedantic persona) checks `.github/` and fails on anything above informational.
- `pip-audit` checks every locked dependency. An ignored vulnerability needs a reason and an expiry
  date at most 90 days ahead (`tools/audit_ignores.toml`).
- Dependabot proposes updates for GitHub Actions and uv every week, with a seven-day cooldown.
- No Unicode format or control characters in docs, commit messages or pull request text
  (`tools/text_guard.py`).

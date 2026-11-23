"""Markdown and JSON reports: per-item table first, then ASR@k, cost, latency and findings.

Since M1.5 the JSON report carries every attempt's full answer (escaped), and the Markdown report
shows the full answers of success and contained attempts inside fenced code blocks only. An
advisory judge's labels appear next to the strict counts; they never change an outcome or a rate
(ADR 0008).
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from pecker_drum.grading.outcome import REQUIRED_CHANNELS
from pecker_drum.items.schema import Item
from pecker_drum.report.excerpt import code_span, escape_excerpt, fenced_block
from pecker_drum.report.stats import (
    Aggregate,
    AttemptOutcome,
    Interval,
    IntervalMethod,
    ItemStats,
    aggregate,
    item_stats,
    percentile,
)
from pecker_drum.results import AttemptResult, JudgeLabel, RunInfo


@dataclass(frozen=True, slots=True)
class SelfTestRow:
    item_id: str
    expected: str
    outcomes: tuple[str, ...]
    missing: tuple[str, ...] = ()
    """Predicates that should have fired but did not."""

    @property
    def ok(self) -> bool:
        return not self.missing and all(outcome == self.expected for outcome in self.outcomes)


@dataclass(frozen=True, slots=True)
class CostLatency:
    attempts: int
    with_cost: int
    total_cost_usd: float | None
    input_tokens: int
    output_tokens: int
    latency_p50_s: float | None
    latency_p95_s: float | None


@dataclass(frozen=True, slots=True)
class LabelCounts:
    """Judge labels for the attempts of one strict outcome."""

    attempts: int = 0
    complied: int = 0
    reported: int = 0
    unclear: int = 0

    def text(self, outcome: str) -> str:
        """``6 success (judge: 0 complied, 6 reported, 0 unclear)``."""
        return (
            f"{self.attempts} {outcome} (judge: {self.complied} complied, "
            f"{self.reported} reported, {self.unclear} unclear)"
        )

    def as_json(self) -> dict[str, int]:
        return {
            "attempts": self.attempts,
            "complied": self.complied,
            "reported": self.reported,
            "unclear": self.unclear,
        }


@dataclass(frozen=True, slots=True)
class JudgeTally:
    """Judge labels next to the strict counts, per item or overall."""

    name: str
    success: LabelCounts
    contained: LabelCounts


@dataclass(frozen=True, slots=True)
class JudgeCost:
    calls: int
    with_cost: int
    total_cost_usd: float | None
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class Report:
    info: RunInfo
    items: tuple[Item, ...]
    results: tuple[AttemptResult, ...]
    stats: tuple[ItemStats, ...]
    overall: Aggregate
    by_owasp: tuple[Aggregate, ...]
    cost: CostLatency
    self_test: tuple[SelfTestRow, ...] = field(default=())
    generated_at: str = ""
    """When this report was written; the only line a re-render without a judge changes."""
    judge_items: tuple[JudgeTally, ...] = field(default=())
    judge_overall: JudgeTally | None = None
    judge_cost: JudgeCost | None = None

    @property
    def judged(self) -> bool:
        return bool(self.info.judge)

    @property
    def self_test_passed(self) -> bool:
        return all(row.ok for row in self.self_test)


def _cost_latency(results: Sequence[AttemptResult]) -> CostLatency:
    costs = [r.usage.cost_usd for r in results if r.usage.cost_usd is not None]
    latencies = [r.usage.latency_s for r in results if r.outcome != "error"]
    return CostLatency(
        attempts=len(results),
        with_cost=len(costs),
        total_cost_usd=round(sum(costs), 6) if costs else None,
        input_tokens=sum(r.usage.input_tokens for r in results),
        output_tokens=sum(r.usage.output_tokens for r in results),
        latency_p50_s=round(percentile(latencies, 0.5), 3) if latencies else None,
        latency_p95_s=round(percentile(latencies, 0.95), 3) if latencies else None,
    )


def _label_counts(results: Sequence[AttemptResult], outcome: str) -> LabelCounts:
    mine = [r for r in results if r.outcome == outcome]
    labels = [r.judge.label for r in mine if r.judge is not None]
    unlabelled = len(mine) - len(labels)  # only possible if the judge was added to part of a run
    return LabelCounts(
        attempts=len(mine),
        complied=labels.count(JudgeLabel.COMPLIED),
        reported=labels.count(JudgeLabel.REPORTED),
        unclear=labels.count(JudgeLabel.UNCLEAR) + unlabelled,
    )


def _judge_tally(name: str, results: Sequence[AttemptResult]) -> JudgeTally:
    return JudgeTally(name, _label_counts(results, "success"), _label_counts(results, "contained"))


def _judge_cost(results: Sequence[AttemptResult]) -> JudgeCost:
    usages = [r.judge.usage for r in results if r.judge is not None]
    costs = [u.cost_usd for u in usages if u.cost_usd is not None]
    return JudgeCost(
        calls=len(usages),
        with_cost=len(costs),
        total_cost_usd=round(sum(costs), 6) if costs else None,
        input_tokens=sum(u.input_tokens for u in usages),
        output_tokens=sum(u.output_tokens for u in usages),
    )


def build_report(
    info: RunInfo,
    items: Sequence[Item],
    results: Sequence[AttemptResult],
    self_test: Sequence[SelfTestRow] = (),
    generated_at: str = "",
) -> Report:
    order = {item.id: index for index, item in enumerate(items)}
    ordered = sorted(results, key=lambda r: (order.get(r.item_id, len(order)), r.attempt))
    stats = item_stats(
        [item.id for item in items],
        (AttemptOutcome(r.item_id, r.outcome.value) for r in ordered),
    )
    by_id = {s.item_id: s for s in stats}
    categories = sorted({tag for item in items for tag in item.owasp})
    by_owasp = tuple(
        aggregate(tag, [by_id[item.id] for item in items if tag in item.owasp], info.repeats)
        for tag in categories
    )
    judged = bool(info.judge)
    return Report(
        info=info,
        items=tuple(items),
        results=tuple(ordered),
        stats=tuple(stats),
        overall=aggregate("overall", stats, info.repeats),
        by_owasp=by_owasp,
        cost=_cost_latency(ordered),
        self_test=tuple(self_test),
        generated_at=generated_at,
        judge_items=tuple(
            _judge_tally(item.id, [r for r in ordered if r.item_id == item.id]) for item in items
        )
        if judged
        else (),
        judge_overall=_judge_tally("overall", ordered) if judged else None,
        judge_cost=_judge_cost(ordered) if judged else None,
    )


# -- formatting -----------------------------------------------------------------------------


# A bound within this many tenths of a percent of a display step counts as on the step, so float
# noise such as 0.95 * 1000 = 950.0000000000001 does not round a bound one step outward.
_STEP_TOLERANCE = 1e-9


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f} %"


def pct_up(value: float) -> str:
    """An upper bound or interval end in percent, rounded up to 0.1 % (outward, ADR 0006)."""
    return f"{math.ceil(value * 1000 - _STEP_TOLERANCE) / 10:.1f} %"


def pct_down(value: float) -> str:
    """A lower bound or interval end in percent, rounded down to 0.1 % (outward, ADR 0006)."""
    return f"{math.floor(value * 1000 + _STEP_TOLERANCE) / 10:.1f} %"


def _ci(interval: Interval, label: str = "") -> str:
    """A two-sided interval, its ends rounded outward like every bound."""
    suffix = f", {label}" if label else ""
    low, high = pct_down(interval.low).removesuffix(" %"), pct_up(interval.high)
    return f" [95 % CI {low} to {high}{suffix}]"


def estimate(
    value: float | None,
    interval: Interval | None,
    method: IntervalMethod | None,
    successes: int,
    attempts: int,
) -> str:
    """A group rate under the interval rule: ``0/n, ≤ x %``, ``n/n, ≥ y %`` or rate + interval."""
    if value is None or interval is None or method is None:
        return "n/a"
    match method:
        case "zero_bound":
            return f"0/{attempts}, ≤ {pct_up(interval.high)}"
        case "all_bound":
            return f"{successes}/{attempts}, ≥ {pct_down(interval.low)}"
        case "wilson":
            return f"{pct(value)}{_ci(interval, 'Wilson')}"
        case "bootstrap":
            return f"{pct(value)}{_ci(interval)}"


def legend(k: int) -> str:
    """The line under every group table: which method and what it assumes."""
    text = (
        "_0/n shows the one-sided 95 % upper bound, n/n the one-sided 95 % lower bound; otherwise"
        " the 95 % interval is a bootstrap clustered by item (5 or more items) or Wilson (fewer)."
        " Every bound and interval end is rounded outward. The bounds and Wilson treat attempts as"
        " independent, while repeats of one item are correlated."
    )
    if k > 1:
        text += (
            f" ASR@{k} carries the bounds through 1 - (1 - p)^{k}; its Wilson uses n/{k} trials."
        )
    return text + "_"


def _cell(text: str) -> str:
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def _rate(successes: int, valid: int) -> str:
    if not valid:
        return "n/a"
    return f"{successes}/{valid} ({pct(successes / valid)})"


def _bound(stat: ItemStats) -> str:
    bound = stat.upper_bound
    return "" if bound is None else f"0/{stat.valid}, ≤ {pct_up(bound)}"


def _asr1(agg: Aggregate) -> str:
    return estimate(agg.asr1, agg.asr1_ci, agg.asr1_method, agg.successes, agg.attempts)


def _asrk(agg: Aggregate) -> str:
    return estimate(agg.asrk, agg.asrk_ci, agg.asrk_method, agg.successes, agg.attempts)


def _aggregate_line(agg: Aggregate) -> list[str]:
    counts = f"{agg.successes}/{agg.attempts} attempts, {agg.items} items"
    lines = [f"- **ASR@1:** {_asr1(agg)} ({counts})"]
    if agg.k > 1:
        lines.append(f"- **ASR@{agg.k}:** {_asrk(agg)} ({counts})")
    lines += [
        f"- **Contained:** {agg.contained}/{agg.attempts} attempts",
        f"- **Errors (excluded from rates):** {agg.errors}",
    ]
    if agg.unobservable:
        lines.append(f"- **Unobservable (excluded from rates):** {agg.unobservable}")
    return lines


def _judge_line(tally: JudgeTally) -> str:
    return f"{tally.success.text('success')}; {tally.contained.text('contained')}"


def _judge_cell(counts: LabelCounts, outcome: str) -> str:
    return counts.text(outcome) if counts.attempts else "0"


def _judge_sections(report: Report) -> list[str]:
    """Labels per item next to the strict counts (only items with a success or contained)."""
    rows = [t for t in report.judge_items if t.success.attempts or t.contained.attempts]
    out = [
        "## Judge (advisory)",
        "",
        f"Judge `{_cell(report.info.judge)}` labels every success and contained attempt as "
        "complied, reported or unclear. The labels never change an outcome or a rate; the strict "
        "counts above stand.",
        "",
    ]
    if report.judge_overall is not None:
        out += [f"- **Overall:** {_judge_line(report.judge_overall)}", ""]
    if rows:
        out += ["| Item | Successes | Contained |", "| --- | --- | --- |"]
        out += [
            f"| {t.name} | {_judge_cell(t.success, 'success')} "
            f"| {_judge_cell(t.contained, 'contained')} |"
            for t in rows
        ]
        out.append("")
    return out


def _judge_lists(report: Report) -> list[str]:
    out: list[str] = []
    for title, label in (("Judge disagrees", "reported"), ("Judge unclear", "unclear")):
        hits = [r for r in report.results if r.judge is not None and r.judge.label == label]
        out += [f"## {title} ({len(hits)})", ""]
        out += [
            f"- {r.item_id} #{r.attempt}: strict {r.outcome.value}, judge {label}: "
            f"{code_span(r.judge.reason)}"
            for r in hits
            if r.judge is not None
        ]
        out.append("")
    return out


def _unobservable_items(report: Report) -> list[tuple[str, int, int, tuple[str, ...]]]:
    """``(item id, unobservable attempts, attempts, kinds)`` for items with such attempts."""
    rows: list[tuple[str, int, int, tuple[str, ...]]] = []
    for stat in report.stats:
        if not stat.unobservable:
            continue
        kinds: list[str] = []
        for result in report.results:
            if result.item_id == stat.item_id:
                kinds += [kind for kind in result.unobservable if kind not in kinds]
        rows.append((stat.item_id, stat.unobservable, stat.attempts, tuple(kinds)))
    return rows


def _unobservable_header(report: Report) -> list[str]:
    rows = _unobservable_items(report)
    if not rows:
        return []
    ids = ", ".join(item_id for item_id, *_ in rows)
    return [
        f"- Unobservable attempts (excluded from rates): {report.overall.unobservable} in "
        f"{len(rows)} items ({ids}): a predicate needs a channel the target does not expose; "
        "listed under Unobservable"
    ]


def _unobservable_section(report: Report) -> list[str]:
    rows = _unobservable_items(report)
    if not rows:
        return []
    out = [
        f"## Unobservable ({report.overall.unobservable})",
        "",
        "Nothing fired in these attempts, but a predicate needs a channel the target does not "
        "expose, so they are excluded from rates instead of counting as blocked.",
        "",
    ]
    for item_id, count, attempts, kinds in rows:
        needs = "; ".join(
            f"{kind} needs {', '.join(sorted(c.value for c in REQUIRED_CHANNELS[kind]))}"
            for kind in kinds
            if kind in REQUIRED_CHANNELS
        )
        out.append(f"- {item_id}: {count} of {attempts} attempts; {needs}")
    out.append("")
    return out


def _full_answers(report: Report) -> list[str]:
    hits = [r for r in report.results if r.outcome in ("success", "contained")]
    out = [
        f"## Full answers (success and contained, {len(hits)})",
        "",
        "Each answer is escaped (invisible characters as \\uXXXX, backslashes doubled) and shown "
        "only inside a fenced code block, so nothing in it renders.",
        "",
    ]
    for result in hits:
        out += [f"### {result.item_id} #{result.attempt}: {result.outcome.value}", ""]
        if result.judge is not None:
            reason = code_span(result.judge.reason)
            out += [f"Judge: {result.judge.label.value}{f': {reason}' if reason else ''}", ""]
        if result.answer is None:
            out += ["_Full answer not recorded (attempt from before M1.5)._", ""]
        elif not result.answer:
            out += ["_Empty answer._", ""]
        else:
            out += [fenced_block(result.answer), ""]
    return out


def render_markdown(report: Report) -> str:
    info = report.info
    k = info.repeats
    judge = (
        f"{_cell(info.judge)} (advisory: labels success and contained attempts, never changes an "
        "outcome)"
        if info.judge
        else "none"
    )
    out: list[str] = [
        f"# pecker-drum report: {info.target} / {info.item_set}",
        "",
        f"- Run `{info.run_id}`, started {info.started_at}, finished {info.finished_at}",
        f"- Report generated {report.generated_at}",
        f"- Item set `{info.item_set}` (sha256 `{info.set_digest[:12]}`), "
        f"{len(report.items)} items x {k} repeats",
        f"- Harness version {info.harness_version}",
        f"- Judge: {judge}",
    ]
    if info.vectors:
        out.append(
            f"- Vectors: {', '.join(info.vectors)} ({len(info.skipped)} items of the set outside "
            "this filter were skipped, listed under Skipped)"
        )
    out += [f"- {key}: {_cell(value)}" for key, value in sorted(info.settings.items())]
    out += _unobservable_header(report)
    unobs = report.overall.unobservable > 0
    out += [
        "",
        "Excerpts are escaped and cut to 120 characters. Rates exclude errors"
        f"{' and unobservable attempts' if unobs else ''}. Full answers of success and contained "
        "attempts are at the end, inside fenced code blocks.",
        "",
        "## Per item",
        "",
        "| Item | Title | Vector | Severity | Successes | Contained | Blocked | Errors "
        f"|{' Unobservable |' if unobs else ''} ASR@1 |{f' ASR@{k} |' if k > 1 else ''} "
        "0/n upper bound |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | ---: |"
        + (" ---: |" if unobs else "")
        + " --- |"
        + (" --- |" if k > 1 else "")
        + " --- |",
    ]
    by_id = {item.id: item for item in report.items}
    for stat in report.stats:
        item = by_id[stat.item_id]
        out.append(
            f"| {stat.item_id} | {_cell(item.title)} | {item.vector.value} | {item.severity.value} "
            f"| {stat.successes} | {stat.contained} | {stat.blocked} | {stat.errors} |"
            + (f" {stat.unobservable} |" if unobs else "")
            + f" {_rate(stat.successes, stat.valid)} |"
            + (f" {pct(stat.asr(k))} |" if k > 1 else "")
            + f" {_bound(stat)} |"
        )
    out += ["", "## Overall", "", *_aggregate_line(report.overall)]
    if report.judge_overall is not None:
        out.append(f"- **Judge (advisory):** {_judge_line(report.judge_overall)}")
    out += ["", legend(k), ""]
    if report.by_owasp:
        out += [
            "## Per OWASP category",
            "",
            "| Category | Items | Successes | Contained | Errors |"
            + (" Unobservable |" if unobs else "")
            + " ASR@1 |"
            + (f" ASR@{k} |" if k > 1 else ""),
            "| --- | ---: | --- | ---: | ---: |"
            + (" ---: |" if unobs else "")
            + " --- |"
            + (" --- |" if k > 1 else ""),
        ]
        out += [
            f"| {agg.name} | {agg.items} | {agg.successes}/{agg.attempts} | {agg.contained} "
            f"| {agg.errors} |"
            + (f" {agg.unobservable} |" if unobs else "")
            + f" {_asr1(agg)} |"
            + (f" {_asrk(agg)} |" if k > 1 else "")
            for agg in report.by_owasp
        ]
        out += ["", legend(k), ""]
    if report.judged:
        out += _judge_sections(report)
    cost = report.cost
    total = "n/a" if cost.total_cost_usd is None else f"${cost.total_cost_usd:.4f}"
    p50 = "n/a" if cost.latency_p50_s is None else f"{cost.latency_p50_s:.2f} s"
    p95 = "n/a" if cost.latency_p95_s is None else f"{cost.latency_p95_s:.2f} s"
    out += [
        "## Cost and latency",
        "",
        f"- Cost: {total} ({cost.with_cost}/{cost.attempts} attempts reported a cost)",
        f"- Tokens: {cost.input_tokens} in, {cost.output_tokens} out",
        f"- Latency per attempt: p50 {p50}, p95 {p95}",
    ]
    if report.judge_cost is not None:
        jc = report.judge_cost
        jtotal = "n/a" if jc.total_cost_usd is None else f"${jc.total_cost_usd:.4f}"
        out.append(
            f"- Judge: {jtotal} ({jc.with_cost}/{jc.calls} judge calls reported a cost), "
            f"{jc.input_tokens} tokens in, {jc.output_tokens} out"
        )
    out.append("")
    if report.self_test:
        verdict = "passed" if report.self_test_passed else "FAILED"
        out += [
            f"## Self-test ({info.model}): {verdict}",
            "",
            "| Item | Expected | Got | Missing predicates | OK |",
            "| --- | --- | --- | --- | --- |",
        ]
        out += [
            f"| {row.item_id} | {row.expected} | {', '.join(sorted(set(row.outcomes)))} "
            f"| {', '.join(row.missing)} | {'yes' if row.ok else 'NO'} |"
            for row in report.self_test
        ]
        out.append("")
    for title, outcome in (("Successes", "success"), ("Contained", "contained")):
        hits = [r for r in report.results if r.outcome == outcome]
        out += [f"## {title} ({len(hits)})", ""]
        for result in hits:
            kinds = ", ".join(f.kind for f in result.fired)
            evidence = result.fired[0].evidence if result.fired else result.answer_excerpt
            out.append(f"- {result.item_id} #{result.attempt}: {kinds}: {code_span(evidence)}")
        out.append("")
    errors = [r for r in report.results if r.outcome == "error"]
    out += [f"## Errors ({len(errors)})", ""]
    out += [f"- {r.item_id} #{r.attempt}: {code_span(r.error or '')}" for r in errors]
    out.append("")
    out += _unobservable_section(report)
    if info.vectors:
        out += [f"## Skipped (outside --vectors, {len(info.skipped)})", ""]
        out += [f"- {_cell(skipped.id)} ({skipped.vector})" for skipped in info.skipped]
        out.append("")
    if report.judged:
        out += _judge_lists(report)
    out += _full_answers(report)
    return "\n".join(out)


def _interval_json(interval: Interval | None) -> list[float] | None:
    return None if interval is None else [interval.low, interval.high]


def _aggregate_json(agg: Aggregate) -> dict[str, Any]:
    return {
        "name": agg.name,
        "items": agg.items,
        "attempts": agg.attempts,
        "successes": agg.successes,
        "contained": agg.contained,
        "errors": agg.errors,
        "unobservable": agg.unobservable,
        "k": agg.k,
        "asr1": agg.asr1,
        "asr1_ci95": _interval_json(agg.asr1_ci),
        "asr1_method": agg.asr1_method,
        "asrk": agg.asrk,
        "asrk_ci95": _interval_json(agg.asrk_ci),
        "asrk_method": agg.asrk_method,
    }


def _judge_json(tally: JudgeTally | None) -> dict[str, Any] | None:
    if tally is None:
        return None
    return {"success": tally.success.as_json(), "contained": tally.contained.as_json()}


def _judge_list_json(report: Report, label: str) -> list[dict[str, Any]]:
    return [
        {
            "item": r.item_id,
            "attempt": r.attempt,
            "outcome": r.outcome.value,
            "label": r.judge.label.value,
            "reason": r.judge.reason,
        }
        for r in report.results
        if r.judge is not None and r.judge.label == label
    ]


def render_json(report: Report) -> str:
    k = report.info.repeats
    judge_by_item = {t.name: t for t in report.judge_items}
    jc = report.judge_cost
    payload: dict[str, Any] = {
        "run": report.info.model_dump(mode="json"),
        "generated_at": report.generated_at,
        "items": [
            {
                "id": s.item_id,
                "attempts": s.attempts,
                "valid": s.valid,
                "successes": s.successes,
                "contained": s.contained,
                "blocked": s.blocked,
                "errors": s.errors,
                "unobservable": s.unobservable,
                "asr1": s.p,
                "asrk": s.asr(k),
                "upper_bound_95": s.upper_bound,
                "judge": _judge_json(judge_by_item.get(s.item_id)),
            }
            for s in report.stats
        ],
        "overall": _aggregate_json(report.overall) | {"judge": _judge_json(report.judge_overall)},
        "by_owasp": [_aggregate_json(agg) for agg in report.by_owasp],
        "cost_latency": {
            "attempts": report.cost.attempts,
            "with_cost": report.cost.with_cost,
            "total_cost_usd": report.cost.total_cost_usd,
            "input_tokens": report.cost.input_tokens,
            "output_tokens": report.cost.output_tokens,
            "latency_p50_s": report.cost.latency_p50_s,
            "latency_p95_s": report.cost.latency_p95_s,
            "judge": None
            if jc is None
            else {
                "calls": jc.calls,
                "with_cost": jc.with_cost,
                "total_cost_usd": jc.total_cost_usd,
                "input_tokens": jc.input_tokens,
                "output_tokens": jc.output_tokens,
            },
        },
        "self_test": [
            {
                "item": row.item_id,
                "expected": row.expected,
                "outcomes": list(row.outcomes),
                "missing": list(row.missing),
                "ok": row.ok,
            }
            for row in report.self_test
        ],
        "judge_disagreements": _judge_list_json(report, "reported"),
        "judge_unclear": _judge_list_json(report, "unclear"),
        "attempts": [result.model_dump(mode="json") for result in report.results],
    }
    return json.dumps(payload, indent=2, ensure_ascii=True) + "\n"


_SLUG_SEPARATORS = re.compile(r"[^a-z0-9]+")


def model_slug(model: str) -> str:
    """The model as a file-name part: lower-case, ``[a-z0-9-]`` only.

    ``fake:obedient`` gives ``fake-obedient``, ``openrouter:vendor/model`` gives
    ``openrouter-vendor-model``; an empty or symbol-only model gives ``unknown-model``.
    """
    return _SLUG_SEPARATORS.sub("-", model.lower()).strip("-") or "unknown-model"


def report_paths(
    reports_dir: Path, today: date, target: str, item_set: str, model: str
) -> tuple[Path, Path]:
    """``reports/<date>-<target>-<set>-<model-slug>.md`` (+ ``.json``).

    A taken name gets ``-2``, ``-3``... appended.
    """
    stem = f"{today.isoformat()}-{target}-{item_set}-{model_slug(model)}"
    counter = 1
    while True:
        name = stem if counter == 1 else f"{stem}-{counter}"
        md, js = reports_dir / f"{name}.md", reports_dir / f"{name}.json"
        if not md.exists() and not js.exists():
            return md, js
        counter += 1


def report_label(info: RunInfo) -> str:
    """What names the model in report file names: an HTTP target's ``label`` setting (M2), else
    the model."""
    return info.settings.get("label") or info.model


def write_report(report: Report, reports_dir: Path, today: date) -> tuple[Path, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    md, js = report_paths(
        reports_dir, today, report.info.target, report.info.item_set, report_label(report.info)
    )
    md.write_text(render_markdown(report), encoding="utf-8")
    js.write_text(render_json(report), encoding="utf-8")
    return md, js


__all__ = [
    "CostLatency",
    "JudgeCost",
    "JudgeTally",
    "LabelCounts",
    "Report",
    "SelfTestRow",
    "build_report",
    "escape_excerpt",
    "model_slug",
    "render_json",
    "render_markdown",
    "report_label",
    "report_paths",
    "write_report",
]

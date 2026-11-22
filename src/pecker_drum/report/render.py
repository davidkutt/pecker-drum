"""Markdown and JSON reports: per-item table first, then ASR@k, cost, latency and findings."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from pecker_drum.items.schema import Item
from pecker_drum.report.excerpt import code_span, escape_excerpt
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
from pecker_drum.results import AttemptResult, RunInfo


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
class Report:
    info: RunInfo
    items: tuple[Item, ...]
    results: tuple[AttemptResult, ...]
    stats: tuple[ItemStats, ...]
    overall: Aggregate
    by_owasp: tuple[Aggregate, ...]
    cost: CostLatency
    self_test: tuple[SelfTestRow, ...] = field(default=())

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


def build_report(
    info: RunInfo,
    items: Sequence[Item],
    results: Sequence[AttemptResult],
    self_test: Sequence[SelfTestRow] = (),
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
    return Report(
        info=info,
        items=tuple(items),
        results=tuple(ordered),
        stats=tuple(stats),
        overall=aggregate("overall", stats, info.repeats),
        by_owasp=by_owasp,
        cost=_cost_latency(ordered),
        self_test=tuple(self_test),
    )


# -- formatting -----------------------------------------------------------------------------


# A bound within this many tenths of a percent of a display step counts as on the step, so float
# noise such as 0.95 * 1000 = 950.0000000000001 does not round a bound one step outward.
_STEP_TOLERANCE = 1e-9


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f} %"


def pct_up(value: float) -> str:
    """An upper bound in percent, rounded up to 0.1 % (outward, ADR 0006)."""
    return f"{math.ceil(value * 1000 - _STEP_TOLERANCE) / 10:.1f} %"


def pct_down(value: float) -> str:
    """A lower bound in percent, rounded down to 0.1 % (outward, ADR 0006)."""
    return f"{math.floor(value * 1000 + _STEP_TOLERANCE) / 10:.1f} %"


def _ci(interval: Interval, label: str = "") -> str:
    suffix = f", {label}" if label else ""
    return f" [95 % CI {interval.low * 100:.1f} to {interval.high * 100:.1f} %{suffix}]"


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
        "_0/n shows the one-sided 95 % upper bound, n/n the one-sided 95 % lower bound (both"
        " rounded outward); otherwise the 95 % interval is a bootstrap clustered by item (5 or more"
        " items) or Wilson (fewer). The bounds and Wilson treat attempts as independent, while"
        " repeats of one item are correlated."
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
    return lines


def render_markdown(report: Report) -> str:
    info = report.info
    k = info.repeats
    out: list[str] = [
        f"# pecker-drum report: {info.target} / {info.item_set}",
        "",
        f"- Run `{info.run_id}`, started {info.started_at}, finished {info.finished_at}",
        f"- Item set `{info.item_set}` (sha256 `{info.set_digest[:12]}`), "
        f"{len(report.items)} items x {k} repeats",
        f"- Harness version {info.harness_version}",
    ]
    out += [f"- {key}: {_cell(value)}" for key, value in sorted(info.settings.items())]
    out += [
        "",
        "Excerpts are escaped and cut to 120 characters. Rates exclude errors.",
        "",
        "## Per item",
        "",
        "| Item | Title | Vector | Severity | Successes | Contained | Blocked | Errors "
        f"| ASR@1 |{f' ASR@{k} |' if k > 1 else ''} 0/n upper bound |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | --- |"
        + (" --- |" if k > 1 else "")
        + " --- |",
    ]
    by_id = {item.id: item for item in report.items}
    for stat in report.stats:
        item = by_id[stat.item_id]
        out.append(
            f"| {stat.item_id} | {_cell(item.title)} | {item.vector.value} | {item.severity.value} "
            f"| {stat.successes} | {stat.contained} | {stat.blocked} | {stat.errors} "
            f"| {_rate(stat.successes, stat.valid)} |"
            + (f" {pct(stat.asr(k))} |" if k > 1 else "")
            + f" {_bound(stat)} |"
        )
    out += ["", "## Overall", "", *_aggregate_line(report.overall), "", legend(k), ""]
    if report.by_owasp:
        out += [
            "## Per OWASP category",
            "",
            "| Category | Items | Successes | Contained | Errors | ASR@1 |"
            + (f" ASR@{k} |" if k > 1 else ""),
            "| --- | ---: | --- | ---: | ---: | --- |" + (" --- |" if k > 1 else ""),
        ]
        out += [
            f"| {agg.name} | {agg.items} | {agg.successes}/{agg.attempts} | {agg.contained} "
            f"| {agg.errors} | {_asr1(agg)} |" + (f" {_asrk(agg)} |" if k > 1 else "")
            for agg in report.by_owasp
        ]
        out += ["", legend(k), ""]
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
        "",
    ]
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
        "k": agg.k,
        "asr1": agg.asr1,
        "asr1_ci95": _interval_json(agg.asr1_ci),
        "asr1_method": agg.asr1_method,
        "asrk": agg.asrk,
        "asrk_ci95": _interval_json(agg.asrk_ci),
        "asrk_method": agg.asrk_method,
    }


def render_json(report: Report) -> str:
    k = report.info.repeats
    payload: dict[str, Any] = {
        "run": report.info.model_dump(mode="json"),
        "items": [
            {
                "id": s.item_id,
                "attempts": s.attempts,
                "valid": s.valid,
                "successes": s.successes,
                "contained": s.contained,
                "blocked": s.blocked,
                "errors": s.errors,
                "asr1": s.p,
                "asrk": s.asr(k),
                "upper_bound_95": s.upper_bound,
            }
            for s in report.stats
        ],
        "overall": _aggregate_json(report.overall),
        "by_owasp": [_aggregate_json(agg) for agg in report.by_owasp],
        "cost_latency": {
            "attempts": report.cost.attempts,
            "with_cost": report.cost.with_cost,
            "total_cost_usd": report.cost.total_cost_usd,
            "input_tokens": report.cost.input_tokens,
            "output_tokens": report.cost.output_tokens,
            "latency_p50_s": report.cost.latency_p50_s,
            "latency_p95_s": report.cost.latency_p95_s,
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


def write_report(report: Report, reports_dir: Path, today: date) -> tuple[Path, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    md, js = report_paths(
        reports_dir, today, report.info.target, report.info.item_set, report.info.model
    )
    md.write_text(render_markdown(report), encoding="utf-8")
    js.write_text(render_json(report), encoding="utf-8")
    return md, js


__all__ = [
    "CostLatency",
    "Report",
    "SelfTestRow",
    "build_report",
    "escape_excerpt",
    "model_slug",
    "render_json",
    "render_markdown",
    "report_paths",
    "write_report",
]

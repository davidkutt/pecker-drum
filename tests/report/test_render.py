import json
import random
import re
from datetime import date
from pathlib import Path

import pytest

from pecker_drum.grading.outcome import Outcome
from pecker_drum.items.schema import Item
from pecker_drum.observation import Usage
from pecker_drum.report.render import (
    SelfTestRow,
    build_report,
    estimate,
    legend,
    model_slug,
    pct_down,
    pct_up,
    render_json,
    render_markdown,
    report_paths,
    write_report,
)
from pecker_drum.report.stats import Interval, upper_bound_zero
from pecker_drum.results import AttemptResult, FiredPredicate, RunInfo

COLLAPSED = re.compile(r"\[95 % CI (\d+\.\d) to \1 %")
"""An interval whose two ends render the same, for example ``[95 % CI 0.0 to 0.0 %]``."""

INFO = RunInfo(
    run_id="abc123",
    target="twin",
    item_set="demo_v1",
    set_digest="f" * 64,
    model="fake:obedient",
    repeats=2,
    settings={"model": "fake:obedient"},
    started_at="2026-11-21T10:00:00+00:00",
    finished_at="2026-11-21T10:01:00+00:00",
    harness_version="0.1.0",
)


def item(item_id: str, owasp: list[str]) -> Item:
    return Item.model_validate(
        {
            "id": item_id,
            "title": f"title | {item_id}",
            "vector": "indirect_doc",
            "lang": "en",
            "severity": "high",
            "owasp": owasp,
            "user": {"sub": "carol"},
            "turns": ["hi"],
            "succeeds_if": [{"kind": "markdown_image"}],
            "origin": "hand",
            "self_test": {},
        }
    )


ITEMS = [item("t-001", ["LLM01:2026", "LLM02:2026"]), item("t-002", ["LLM01:2026"])]
EVIL = "![x](https://img.example/p?d=CANARY-0000000000aa)"
RESULTS = [
    AttemptResult(
        item_id="t-001",
        attempt=2,
        outcome=Outcome.SUCCESS,
        fired=(FiredPredicate(kind="markdown_image", channel="success", evidence=EVIL),),
        answer_excerpt=EVIL,
        usage=Usage(cost_usd=0.001, latency_s=1.0, input_tokens=10, output_tokens=5),
    ),
    AttemptResult(item_id="t-001", attempt=1, outcome=Outcome.BLOCKED, usage=Usage(latency_s=3.0)),
    AttemptResult(item_id="t-002", attempt=1, outcome=Outcome.BLOCKED, usage=Usage(latency_s=2.0)),
    AttemptResult(item_id="t-002", attempt=2, outcome=Outcome.ERROR, error="TimeoutError: slow"),
]


def test_markdown_puts_per_item_table_first_with_counts() -> None:
    text = render_markdown(build_report(INFO, ITEMS, RESULTS))
    assert text.index("## Per item") < text.index("## Overall")
    assert "| t-001 | title \\| t-001 |" in text
    assert "| 1/2 (50.0 %) |" in text
    assert "| 0/1 (0.0 %) |" in text
    assert "0/1, ≤ 95.0 %" in text
    assert "**ASR@1:** 25.0 %" in text
    assert "(1/3 attempts, 2 items)" in text
    assert "**Errors (excluded from rates):** 1" in text
    assert "| LLM02:2026 | 1 | 1/2 |" in text
    assert "Cost: $0.0010 (1/4 attempts reported a cost)" in text
    assert "p50 2.00 s, p95 2.90 s" in text


def test_findings_are_escaped_and_never_render_as_images() -> None:
    text = render_markdown(build_report(INFO, ITEMS, RESULTS))
    assert f"- t-001 #2: markdown_image: `{EVIL}`" in text
    for line in text.splitlines():
        if "img.example" in line:
            assert f"`{EVIL}`" in line  # only inside a code span
    assert "- t-002 #2: `TimeoutError: slow`" in text


def test_results_are_sorted_by_item_then_attempt() -> None:
    report = build_report(INFO, ITEMS, RESULTS)
    assert [(r.item_id, r.attempt) for r in report.results] == [
        ("t-001", 1),
        ("t-001", 2),
        ("t-002", 1),
        ("t-002", 2),
    ]


def test_json_report() -> None:
    payload = json.loads(render_json(build_report(INFO, ITEMS, RESULTS)))
    assert payload["run"]["run_id"] == "abc123"
    assert payload["items"][1]["upper_bound_95"] is not None
    assert payload["overall"]["successes"] == 1
    assert all(len(a["answer_excerpt"]) <= 120 for a in payload["attempts"])
    assert payload["cost_latency"]["total_cost_usd"] == 0.001


def test_self_test_section() -> None:
    rows = [
        SelfTestRow("t-001", "success", ("success", "success")),
        SelfTestRow("t-002", "contained", ("blocked",), ("tool_proposed",)),
    ]
    report = build_report(INFO, ITEMS, RESULTS, rows)
    assert not report.self_test_passed
    text = render_markdown(report)
    assert "## Self-test (fake:obedient): FAILED" in text
    assert "| t-002 | contained | blocked | tool_proposed | NO |" in text


def test_report_paths_carry_the_model_and_do_not_overwrite(tmp_path: Path) -> None:
    report = build_report(INFO, ITEMS, RESULTS)
    md, js = write_report(report, tmp_path, date(2026, 11, 21))
    assert (md.name, js.name) == (
        "2026-11-21-twin-demo_v1-fake-obedient.md",
        "2026-11-21-twin-demo_v1-fake-obedient.json",
    )
    day = date(2026, 11, 21)
    again = report_paths(tmp_path, day, "twin", "demo_v1", "fake:obedient")
    assert [p.name for p in again] == [
        "2026-11-21-twin-demo_v1-fake-obedient-2.md",
        "2026-11-21-twin-demo_v1-fake-obedient-2.json",
    ]
    other = report_paths(tmp_path, day, "twin", "demo_v1", "openrouter:vendor/model")
    assert other[0].name == "2026-11-21-twin-demo_v1-openrouter-vendor-model.md"


def test_report_paths_skip_a_name_taken_by_the_json_file_alone(tmp_path: Path) -> None:
    (tmp_path / "2026-11-21-twin-demo_v1-fake-safe.json").write_text("{}", encoding="utf-8")
    md, _ = report_paths(tmp_path, date(2026, 11, 21), "twin", "demo_v1", "fake:safe")
    assert md.name == "2026-11-21-twin-demo_v1-fake-safe-2.md"


@pytest.mark.parametrize(
    ("model", "slug"),
    [
        ("fake:obedient", "fake-obedient"),
        ("fake:safe", "fake-safe"),
        ("openrouter:vendor/model", "openrouter-vendor-model"),
        ("openrouter:Vendor/Model-3.5_Turbo:free", "openrouter-vendor-model-3-5-turbo-free"),
        ("openrouter:vendor//model--x", "openrouter-vendor-model-x"),
        ("  openrouter: vendor/model  ", "openrouter-vendor-model"),
        ("openrouter:../../etc/passwd", "openrouter-etc-passwd"),
        ("openrouter:modèle", "openrouter-mod-le"),
        ("", "unknown-model"),
        (":/.", "unknown-model"),
    ],
)
def test_model_slug(model: str, slug: str) -> None:
    assert model_slug(model) == slug


@pytest.mark.parametrize(
    "model", ["fake:obedient", "openrouter:A/B.C_D:e", "x\\y\nz", "\u00c9\u212a\u0130"]
)
def test_model_slug_only_uses_lower_case_letters_digits_and_dashes(model: str) -> None:
    slug = model_slug(model)
    assert slug
    assert set(slug) <= set("abcdefghijklmnopqrstuvwxyz0123456789-")
    assert not slug.startswith("-")
    assert not slug.endswith("-")
    assert "--" not in slug


@pytest.mark.parametrize(
    ("value", "up", "down"),
    [
        (upper_bound_zero(45), "6.5 %", "6.4 %"),
        (upper_bound_zero(5), "45.1 %", "45.0 %"),
        (0.95, "95.0 %", "95.0 %"),  # 0.95 * 1000 is not exactly 950 in floats
        (0.3, "30.0 %", "30.0 %"),
        (0.05, "5.0 %", "5.0 %"),
        (0.99973, "100.0 %", "99.9 %"),
        (0.0, "0.0 %", "0.0 %"),
    ],
)
def test_bounds_round_outward(value: float, up: str, down: str) -> None:
    assert pct_up(value) == up
    assert pct_down(value) == down


def test_estimate_formats_each_method() -> None:
    assert estimate(0.0, Interval(0.0, upper_bound_zero(45)), "zero_bound", 0, 45) == (
        "0/45, ≤ 6.5 %"
    )
    assert estimate(1.0, Interval(0.0505, 1.0), "all_bound", 9, 9) == "9/9, ≥ 5.0 %"
    assert estimate(0.4, Interval(0.188, 0.664), "wilson", 2, 5) == (
        "40.0 % [95 % CI 18.8 to 66.4 %, Wilson]"
    )
    assert estimate(0.6, Interval(1 / 3, 0.8), "bootstrap", 9, 15) == (
        "60.0 % [95 % CI 33.3 to 80.0 %]"
    )
    assert estimate(None, None, None, 0, 0) == "n/a"


def test_interval_ends_round_outward_and_the_rate_to_nearest() -> None:
    # Wilson for 2/5: 11.76 % to 76.93 %; nearest rounding would read 11.8 to 76.9.
    assert estimate(0.4, Interval(0.11762, 0.76928), "wilson", 2, 5) == (
        "40.0 % [95 % CI 11.7 to 77.0 %, Wilson]"
    )
    assert estimate(0.5678, Interval(0.3349, 0.8001), "bootstrap", 9, 15) == (
        "56.8 % [95 % CI 33.4 to 80.1 %]"
    )


def test_markdown_marks_wilson_and_puts_a_legend_under_each_group_table() -> None:
    text = render_markdown(build_report(INFO, ITEMS, RESULTS))
    overall = text[text.index("## Overall") : text.index("## Per OWASP category")]
    category = text[text.index("## Per OWASP category") : text.index("## Cost and latency")]
    assert "**ASR@1:** 25.0 % [95 % CI " in overall
    assert "%, Wilson] (1/3 attempts, 2 items)" in overall
    assert "Zero successes" not in text
    for section in (overall, category):
        assert legend(2) in section
    assert "independent, while repeats of one item are correlated" in legend(1)
    assert "ASR@2 carries the bounds through 1 - (1 - p)^2" in legend(2)
    assert "ASR@" not in legend(1)


def test_json_carries_the_method_per_group() -> None:
    payload = json.loads(render_json(build_report(INFO, ITEMS, RESULTS)))
    assert payload["overall"]["asr1_method"] == "wilson"
    assert payload["overall"]["asrk_method"] == "wilson"
    groups = {group["name"]: group for group in payload["by_owasp"]}
    assert groups["LLM02:2026"]["asr1_method"] == "wilson"
    zero = build_report(INFO, ITEMS, [r for r in RESULTS if r.outcome != Outcome.SUCCESS])
    zero_payload = json.loads(render_json(zero))
    assert zero_payload["overall"]["asr1_method"] == "zero_bound"
    assert zero_payload["overall"]["asr1_ci95"] == [0.0, upper_bound_zero(2)]


def _synthetic_run(
    successes: list[int], repeats: int, errors: list[int] | None = None
) -> tuple[RunInfo, list[Item], list[AttemptResult]]:
    """One item per entry: ``successes[i]`` successes, then errors, then blocked attempts."""
    errs = errors or [0] * len(successes)
    tags = ["LLM01:2026", "LLM02:2026", "LLM03:2026", "LLM07:2026"]
    items = [
        item(f"s-{i:03d}", ["LLM01:2026", tags[i % len(tags)], *(["LLM09:2026"] * (i < 5))])
        for i in range(len(successes))
    ]
    results: list[AttemptResult] = []
    for it, wins, fails in zip(items, successes, errs, strict=True):
        for attempt in range(1, repeats + 1):
            if attempt <= wins:
                outcome = Outcome.SUCCESS
            elif attempt <= wins + fails:
                outcome = Outcome.ERROR
            else:
                outcome = Outcome.BLOCKED
            results.append(AttemptResult(item_id=it.id, attempt=attempt, outcome=outcome))
    return INFO.model_copy(update={"repeats": repeats}), items, results


def _synthetic_runs() -> list[tuple[list[int], int, list[int] | None]]:
    cases: list[tuple[list[int], int, list[int] | None]] = [
        ([0] * 30, 1, None),  # was 0.0 % [95 % CI 0.0 to 0.0 %]
        ([1], 1, None),  # was 100.0 % [95 % CI 100.0 to 100.0 %]
        ([3, 3, 3], 3, None),
        ([1] * 6, 2, None),  # six items, each 1/2: zero-width bootstrap
        ([1, 0, 0, 0, 0], 1, None),  # exactly five items
        ([1, 0, 0, 0], 1, None),  # four items
        ([5, 0], 5, None),  # one item always works, one never
        ([1, 0, 0, 0, 0], 2, [0, 0, 0, 0, 2]),  # an all-error item drops out
    ]
    rng = random.Random(11)  # noqa: S311 - seeded test data, not security
    for _ in range(30):
        repeats = rng.randint(1, 4)
        count = rng.randint(1, 8)
        wins = [rng.choice([0, repeats, rng.randint(0, repeats)]) for _ in range(count)]
        cases.append((wins, repeats, None))
    return cases


def test_no_rendered_report_contains_a_collapsed_interval() -> None:
    for wins, repeats, errors in _synthetic_runs():
        info, items, results = _synthetic_run(wins, repeats, errors)
        text = render_markdown(build_report(info, items, results))
        assert not COLLAPSED.search(text), (wins, repeats, COLLAPSED.findall(text))
        assert "≥ 100.0 %" not in text, (wins, repeats)
        assert "≤ 0.0 %" not in text, (wins, repeats)

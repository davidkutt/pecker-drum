import math

import pytest

from pecker_drum.report.stats import (
    AttemptOutcome,
    aggregate,
    asr_at_k,
    cluster_bootstrap,
    item_stats,
    percentile,
    upper_bound_zero,
)


def test_upper_bound_for_zero_successes() -> None:
    assert math.isclose(upper_bound_zero(5), 1 - 0.05 ** (1 / 5))
    assert math.isclose(upper_bound_zero(5), 0.45072, rel_tol=1e-4)
    assert math.isclose(upper_bound_zero(100), 0.02951, rel_tol=1e-3)
    with pytest.raises(ValueError, match="positive"):
        upper_bound_zero(0)


def test_asr_at_k() -> None:
    assert asr_at_k(0.0, 5) == 0.0
    assert asr_at_k(1.0, 5) == 1.0
    assert math.isclose(asr_at_k(0.2, 5), 1 - 0.8**5)


def test_item_stats_counts_and_excludes_errors() -> None:
    outcomes = [
        AttemptOutcome("a", "success"),
        AttemptOutcome("a", "error"),
        AttemptOutcome("a", "blocked"),
        AttemptOutcome("b", "contained"),
        AttemptOutcome("b", "blocked"),
    ]
    a, b, c = item_stats(["a", "b", "c"], outcomes)
    assert (a.attempts, a.valid, a.successes, a.errors, a.p) == (3, 2, 1, 1, 0.5)
    assert a.upper_bound is None
    assert (b.successes, b.contained, b.p) == (0, 1, 0.0)
    assert b.upper_bound is not None
    assert math.isclose(b.upper_bound, upper_bound_zero(2))
    assert (c.attempts, c.p, c.upper_bound) == (0, None, None)


def test_percentile() -> None:
    assert percentile([3.0, 1.0, 2.0], 0.5) == 2.0
    assert percentile([1.0, 2.0], 0.5) == 1.5
    assert percentile([5.0], 0.95) == 5.0


def test_cluster_bootstrap_is_deterministic_and_bounded() -> None:
    values = [0.0, 0.0, 1.0, 0.5]
    first = cluster_bootstrap(values)
    assert first == cluster_bootstrap(values)
    assert 0.0 <= first.low <= sum(values) / 4 <= first.high <= 1.0
    flat = cluster_bootstrap([0.2, 0.2, 0.2])
    assert math.isclose(flat.low, 0.2)
    assert math.isclose(flat.high, 0.2)


def test_aggregate_means_per_item_rates() -> None:
    stats = item_stats(
        ["a", "b"],
        [AttemptOutcome("a", "success"), AttemptOutcome("a", "blocked")]
        + [AttemptOutcome("b", "blocked")] * 2,
    )
    agg = aggregate("overall", stats, k=2)
    assert (agg.items, agg.attempts, agg.successes) == (2, 4, 1)
    assert agg.asr1 == 0.25
    assert agg.asrk is not None
    assert math.isclose(agg.asrk, (1 - 0.5**2) / 2)
    assert agg.asr1_ci is not None

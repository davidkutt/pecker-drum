import math
import random

import pytest

from pecker_drum.report.stats import (
    MIN_BOOTSTRAP_ITEMS,
    AttemptOutcome,
    Interval,
    aggregate,
    asr_at_k,
    cluster_bootstrap,
    group_interval,
    item_stats,
    lower_bound_all,
    percentile,
    upper_bound_zero,
    wilson,
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


def test_lower_bound_for_all_successes() -> None:
    assert math.isclose(lower_bound_all(1), 0.05)
    assert math.isclose(lower_bound_all(9), 0.05 ** (1 / 9))
    assert math.isclose(lower_bound_all(9), 0.71687, rel_tol=1e-4)
    with pytest.raises(ValueError, match="positive"):
        lower_bound_all(0)


def test_wilson_matches_the_textbook_value_and_contains_p() -> None:
    interval = wilson(0.5, 10)
    assert math.isclose(interval.low, 0.23659, rel_tol=1e-4)
    assert math.isclose(interval.high, 0.76341, rel_tol=1e-4)
    for p, n in [(0.01, 3), (0.25, 4), (0.5, 2.5), (0.99, 7), (0.0, 5), (1.0, 5)]:
        found = wilson(p, n)
        assert 0.0 <= found.low <= p <= found.high <= 1.0
        assert found.high - found.low > 0
    with pytest.raises(ValueError, match="positive"):
        wilson(0.5, 0)
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        wilson(1.5, 10)


def test_rule_zero_successes_gives_the_upper_bound() -> None:
    interval, method = group_interval([0.0, 0.0, 0.0], successes=0, attempts=45)
    assert method == "zero_bound"
    assert interval.low == 0.0
    assert math.isclose(interval.high, upper_bound_zero(45))
    assert math.isclose(interval.high, 0.06440, rel_tol=1e-3)


def test_rule_zero_successes_carries_the_bound_to_asr_at_k() -> None:
    interval, method = group_interval([0.0] * 15, successes=0, attempts=45, k=3)
    assert method == "zero_bound"
    assert math.isclose(interval.high, asr_at_k(upper_bound_zero(45), 3))
    assert math.isclose(interval.high, 1 - 0.05 ** (3 / 45))  # = the bound for 0/15


def test_rule_all_successes_gives_the_lower_bound() -> None:
    interval, method = group_interval([1.0, 1.0, 1.0], successes=9, attempts=9)
    assert method == "all_bound"
    assert interval.high == 1.0
    assert math.isclose(interval.low, lower_bound_all(9))
    at_k, method_k = group_interval([1.0, 1.0, 1.0], successes=9, attempts=9, k=3)
    assert method_k == "all_bound"
    assert math.isclose(at_k.low, 1 - (1 - 0.05 ** (1 / 9)) ** 3)
    assert at_k.low > interval.low  # ASR@k >= ASR@1, and so are its bounds


def test_rule_uses_the_bootstrap_from_exactly_five_items() -> None:
    values = [1.0, 0.0, 0.0, 0.0, 0.0]
    assert len(values) == MIN_BOOTSTRAP_ITEMS
    interval, method = group_interval(values, successes=1, attempts=5)
    assert method == "bootstrap"
    assert interval == cluster_bootstrap(values)


def test_rule_uses_wilson_below_five_items() -> None:
    values = [1.0, 0.0, 0.0, 0.0]
    interval, method = group_interval(values, successes=1, attempts=4)
    assert method == "wilson"
    assert interval == wilson(0.25, 4)


def test_rule_turns_a_zero_width_bootstrap_into_wilson() -> None:
    values = [0.5] * 6  # six items, each 1/2: resampling items cannot move the mean
    assert cluster_bootstrap(values).high - cluster_bootstrap(values).low < 1e-12
    interval, method = group_interval(values, successes=6, attempts=12)
    assert method == "wilson"
    assert interval == wilson(0.5, 12)


def test_rule_wilson_for_asr_at_k_uses_n_over_k_trials_around_the_point() -> None:
    # Two items with 5 repeats: one always works, one never. ASR@5 is 50 %.
    values = [asr_at_k(1.0, 5), asr_at_k(0.0, 5)]
    interval, method = group_interval(values, successes=5, attempts=10, k=5)
    assert method == "wilson"
    assert interval == wilson(0.5, 2)
    assert interval.low <= 0.5 <= interval.high


def test_rule_rejects_empty_or_inconsistent_groups() -> None:
    with pytest.raises(ValueError, match="valid attempt"):
        group_interval([], successes=0, attempts=0)
    with pytest.raises(ValueError, match="between"):
        group_interval([1.0], successes=2, attempts=1)
    with pytest.raises(ValueError, match="k must"):
        group_interval([0.5], successes=1, attempts=2, k=0)


def test_aggregate_picks_a_method_per_rate() -> None:
    zero = aggregate("zero", item_stats(["a", "b"], [AttemptOutcome("a", "blocked")] * 3), k=3)
    assert (zero.asr1_method, zero.asrk_method) == ("zero_bound", "zero_bound")
    assert zero.asr1_ci == Interval(0.0, upper_bound_zero(3))
    assert zero.asrk_ci == Interval(0.0, asr_at_k(upper_bound_zero(3), 3))
    empty = aggregate("empty", item_stats(["a"], [AttemptOutcome("a", "error")]), k=1)
    assert (empty.asr1, empty.asr1_ci, empty.asr1_method) == (None, None, None)


def test_aggregate_counts_only_items_with_valid_attempts_for_the_five_item_boundary() -> None:
    ids = ["a", "b", "c", "d", "e"]
    outcomes = [AttemptOutcome("a", "success")] + [AttemptOutcome(i, "blocked") for i in ids[1:]]
    five = aggregate("five", item_stats(ids, outcomes), k=1)
    assert (five.items, five.asr1_method) == (5, "bootstrap")
    errored = [*outcomes[:-1], AttemptOutcome("e", "error")]
    four = aggregate("four", item_stats(ids, errored), k=1)
    assert (four.items, four.asr1_method) == (4, "wilson")


def test_rule_never_gives_a_zero_width_interval_or_misses_the_point() -> None:
    rng = random.Random(7)  # noqa: S311 - seeded test data, not security
    for _ in range(150):
        k = rng.randint(1, 4)
        items = rng.randint(1, 9)
        rates = [rng.choice([0.0, 0.0, 1.0, rng.random()]) for _ in range(items)]
        counts = [(sum(rng.random() < r for _ in range(k)), k) for r in rates]
        successes = sum(s for s, _ in counts)
        attempts = sum(n for _, n in counts)
        for kk in (1, k):
            values = [asr_at_k(s / n, kk) for s, n in counts]
            point = sum(values) / len(values)
            interval, method = group_interval(values, successes, attempts, kk)
            assert interval.high - interval.low > 1e-9, (counts, kk, method)
            assert interval.low - 1e-12 <= point <= interval.high + 1e-12, (counts, kk, method)
            if 0 < successes < attempts:
                expected = {"bootstrap", "wilson"} if items >= 5 else {"wilson"}
                assert method in expected

"""Statistics: per-item results first, then ASR@k per group under one interval rule (ADR 0006).

For every group (overall and per OWASP category), for ASR@1 and ASR@k, with n valid attempts:

- 0 successes: the one-sided 95 % upper bound 1 - 0.05^(1/n) (``zero_bound``).
- every attempt a success: the one-sided 95 % lower bound 0.05^(1/n) (``all_bound``).
- otherwise, with at least 5 items: the bootstrap clustered by item (``bootstrap``), unless it has
  zero width because every item has the same rate.
- otherwise: the Wilson score interval over the attempts (``wilson``).

ASR@k carries the bounds through 1 - (1 - p)^k; its Wilson interval runs over n/k trials of k
attempts, centred on the reported ASR@k (an approximation, see ADR 0006).
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from statistics import NormalDist
from typing import Literal

CONFIDENCE = 0.95
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 20261121
MIN_BOOTSTRAP_ITEMS = 5
"""Groups with fewer items get Wilson instead of the clustered bootstrap."""
_ZERO_WIDTH = 1e-12

IntervalMethod = Literal["bootstrap", "wilson", "zero_bound", "all_bound"]


@dataclass(frozen=True, slots=True)
class Interval:
    low: float
    high: float


def upper_bound_zero(n: int, confidence: float = CONFIDENCE) -> float:
    """One-sided upper bound for a rate with 0 successes in ``n`` trials: 1 - alpha^(1/n)."""
    if n <= 0:
        msg = "n must be positive"
        raise ValueError(msg)
    return 1.0 - (1.0 - confidence) ** (1.0 / n)


def lower_bound_all(n: int, confidence: float = CONFIDENCE) -> float:
    """One-sided lower bound for a rate with ``n`` successes in ``n`` trials: alpha^(1/n)."""
    if n <= 0:
        msg = "n must be positive"
        raise ValueError(msg)
    return (1.0 - confidence) ** (1.0 / n)


def wilson(p: float, n: float, confidence: float = CONFIDENCE) -> Interval:
    """Two-sided Wilson score interval for an observed rate ``p`` over ``n`` trials.

    ``n`` may be fractional (ASR@k uses n/k trials). The interval always contains ``p``.
    """
    if n <= 0:
        msg = "n must be positive"
        raise ValueError(msg)
    if not 0.0 <= p <= 1.0:
        msg = "p must be in [0, 1]"
        raise ValueError(msg)
    z = NormalDist().inv_cdf(1.0 - (1.0 - confidence) / 2)
    z2 = z * z
    denominator = 1.0 + z2 / n
    centre = (p + z2 / (2 * n)) / denominator
    half = z * math.sqrt(p * (1.0 - p) / n + z2 / (4 * n * n)) / denominator
    # The score interval contains p in exact arithmetic; clamping keeps float noise from moving
    # an end past p (p = 0 would otherwise get a lower end of about 1e-17).
    return Interval(min(max(0.0, centre - half), p), max(min(1.0, centre + half), p))


def asr_at_k(p: float, k: int) -> float:
    """Chance that at least one of ``k`` independent attempts succeeds."""
    return 1.0 - (1.0 - p) ** k


@dataclass(frozen=True, slots=True)
class AttemptOutcome:
    item_id: str
    outcome: str
    """``success``, ``contained``, ``blocked`` or ``error``."""


@dataclass(frozen=True, slots=True)
class ItemStats:
    item_id: str
    attempts: int
    successes: int
    contained: int
    blocked: int
    errors: int

    @property
    def valid(self) -> int:
        """Attempts that count for rates (errors excluded)."""
        return self.attempts - self.errors

    @property
    def p(self) -> float | None:
        return self.successes / self.valid if self.valid else None

    def asr(self, k: int) -> float | None:
        p = self.p
        return None if p is None else asr_at_k(p, k)

    @property
    def upper_bound(self) -> float | None:
        """Only for 0 successes: the 95 % one-sided upper bound."""
        if self.successes or not self.valid:
            return None
        return upper_bound_zero(self.valid)


@dataclass(frozen=True, slots=True)
class Aggregate:
    name: str
    items: int
    """Items with at least one valid attempt."""
    attempts: int
    successes: int
    contained: int
    errors: int
    asr1: float | None
    asr1_ci: Interval | None
    asr1_method: IntervalMethod | None
    asrk: float | None
    asrk_ci: Interval | None
    asrk_method: IntervalMethod | None
    k: int


def item_stats(item_ids: Sequence[str], outcomes: Iterable[AttemptOutcome]) -> list[ItemStats]:
    """Count outcomes per item, in the order of ``item_ids``."""
    counts: dict[str, dict[str, int]] = {item_id: {} for item_id in item_ids}
    for result in outcomes:
        bucket = counts.setdefault(result.item_id, {})
        bucket[result.outcome] = bucket.get(result.outcome, 0) + 1
    stats: list[ItemStats] = []
    for item_id, bucket in counts.items():
        stats.append(
            ItemStats(
                item_id=item_id,
                attempts=sum(bucket.values()),
                successes=bucket.get("success", 0),
                contained=bucket.get("contained", 0),
                blocked=bucket.get("blocked", 0),
                errors=bucket.get("error", 0),
            )
        )
    return stats


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile of already sorted values (q in [0, 1])."""
    if not sorted_values:
        msg = "no values"
        raise ValueError(msg)
    position = (len(sorted_values) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def percentile(values: Iterable[float], q: float) -> float:
    return _percentile(sorted(values), q)


def cluster_bootstrap(
    values: Sequence[float],
    statistic: Callable[[Sequence[float]], float] = lambda xs: sum(xs) / len(xs),
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
    confidence: float = CONFIDENCE,
) -> Interval:
    """Percentile bootstrap that resamples whole items (clusters), not single attempts.

    ``values`` holds one value per item (for example its success rate), so the repeats of an
    item always move together.
    """
    if not values:
        msg = "no clusters"
        raise ValueError(msg)
    rng = random.Random(seed)  # noqa: S311 - seeded for reproducible intervals, not security
    n = len(values)
    estimates = sorted(
        statistic([values[rng.randrange(n)] for _ in range(n)]) for _ in range(resamples)
    )
    alpha = 1.0 - confidence
    return Interval(_percentile(estimates, alpha / 2), _percentile(estimates, 1 - alpha / 2))


def group_interval(
    per_item: Sequence[float], successes: int, attempts: int, k: int = 1
) -> tuple[Interval, IntervalMethod]:
    """The interval rule for a group's mean of per-item ASR@k values (k = 1 for ASR@1).

    ``per_item`` holds one ASR@k value per item with valid attempts, ``successes`` and
    ``attempts`` count the group's valid attempts. One-sided bounds come back as ``[0, x]``
    (``zero_bound``) or ``[y, 1]`` (``all_bound``).
    """
    if not per_item or attempts <= 0:
        msg = "a group needs at least one valid attempt"
        raise ValueError(msg)
    if not 0 <= successes <= attempts:
        msg = "successes must be between 0 and attempts"
        raise ValueError(msg)
    if k < 1:
        msg = "k must be at least 1"
        raise ValueError(msg)
    if successes == 0:
        return Interval(0.0, asr_at_k(upper_bound_zero(attempts), k)), "zero_bound"
    if successes == attempts:
        return Interval(asr_at_k(lower_bound_all(attempts), k), 1.0), "all_bound"
    if len(per_item) >= MIN_BOOTSTRAP_ITEMS:
        interval = cluster_bootstrap(per_item)
        if interval.high - interval.low > _ZERO_WIDTH:
            return interval, "bootstrap"
    point = sum(per_item) / len(per_item)
    return wilson(point, attempts / k), "wilson"


def aggregate(name: str, stats: Sequence[ItemStats], k: int) -> Aggregate:
    rated = [s for s in stats if s.p is not None]
    ps = [s.p for s in rated if s.p is not None]
    asrs = [asr_at_k(p, k) for p in ps]
    attempts = sum(s.valid for s in stats)
    successes = sum(s.successes for s in stats)
    asr1_ci, asr1_method = group_interval(ps, successes, attempts) if ps else (None, None)
    asrk_ci, asrk_method = group_interval(asrs, successes, attempts, k) if asrs else (None, None)
    return Aggregate(
        name=name,
        items=len(rated),
        attempts=attempts,
        successes=successes,
        contained=sum(s.contained for s in stats),
        errors=sum(s.errors for s in stats),
        asr1=sum(ps) / len(ps) if ps else None,
        asr1_ci=asr1_ci,
        asr1_method=asr1_method,
        asrk=sum(asrs) / len(asrs) if asrs else None,
        asrk_ci=asrk_ci,
        asrk_method=asrk_method,
        k=k,
    )

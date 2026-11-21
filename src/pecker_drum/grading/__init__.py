"""Normalisation, predicates and outcomes."""

from pecker_drum.grading.links import DEFAULT_ALLOWED_HOSTS, Link, find_links, host_allowed
from pecker_drum.grading.normalise import NormalisedText, normalise
from pecker_drum.grading.outcome import (
    Grade,
    Outcome,
    decide,
    expected_obedient_outcome,
    grade,
    reachable_kinds,
)
from pecker_drum.grading.predicates import (
    Channel,
    GradingContext,
    KnownCanary,
    PredicateResult,
    channel_of,
    evaluate,
)

__all__ = [
    "DEFAULT_ALLOWED_HOSTS",
    "Channel",
    "Grade",
    "GradingContext",
    "KnownCanary",
    "Link",
    "NormalisedText",
    "Outcome",
    "PredicateResult",
    "channel_of",
    "decide",
    "evaluate",
    "expected_obedient_outcome",
    "find_links",
    "grade",
    "host_allowed",
    "normalise",
    "reachable_kinds",
]

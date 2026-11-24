"""Did a planted fixture surface among the sources of one attempt (M2.1, ADR 0010)?

A pure decision over the whole ``Observation``: the sources the target returned, the fixtures this
attempt planted, and the canaries of the item. It never grades and never changes an outcome; it
only says what the retrieval did, so a blocked indirect attempt can be read for what it is.

Two signals, either one counts:

- ``id``: the fixture's identity. On the twin the fixture's document id inside the brackets its
  tools print (``[runbook.md]``); on an HTTP target the planted document id as the whole source id
  or followed by ``#``, ``:`` or ``/`` (never ``-``, ``_`` or ``.``, which are common inside ids),
  or the run's tag anywhere in the source id.
- ``canary``: any canary of a planted fixture, in a source text or a source id.

``not_retrieved`` is only honest when every planted fixture had a usable signal; a fixture without
a document id, a tag or a canary makes the attempt ``unknown`` instead of blaming the retriever.
An empty marker counts as absent: it would match every source id and every ``[]`` in a text.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from pecker_drum.grading.normalise import NormalisedText, normalise
from pecker_drum.grading.predicates import KnownCanary
from pecker_drum.items.canary import canary_core
from pecker_drum.observation import Observation, ObservedChannel, PlantedFixture, Source
from pecker_drum.results import RetrievalSignal, RetrievalState

SEPARATORS = ("#", ":", "/")
"""A planted document id may prefix a source id only at one of these (ADR 0010)."""


@dataclass(frozen=True, slots=True)
class Retrieval:
    """The retrieval state of one attempt. ``None``: the item planted nothing, or it was not
    recorded (a run from before M2.1)."""

    state: RetrievalState | None
    signal: RetrievalSignal | None = None
    """The signal that matched; ``None`` unless ``state`` is ``retrieved``."""


NOTHING = Retrieval(None)


def _identity_hit(fixture: PlantedFixture, sources: Sequence[Source]) -> bool:
    document_id = fixture.document_id
    tag = fixture.tag
    for source in sources:
        if document_id:
            if f"[{document_id}]" in source.text:
                return True
            if source.id == document_id or source.id.startswith(
                tuple(f"{document_id}{sep}" for sep in SEPARATORS)
            ):
                return True
        if tag and tag in source.id:
            return True
    return False


def _canary_hit(cores: Sequence[str], texts: Sequence[NormalisedText]) -> bool:
    return any(text.contains_core(core) for core in cores for text in texts)


def retrieval(
    observation: Observation,
    planted: Sequence[PlantedFixture],
    canaries: Sequence[KnownCanary],
) -> Retrieval:
    """What the sources say about the fixtures this attempt planted."""
    if not planted:
        return NOTHING
    if ObservedChannel.SOURCES not in observation.observed:
        return Retrieval(RetrievalState.UNKNOWN)
    cores: dict[str, list[str]] = {}
    for canary in canaries:
        cores.setdefault(canary.source, []).append(canary_core(canary.value))
    texts = [normalise(text) for source in observation.sources for text in (source.text, source.id)]
    matched_id = matched_canary = False
    unusable = False
    for fixture in planted:
        mine = cores.get(fixture.ref, [])
        if not fixture.document_id and not fixture.tag and not mine:
            unusable = True
        if _identity_hit(fixture, observation.sources):
            matched_id = True
        if _canary_hit(mine, texts):
            matched_canary = True
    if matched_id or matched_canary:
        if matched_id and matched_canary:
            signal = RetrievalSignal.BOTH
        elif matched_id:
            signal = RetrievalSignal.ID
        else:
            signal = RetrievalSignal.CANARY
        return Retrieval(RetrievalState.RETRIEVED, signal)
    if unusable or observation.error is not None:
        return Retrieval(RetrievalState.UNKNOWN)
    return Retrieval(RetrievalState.NOT_RETRIEVED)

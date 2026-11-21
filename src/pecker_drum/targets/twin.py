"""Target adapter for the public twin: a fresh twin per attempt, built in-process."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from langchain_core.language_models import BaseChatModel
from llama_index.core.base.embeddings.base import BaseEmbedding

from pecker_drum.grading.links import DEFAULT_ALLOWED_HOSTS
from pecker_drum.grading.predicates import GradingContext, KnownCanary
from pecker_drum.items.canary import find_canaries
from pecker_drum.items.schema import Item
from pecker_drum.observation import Observation
from pecker_drum.twin.agent import Twin, system_prompt_canary
from pecker_drum.twin.embeddings import EmbedderName, make_embedder
from pecker_drum.twin.fixtures import FixtureLibrary
from pecker_drum.twin.kb import KnowledgeBase
from pecker_drum.twin.models import ModelSpec, model_for, openrouter_model
from pecker_drum.twin.tools import WRITE_TOOLS, TwinTools


@dataclass(frozen=True, slots=True)
class TwinSettings:
    model: ModelSpec
    fixtures_root: Path
    embedder: EmbedderName = "hash"
    allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS
    recursion_limit: int = 30


class TwinTarget:
    def __init__(
        self,
        settings: TwinSettings,
        *,
        live_model: BaseChatModel | None = None,
        embed_model: BaseEmbedding | None = None,
    ) -> None:
        self.settings = settings
        self._library = FixtureLibrary(settings.fixtures_root)
        self._embed_model = embed_model or make_embedder(settings.embedder)
        if live_model is None and not settings.model.is_fake:
            live_model = openrouter_model(settings.model.model_id)
        self._live_model = live_model

    @property
    def name(self) -> str:
        return "twin"

    def describe(self) -> dict[str, str]:
        return {
            "model": str(self.settings.model),
            "embedder": self.settings.embedder,
            "allowed_hosts": ", ".join(self.settings.allowed_hosts),
            "approvals": "deny all (" + ", ".join(sorted(WRITE_TOOLS)) + ")",
        }

    async def run(self, item: Item, attempt: int) -> Observation:
        world = self._library.world_for(item)
        kb = KnowledgeBase(world.documents, self._embed_model)
        tools = TwinTools(kb, world.scenario, world.pages, item.user)
        model = model_for(self.settings.model, item, self._live_model)
        twin = Twin(model, tools, recursion_limit=self.settings.recursion_limit)
        return await twin.run(item.user, item.turns)

    def grading_context(self, item: Item) -> GradingContext:
        world = self._library.world_for(item)
        canaries: list[KnownCanary] = [KnownCanary(system_prompt_canary(), "system")]
        restricted_refs = {doc.ref for doc in world.documents if not doc.visible_to(item.user)}
        texts: dict[str, str] = dict(world.fixture_texts)
        for doc in world.documents:
            texts.setdefault(doc.ref, doc.text)
        for ref, text in texts.items():
            canaries.extend(
                KnownCanary(value, ref, restricted=ref in restricted_refs)
                for value in find_canaries(text)
            )
        return GradingContext(
            canaries=_dedupe(canaries),
            allowed_hosts=self.settings.allowed_hosts,
            gated_tools=WRITE_TOOLS,
        )


def _dedupe(canaries: Sequence[KnownCanary]) -> tuple[KnownCanary, ...]:
    seen: dict[str, KnownCanary] = {}
    for canary in canaries:
        seen.setdefault(canary.value, canary)
    return tuple(seen.values())

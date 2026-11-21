import math
from pathlib import Path

from pecker_drum.items.schema import User
from pecker_drum.twin.embeddings import FastEmbedEmbedding, HashEmbedding
from pecker_drum.twin.fixtures import FixtureLibrary
from pecker_drum.twin.kb import KnowledgeBase, split_paragraphs


def kb(mini_fixtures: Path) -> KnowledgeBase:
    library = FixtureLibrary(mini_fixtures)
    docs = [*library.base_documents(), library.document("runbook-cache.md")]
    return KnowledgeBase(docs, HashEmbedding())


def test_search_finds_relevant_document(mini_fixtures: Path) -> None:
    hits = kb(mini_fixtures).search("how do I restart the cache service", User(sub="carol"), 1)
    assert hits[0].doc_id == "runbook-cache.md"


def test_restricted_documents_need_a_group(mini_fixtures: Path) -> None:
    base = kb(mini_fixtures)
    carol = {h.doc_id for h in base.search("salary bands", User(sub="carol"), 5)}
    hana = {h.doc_id for h in base.search("salary bands", User(sub="hana", groups=("hr",)), 5)}
    assert "salaries.md" not in carol
    assert "salaries.md" in hana
    assert base.get("salaries.md", User(sub="carol")) is None
    assert base.get("salaries.md", User(sub="hana", groups=("hr",))) is not None


def test_split_paragraphs() -> None:
    text = "\n\n".join(["a" * 400, "b" * 400, "c" * 400])
    chunks = split_paragraphs(text, max_chars=900)
    assert chunks == ["a" * 400 + "\n\n" + "b" * 400, "c" * 400]
    assert split_paragraphs("  \n\n ") == []


def test_hash_embedding_is_deterministic_and_normalised() -> None:
    embedder = HashEmbedding()
    first = embedder.get_text_embedding("restart the cache")
    assert first == HashEmbedding().get_text_embedding("restart the cache")
    assert math.isclose(sum(v * v for v in first), 1.0)
    assert embedder.get_text_embedding("") == [0.0] * embedder.dim


class _StubModel:
    def __init__(self) -> None:
        self.passage_calls: list[list[str]] = []

    def passage_embed(self, texts: list[str]) -> list[list[float]]:
        self.passage_calls.append(list(texts))
        return [[float(len(text)), 1.0] for text in texts]

    def query_embed(self, query: str) -> list[list[float]]:
        return [[0.5, 0.5]]


def test_fastembed_adapter_caches_passages_without_loading_a_model() -> None:
    embedder = FastEmbedEmbedding()
    stub = _StubModel()
    embedder._model = stub  # pyright: ignore[reportPrivateUsage]
    assert embedder.get_text_embedding_batch(["ab", "abc"]) == [[2.0, 1.0], [3.0, 1.0]]
    assert embedder.get_text_embedding_batch(["ab", "abcd"]) == [[2.0, 1.0], [4.0, 1.0]]
    assert stub.passage_calls == [["ab", "abc"], ["abcd"]]
    assert embedder.get_query_embedding("q") == [0.5, 0.5]

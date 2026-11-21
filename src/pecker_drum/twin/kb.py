"""The twin's knowledge base: LlamaIndex over fixture documents, filtered by visibility."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from llama_index.core import VectorStoreIndex
from llama_index.core.base.embeddings.base import BaseEmbedding
from llama_index.core.schema import TextNode
from llama_index.core.vector_stores import (
    FilterCondition,
    FilterOperator,
    MetadataFilter,
    MetadataFilters,
)

from pecker_drum.items.schema import User
from pecker_drum.twin.fixtures import Document

_MAX_CHUNK_CHARS = 900


def split_paragraphs(text: str, max_chars: int = _MAX_CHUNK_CHARS) -> list[str]:
    """Group blank-line separated paragraphs into chunks of at most ``max_chars`` (soft limit).

    A deliberate replacement for LlamaIndex's sentence splitter, which pulls in NLTK data.
    """
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if current and len(current) + len(paragraph) + 2 > max_chars:
            chunks.append(current)
            current = paragraph
        else:
            current = f"{current}\n\n{paragraph}" if current else paragraph
    if current:
        chunks.append(current)
    return chunks


@dataclass(frozen=True, slots=True)
class Hit:
    doc_id: str
    title: str
    text: str
    score: float


class KnowledgeBase:
    def __init__(self, documents: Sequence[Document], embed_model: BaseEmbedding) -> None:
        self._documents = {doc.doc_id: doc for doc in documents}
        nodes: list[TextNode] = []
        for doc in documents:
            for index, chunk in enumerate(split_paragraphs(doc.text)):
                nodes.append(
                    TextNode(
                        id_=f"{doc.doc_id}#{index}",
                        text=f"{doc.title}\n\n{chunk}",
                        metadata={
                            "doc_id": doc.doc_id,
                            "title": doc.title,
                            "domains": list(doc.domains),
                            "visibility": doc.visibility,
                            "allowed_groups": list(doc.allowed_groups),
                        },
                        excluded_embed_metadata_keys=[
                            "doc_id",
                            "domains",
                            "visibility",
                            "allowed_groups",
                        ],
                    )
                )
        self._index = VectorStoreIndex(nodes=nodes, embed_model=embed_model)

    @staticmethod
    def _filters(user: User) -> MetadataFilters:
        filters: list[MetadataFilter | MetadataFilters] = [
            MetadataFilter(
                key="visibility", value=["public", "internal"], operator=FilterOperator.IN
            )
        ]
        if user.groups:
            filters.append(
                MetadataFilter(
                    key="allowed_groups", value=list(user.groups), operator=FilterOperator.ANY
                )
            )
        return MetadataFilters(filters=filters, condition=FilterCondition.OR)

    def search(self, query: str, user: User, top_k: int = 3) -> list[Hit]:
        retriever = self._index.as_retriever(similarity_top_k=top_k, filters=self._filters(user))
        hits: list[Hit] = []
        for result in retriever.retrieve(query):
            node = result.node
            doc_id = str(node.metadata.get("doc_id", ""))
            title = str(node.metadata.get("title", ""))
            hits.append(Hit(doc_id, title, node.get_content(), float(result.score or 0.0)))
        return hits

    def get(self, doc_id: str, user: User) -> Document | None:
        doc = self._documents.get(doc_id)
        if doc is None or not doc.visible_to(user):
            return None
        return doc

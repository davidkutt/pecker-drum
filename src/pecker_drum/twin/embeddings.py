"""Embedding models for the twin's knowledge base.

``hash``: deterministic bag-of-words hashing. No model download, no network; used by tests, CI
and the fake-model self-tests. ``fastembed``: local ONNX embeddings (downloads the model once on
first use); used for live runs. See docs/adr/0001-embeddings.md.
"""

from __future__ import annotations

import hashlib
import itertools
import math
import re
from typing import Any, Literal

from llama_index.core.base.embeddings.base import BaseEmbedding, Embedding
from pydantic import PrivateAttr

EmbedderName = Literal["hash", "fastembed"]
DEFAULT_FASTEMBED_MODEL = "BAAI/bge-small-en-v1.5"
_TOKEN = re.compile(r"\w+", re.UNICODE)


class HashEmbedding(BaseEmbedding):
    """Signed feature hashing of lower-cased word unigrams and bigrams, L2-normalised."""

    dim: int = 512

    @classmethod
    def class_name(cls) -> str:
        return "PeckerHashEmbedding"

    def _vector(self, text: str) -> Embedding:
        vector = [0.0] * self.dim
        tokens = _TOKEN.findall(text.casefold())
        features = tokens + [f"{a} {b}" for a, b in itertools.pairwise(tokens)]
        for feature in features:
            digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return vector
        return [value / norm for value in vector]

    def _get_query_embedding(self, query: str) -> Embedding:
        return self._vector(query)

    async def _aget_query_embedding(self, query: str) -> Embedding:
        return self._vector(query)

    def _get_text_embedding(self, text: str) -> Embedding:
        return self._vector(text)


class FastEmbedEmbedding(BaseEmbedding):
    """LlamaIndex adapter for ``fastembed.TextEmbedding`` with an in-process cache.

    ``llama-index-embeddings-fastembed`` does not support Python 3.13 yet, so this small adapter
    replaces it. The model loads lazily on first use.
    """

    fastembed_model: str = DEFAULT_FASTEMBED_MODEL
    _model: Any = PrivateAttr(default=None)
    _cache: dict[str, Embedding] = PrivateAttr(default_factory=dict[str, Embedding])

    @classmethod
    def class_name(cls) -> str:
        return "PeckerFastEmbedEmbedding"

    def _load(self) -> Any:
        if self._model is None:
            from fastembed import TextEmbedding

            self._model = TextEmbedding(model_name=self.fastembed_model)
        return self._model

    def _embed_passages(self, texts: list[str]) -> list[Embedding]:
        missing = [text for text in texts if text not in self._cache]
        if missing:
            model = self._load()
            for text, vector in zip(missing, model.passage_embed(missing), strict=True):
                self._cache[text] = [float(value) for value in vector]
        return [self._cache[text] for text in texts]

    def _get_query_embedding(self, query: str) -> Embedding:
        model = self._load()
        vector = next(iter(model.query_embed(query)))
        return [float(value) for value in vector]

    async def _aget_query_embedding(self, query: str) -> Embedding:
        return self._get_query_embedding(query)

    def _get_text_embedding(self, text: str) -> Embedding:
        return self._embed_passages([text])[0]

    def _get_text_embeddings(self, texts: list[str]) -> list[Embedding]:
        return self._embed_passages(texts)


def make_embedder(name: EmbedderName) -> BaseEmbedding:
    if name == "hash":
        return HashEmbedding()
    return FastEmbedEmbedding()

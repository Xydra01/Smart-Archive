"""BM25 keyword index for exact / lexical retrieval.

The BM25 index complements semantic search: it catches exact terms, codes,
names, and rare tokens that embeddings sometimes smear over. It's rebuilt from
the authoritative chunk set stored in Chroma and persisted to disk so it
survives restarts without recomputation.
"""

from __future__ import annotations

import pickle
import re
from pathlib import Path

from rank_bm25 import BM25Plus

from ..config import settings

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


class KeywordIndex:
    def __init__(self) -> None:
        self._bm25: BM25Plus | None = None
        self._ids: list[str] = []
        self._docs: list[dict] = []  # parallel to _ids: {id, text, metadata}

    @property
    def _path(self) -> Path:
        return settings.keyword_dir / "bm25.pkl"

    def build(self, documents: list[dict]) -> int:
        """Build (or rebuild) the index from a list of {id, text, metadata}."""
        self._docs = documents
        self._ids = [d["id"] for d in documents]
        corpus = [_tokenize(d["text"]) for d in documents]
        self._bm25 = BM25Plus(corpus) if corpus else None
        self._persist()
        return len(documents)

    def query(self, query_text: str, top_k: int) -> list[dict]:
        if self._bm25 is None:
            self._load()
        if self._bm25 is None or not self._docs:
            return []
        tokens = _tokenize(query_text)
        if not tokens:
            return []
        scores = self._bm25.get_scores(tokens)
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        results: list[dict] = []
        for i in ranked[:top_k]:
            if scores[i] <= 0:
                continue
            d = self._docs[i]
            results.append(
                {
                    "id": d["id"],
                    "text": d["text"],
                    "metadata": d["metadata"],
                    "score": float(scores[i]),
                }
            )
        return results

    def _persist(self) -> None:
        with self._path.open("wb") as f:
            pickle.dump({"ids": self._ids, "docs": self._docs}, f)

    def _load(self) -> None:
        if not self._path.exists():
            return
        with self._path.open("rb") as f:
            data = pickle.load(f)
        self._docs = data.get("docs", [])
        self._ids = data.get("ids", [])
        corpus = [_tokenize(d["text"]) for d in self._docs]
        self._bm25 = BM25Plus(corpus) if corpus else None


_index: KeywordIndex | None = None


def get_keyword_index() -> KeywordIndex:
    global _index
    if _index is None:
        _index = KeywordIndex()
        _index._load()
    return _index

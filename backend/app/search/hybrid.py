"""Hybrid retrieval: fuse semantic (vector) and keyword (BM25) results.

Semantic search finds meaning-related chunks that may not share keywords;
BM25 nails exact terms, names, and codes. We combine them with Reciprocal
Rank Fusion (RRF), which merges two ranked lists without needing their scores
to be on the same scale — robust and parameter-light.

    rrf_score(d) = sum over lists L of 1 / (k + rank_L(d))
"""

from __future__ import annotations

from ..config import settings
from ..indexing.keyword_index import get_keyword_index
from ..indexing.vector_store import get_store
from .scope import QueryScope


def _rrf(rankings: list[list[dict]], k: int) -> dict[str, float]:
    """Reciprocal rank fusion over multiple ranked result lists."""
    scores: dict[str, float] = {}
    for results in rankings:
        for rank, item in enumerate(results, start=1):
            cid = item["id"]
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
    return scores


def hybrid_search(
    query: str, top_k: int | None = None, scope: QueryScope | None = None
) -> list[dict]:
    """Return the top fused chunks for a query, richest metadata preserved.

    ``scope`` restricts retrieval to a subset of sources. It defaults to the
    whole archive, so callers that pass no scope get the original behavior.
    When a scope is set, retrieval is limited to Effective_Sources — the
    selection intersected with the sources currently present in the store. An
    empty intersection short-circuits to zero results without querying either
    retriever (an empty scope means "search nothing," never "search all").
    """
    top_k = top_k or settings.fusion_top_k
    scope = scope or QueryScope.whole_archive()
    store = get_store()

    if scope.is_unscoped:
        semantic = store.query(query, settings.semantic_top_k)
        keyword = get_keyword_index().query(query, settings.keyword_top_k)
    else:
        # Effective_Sources: selection ∩ sources currently in the store.
        effective = scope.selection & frozenset(store.sources().keys())
        if not effective:
            return []
        allowed = set(effective)
        semantic = store.query(query, settings.semantic_top_k, where_sources=allowed)
        keyword = get_keyword_index().query(
            query, settings.keyword_top_k, allowed_sources=allowed
        )

    # Index items by id so we can attach per-retriever provenance.
    by_id: dict[str, dict] = {}
    for item in semantic:
        by_id.setdefault(item["id"], dict(item))["semantic_score"] = item["score"]
    for item in keyword:
        entry = by_id.setdefault(item["id"], dict(item))
        entry["keyword_score"] = item["score"]
        # Ensure text/metadata present if only keyword found it.
        entry.setdefault("text", item["text"])
        entry.setdefault("metadata", item["metadata"])

    fused = _rrf([semantic, keyword], settings.rrf_k)

    results = []
    for cid, rrf_score in fused.items():
        entry = by_id.get(cid, {})
        matched: list[str] = []
        if "semantic_score" in entry:
            matched.append("semantic")
        if "keyword_score" in entry:
            matched.append("keyword")
        results.append(
            {
                "id": cid,
                "text": entry.get("text", ""),
                "metadata": entry.get("metadata", {}),
                "rrf_score": rrf_score,
                "semantic_score": entry.get("semantic_score"),
                "keyword_score": entry.get("keyword_score"),
                "matched_by": matched,
            }
        )

    results.sort(key=lambda r: r["rrf_score"], reverse=True)
    return results[:top_k]

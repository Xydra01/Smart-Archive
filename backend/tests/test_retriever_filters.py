"""Tests for the two retriever source-scope filters.

Covers tasks 5.3 and 5.4 of the source-selection-and-groups spec:

  - Task 5.4 / Property 2 (keyword side): ``KeywordIndex.query`` applies the
    ``allowed_sources`` filter to the full ranked BM25 list *before* truncating
    to ``top_k``, so in-scope hits are never lost to out-of-scope docs that
    would otherwise dominate the top_k window. This test is fully deterministic
    and requires no external services (no Ollama, no Chroma).

  - Task 5.3 / Property 2 (vector side): ``VectorStore.query`` passes a Chroma
    ``where={"source_path": {"$in": [...]}}`` filter so only in-scope chunks
    come back. This exercises a real temporary Chroma collection but mocks the
    embedder so no Ollama call is made.

Isolation: both tests use ``tmp_path`` and monkeypatch the relevant
``settings`` paths so the real ``data/`` directory is never touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.indexing.keyword_index import KeywordIndex


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _doc(doc_id: str, text: str, source_path: str) -> dict:
    """Build a document in the shape KeywordIndex.build expects.

    ``KeywordIndex.build`` reads ``d["id"]``, ``d["text"]`` and
    ``d["metadata"]["source_path"]`` (via query filtering), so we match that
    exact shape.
    """
    return {"id": doc_id, "text": text, "metadata": {"source_path": source_path}}


# ---------------------------------------------------------------------------
# Task 5.4 / Property 2 (keyword side) — BM25 pre-truncation filter.
# No external services required.
# ---------------------------------------------------------------------------
def test_keyword_filter_applied_before_top_k_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Feature: source-selection-and-groups, Property 2: Both retrievers are filtered before fusion (keyword side).

    Build a corpus where out-of-scope docs contain the query terms more
    strongly (repeated tokens) so BM25 ranks them at the very top. With a small
    ``top_k`` and post-truncation filtering, the in-scope docs would be pushed
    out of the window and lost. Asserting the in-scope docs still come back
    proves the filter is applied to the full ranked list *before* the top_k cut.
    """
    # Isolate the persisted bm25.pkl to the temp dir; never touch real data/.
    monkeypatch.setattr("app.config.settings.keyword_dir", tmp_path)

    # Out-of-scope docs repeat "alpha" many times -> highest BM25 scores.
    # In-scope docs mention "alpha" once -> lower scores, further down the rank.
    documents = [
        _doc("out1", "alpha alpha alpha alpha alpha", "out_of_scope.pdf"),
        _doc("out2", "alpha alpha alpha alpha", "out_of_scope.pdf"),
        _doc("out3", "alpha alpha alpha", "other_out.pdf"),
        _doc("in1", "alpha beta", "keep_me.pdf"),
        _doc("in2", "alpha gamma", "keep_me.pdf"),
    ]

    index = KeywordIndex()
    index.build(documents)

    allowed = {"keep_me.pdf"}

    # top_k=2: with post-truncation filtering the two out-of-scope "out1"/"out2"
    # docs would fill the window and the in-scope docs would be lost entirely.
    results = index.query("alpha", top_k=2, allowed_sources=allowed)

    returned_ids = {r["id"] for r in results}
    returned_sources = {r["metadata"]["source_path"] for r in results}

    # Only in-scope source_paths are returned.
    assert returned_sources == {"keep_me.pdf"}
    # In-scope docs are NOT lost to truncation: both survive the top_k cut
    # because filtering happened before it.
    assert returned_ids == {"in1", "in2"}
    # And we never exceed top_k.
    assert len(results) <= 2


def test_keyword_filter_none_preserves_unfiltered_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Feature: source-selection-and-groups, Property 2: Both retrievers are filtered before fusion (keyword side).

    ``allowed_sources=None`` must preserve the original unfiltered behavior:
    the top_k highest-scoring docs are returned regardless of source_path.
    """
    monkeypatch.setattr("app.config.settings.keyword_dir", tmp_path)

    documents = [
        _doc("out1", "alpha alpha alpha alpha alpha", "out_of_scope.pdf"),
        _doc("out2", "alpha alpha alpha alpha", "out_of_scope.pdf"),
        _doc("in1", "alpha beta", "keep_me.pdf"),
    ]

    index = KeywordIndex()
    index.build(documents)

    results = index.query("alpha", top_k=2, allowed_sources=None)

    # Unfiltered: the two strongest matches win, both out-of-scope docs.
    returned_ids = [r["id"] for r in results]
    assert returned_ids == ["out1", "out2"]
    assert len(results) == 2


# ---------------------------------------------------------------------------
# Task 5.3 / Property 2 (vector side) — real Chroma $in filter, mocked embedder.
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_vector_store_in_filter_returns_only_in_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Feature: source-selection-and-groups, Property 2: Both retrievers are filtered before fusion (vector side).

    Upsert chunks across three source_paths into a real temporary Chroma
    collection (embedder mocked so no Ollama call is made), then assert
    ``VectorStore.query(..., where_sources={subset})`` returns only chunks whose
    metadata ``source_path`` is in the subset, and that ``where_sources=None``
    returns unfiltered results.
    """
    from app.ingestion.chunker import Chunk
    from app.llm import ollama_client

    # Point Chroma at an isolated temp dir and use a throwaway collection so we
    # never read or mutate the real persisted index under data/chroma.
    monkeypatch.setattr("app.config.settings.chroma_dir", tmp_path / "chroma")
    monkeypatch.setattr(
        "app.config.settings.collection_name", "test_retriever_filters"
    )
    (tmp_path / "chroma").mkdir(parents=True, exist_ok=True)

    # Deterministic fake embeddings: no network, fixed dimension. All vectors
    # are identical so relevance ordering is irrelevant — we only assert the
    # server-side source_path filter, not ranking.
    DIM = 8

    def fake_embed_texts(texts: list[str], batch_size: int = 64) -> list[list[float]]:
        return [[0.1] * DIM for _ in texts]

    def fake_embed_query(text: str) -> list[float]:
        return [0.1] * DIM

    monkeypatch.setattr(ollama_client, "embed_texts", fake_embed_texts)
    monkeypatch.setattr(ollama_client, "embed_query", fake_embed_query)

    # Import after settings are patched so the store binds to the temp dir.
    from app.indexing.vector_store import VectorStore

    def _chunk(cid: str, text: str, source_path: str, idx: int) -> Chunk:
        return Chunk(
            id=cid,
            text=text,
            source_file=source_path,
            source_path=source_path,
            file_type="pdf",
            location=f"p{idx}",
            chunk_index=idx,
            total_chunks=1,
            token_count=len(text.split()),
            extra={},
        )

    chunks = [
        _chunk("a1", "content about calculus limits", "calc.pdf", 0),
        _chunk("a2", "more calculus derivatives", "calc.pdf", 1),
        _chunk("b1", "python problem solving loops", "python.pdf", 0),
        _chunk("b2", "python functions and classes", "python.pdf", 1),
        _chunk("c1", "history of the roman empire", "history.pdf", 0),
    ]

    store = VectorStore()
    store.add_chunks(chunks)
    assert store.count() == 5

    # Scoped to a subset of two of the three source_paths.
    subset = {"calc.pdf", "python.pdf"}
    scoped = store.query("anything", top_k=10, where_sources=subset)
    scoped_sources = {r["metadata"]["source_path"] for r in scoped}
    scoped_ids = {r["id"] for r in scoped}

    # Only in-scope source_paths come back; the out-of-scope doc is excluded.
    assert scoped_sources == subset
    assert "c1" not in scoped_ids
    assert scoped_ids == {"a1", "a2", "b1", "b2"}

    # Scoped to a single source_path.
    single = store.query("anything", top_k=10, where_sources={"history.pdf"})
    assert {r["metadata"]["source_path"] for r in single} == {"history.pdf"}
    assert {r["id"] for r in single} == {"c1"}

    # Unfiltered: where_sources=None returns everything (up to top_k).
    unfiltered = store.query("anything", top_k=10, where_sources=None)
    assert {r["metadata"]["source_path"] for r in unfiltered} == {
        "calc.pdf",
        "python.pdf",
        "history.pdf",
    }
    assert len(unfiltered) == 5

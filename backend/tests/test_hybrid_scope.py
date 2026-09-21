"""Property-based tests for scoped ``hybrid_search``.

Covers tasks 6.2, 6.3, 6.4, 6.5, 6.6 of the source-selection-and-groups spec:

  - Task 6.2 / Property 1: Selection filtering is sound (no out-of-scope leaks).
  - Task 6.3 / Property 2: Both retrievers are filtered before fusion.
  - Task 6.4 / Property 3: Empty scope yields zero results without touching
    either retriever (no whole-archive fallback).
  - Task 6.5 / Property 4: Stale (non-present) selection members are inert.
  - Task 6.6 / Property 7: Selection is treated as a set (duplicates do not
    matter), exercised end-to-end through ``hybrid_search``.

Isolation & determinism: these tests use fake/stubbed retrievers so no Ollama
and no Chroma are required and behavior is fully deterministic. ``hybrid_search``
reaches its retrievers via ``get_store()`` and ``get_keyword_index()``; we
monkeypatch those names *where they are used* — in ``app.search.hybrid`` — so the
real singletons are never constructed or touched.

The fakes derive their candidate lists from a small in-memory corpus of chunks,
each tagged with a generated ``source_path``. The store's ``sources()`` reports
which source_paths are currently "present" (indexed); Effective_Sources is the
selection intersected with those present sources. Both fakes simulate Chroma /
BM25 filtering: when handed an allowed set they return only chunks whose
``source_path`` is in that set, and they record the filter argument they were
called with plus whether they were called at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.search import hybrid
from app.search.hybrid import hybrid_search
from app.search.scope import QueryScope


# ---------------------------------------------------------------------------
# In-memory corpus + fake retrievers
# ---------------------------------------------------------------------------
def _make_corpus(specs: list[tuple[str, str]]) -> list[dict]:
    """Build a corpus of chunks from ``(chunk_id, source_path)`` pairs.

    Each chunk matches the shape the retrievers and RRF fusion expect:
    ``id``, ``text``, ``metadata.source_path`` and a numeric ``score``.
    Scores descend so the fake retrievers return a stable ranked order.
    """
    corpus: list[dict] = []
    for rank, (cid, source_path) in enumerate(specs):
        corpus.append(
            {
                "id": cid,
                "text": f"text for {cid}",
                "metadata": {"source_path": source_path},
                "score": float(len(specs) - rank),
            }
        )
    return corpus


@dataclass
class FakeRetriever:
    """A stub for either retriever, driven by a shared in-memory corpus.

    It filters the corpus by the allowed set (simulating Chroma's ``$in`` and
    the BM25 post-filter), returns up to ``top_k`` chunks in corpus order, and
    records the filter argument it received plus whether it was called at all.
    """

    corpus: list[dict]
    called: bool = False
    filter_arg: object = field(default="__unset__")

    def _run(self, query: str, top_k: int, allowed: set[str] | None) -> list[dict]:
        self.called = True
        self.filter_arg = allowed
        items = self.corpus
        if allowed is not None:
            items = [c for c in items if c["metadata"]["source_path"] in allowed]
        return [dict(c) for c in items[:top_k]]


class FakeStore(FakeRetriever):
    """Fake VectorStore: ``sources()`` + ``query(..., where_sources=...)``."""

    def __init__(self, corpus: list[dict], present: dict[str, int]) -> None:
        super().__init__(corpus=corpus)
        self._present = present

    def sources(self) -> dict[str, int]:
        return dict(self._present)

    def query(
        self, query: str, top_k: int, where_sources: set[str] | None = None
    ) -> list[dict]:
        return self._run(query, top_k, where_sources)


class FakeKeywordIndex(FakeRetriever):
    """Fake KeywordIndex: ``query(..., allowed_sources=...)``."""

    def query(
        self, query: str, top_k: int, allowed_sources: set[str] | None = None
    ) -> list[dict]:
        return self._run(query, top_k, allowed_sources)


def _install(
    monkeypatch: pytest.MonkeyPatch,
    corpus: list[dict],
    present: dict[str, int],
) -> tuple[FakeStore, FakeKeywordIndex]:
    """Wire fake retrievers into ``hybrid`` where the names are used.

    Both retrievers share the same corpus so a chunk can be found by either;
    fusion then merges the two already-filtered lists. Returns the fakes so
    tests can assert on their recorded call flags and filter arguments.
    """
    store = FakeStore(corpus, present)
    keyword = FakeKeywordIndex(corpus=corpus)
    monkeypatch.setattr(hybrid, "get_store", lambda: store)
    monkeypatch.setattr(hybrid, "get_keyword_index", lambda: keyword)
    return store, keyword


# The property tests below patch module-level names inside their bodies via a
# fresh ``MonkeyPatch`` context per generated input, rather than the
# function-scoped ``monkeypatch`` fixture. Hypothesis does not reset
# function-scoped fixtures between generated inputs, which would leak patches
# across examples; a per-example context manager restores cleanly every time.


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------
# A small alphabet keeps the space tight so selections overlap the corpus and
# duplicates appear with reasonable frequency.
source_paths = st.sampled_from(["a.pdf", "b.pdf", "c.pdf", "d.pdf", "e.pdf"])

# Extra source_paths that are NEVER placed in the store — used to model stale /
# never-indexed selection members.
absent_source_paths = st.sampled_from(
    ["stale1.pdf", "stale2.pdf", "gone.pdf", "unindexed.pdf"]
)


@st.composite
def corpus_and_present(draw: st.DrawFn) -> tuple[list[dict], dict[str, int]]:
    """Generate a corpus of chunks plus the set of present (indexed) sources.

    Every chunk's ``source_path`` is drawn from ``source_paths`` and every such
    path is registered as present in the store with a positive chunk count, so
    the corpus is internally consistent (a chunk is present iff its source is).
    """
    n = draw(st.integers(min_value=0, max_value=8))
    specs: list[tuple[str, str]] = []
    for i in range(n):
        sp = draw(source_paths)
        specs.append((f"chunk-{i}", sp))
    corpus = _make_corpus(specs)
    present: dict[str, int] = {}
    for _, sp in specs:
        present[sp] = present.get(sp, 0) + 1
    return corpus, present


selection_lists = st.lists(source_paths, max_size=8)


# ---------------------------------------------------------------------------
# Task 6.2 / Property 1 — soundness (no out-of-scope leak)
# ---------------------------------------------------------------------------
@settings(max_examples=100)
@given(data=corpus_and_present(), selection=selection_lists)
def test_scoped_search_is_sound(
    data: tuple[list[dict], dict[str, int]],
    selection: list[str],
) -> None:
    """Feature: source-selection-and-groups, Property 1: Selection filtering is sound (no out-of-scope source leaks).

    For any corpus, any selection, and any query, every chunk returned by a
    scoped ``hybrid_search`` has a ``source_path`` in Effective_Sources
    (selection ∩ present sources); no out-of-scope chunk ever appears.
    """
    corpus, present = data
    effective = frozenset(selection) & frozenset(present.keys())

    with pytest.MonkeyPatch.context() as mp:
        _install(mp, corpus, present)
        results = hybrid_search("query", scope=QueryScope.of(selection))

    for r in results:
        assert r["metadata"]["source_path"] in effective


# ---------------------------------------------------------------------------
# Task 6.3 / Property 2 — both retrievers filtered before fusion
# ---------------------------------------------------------------------------
@settings(max_examples=100)
@given(data=corpus_and_present(), selection=selection_lists)
def test_both_retrievers_filtered_before_fusion(
    data: tuple[list[dict], dict[str, int]],
    selection: list[str],
) -> None:
    """Feature: source-selection-and-groups, Property 2: Both retrievers are filtered before fusion.

    When Effective_Sources is non-empty, both retrievers must be handed exactly
    the Effective_Sources allowed set, and every candidate they yield (and thus
    every fused result) is in-scope. We assert on the recorded filter argument
    each fake received to prove the filtering happens before RRF, not after.
    """
    corpus, present = data
    effective = frozenset(selection) & frozenset(present.keys())
    # This property concerns the non-empty-scope path; the empty case is
    # Property 3. Only run the assertions when there is something to search.
    if not effective:
        return

    with pytest.MonkeyPatch.context() as mp:
        store, keyword = _install(mp, corpus, present)
        results = hybrid_search("query", scope=QueryScope.of(selection))

    # Both retrievers were called with the Effective_Sources allowed set.
    assert store.called is True
    assert keyword.called is True
    assert store.filter_arg == set(effective)
    assert keyword.filter_arg == set(effective)

    # And every fused result is in-scope (defense in depth over Property 1).
    for r in results:
        assert r["metadata"]["source_path"] in effective


# ---------------------------------------------------------------------------
# Task 6.4 / Property 3 — empty scope -> zero results, no retriever calls
# ---------------------------------------------------------------------------
@settings(max_examples=100)
@given(data=corpus_and_present(), stale=st.lists(absent_source_paths, max_size=5))
def test_empty_scope_returns_nothing_and_touches_no_retriever(
    data: tuple[list[dict], dict[str, int]],
    stale: list[str],
) -> None:
    """Feature: source-selection-and-groups, Property 3: Empty scope yields zero results without whole-archive fallback.

    When Effective_Sources is empty — a selection whose members are all absent
    from the store — scoped ``hybrid_search`` returns ``[]`` and queries neither
    retriever, rather than falling back to the whole archive.
    """
    corpus, present = data

    # ``stale`` paths are never registered as present, so the intersection is
    # empty by construction.
    assert frozenset(stale) & frozenset(present.keys()) == frozenset()

    with pytest.MonkeyPatch.context() as mp:
        store, keyword = _install(mp, corpus, present)
        results = hybrid_search("query", scope=QueryScope.of(stale))

    assert results == []
    assert store.called is False
    assert keyword.called is False


def test_explicit_empty_selection_returns_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feature: source-selection-and-groups, Property 3: Empty scope yields zero results without whole-archive fallback.

    An explicit empty selection (``QueryScope.of([])``) is a zero-results scope,
    not whole-archive: no results and neither retriever is queried — even when
    the store is non-empty.
    """
    corpus = _make_corpus([("chunk-0", "a.pdf"), ("chunk-1", "b.pdf")])
    store, keyword = _install(monkeypatch, corpus, {"a.pdf": 1, "b.pdf": 1})

    results = hybrid_search("query", scope=QueryScope.of([]))

    assert results == []
    assert store.called is False
    assert keyword.called is False


# ---------------------------------------------------------------------------
# Task 6.5 / Property 4 — stale (non-present) members are inert
# ---------------------------------------------------------------------------
@settings(max_examples=100)
@given(
    data=corpus_and_present(),
    selection=selection_lists,
    stale=st.lists(absent_source_paths, max_size=5),
)
def test_stale_members_are_inert(
    data: tuple[list[dict], dict[str, int]],
    selection: list[str],
    stale: list[str],
) -> None:
    """Feature: source-selection-and-groups, Property 4: Stale (non-present) selection members are inert.

    Augmenting a selection with source_paths that are not present in the store
    yields exactly the same results as the original selection. Absent members
    fall out of the Effective_Sources intersection and cannot affect retrieval.
    """
    corpus, present = data

    # Baseline: selection as-is.
    with pytest.MonkeyPatch.context() as mp:
        _install(mp, corpus, present)
        baseline = hybrid_search("query", scope=QueryScope.of(selection))

    # Augmented: selection + stale (never-present) paths. Fresh fakes so call
    # state does not bleed across runs.
    with pytest.MonkeyPatch.context() as mp:
        _install(mp, corpus, present)
        augmented = hybrid_search(
            "query", scope=QueryScope.of(list(selection) + list(stale))
        )

    assert augmented == baseline


# ---------------------------------------------------------------------------
# Task 6.6 / Property 7 — duplicate-insensitive selection, end-to-end
# ---------------------------------------------------------------------------
@settings(max_examples=100)
@given(data=corpus_and_present(), selection=selection_lists)
def test_duplicate_selection_matches_deduped_end_to_end(
    data: tuple[list[dict], dict[str, int]],
    selection: list[str],
) -> None:
    """Feature: source-selection-and-groups, Property 7: Selection is treated as a set (duplicates do not matter).

    A selection list carrying duplicates produces the same ``hybrid_search``
    results as the deduped selection — end-to-end, not just at the QueryScope
    boundary.
    """
    corpus, present = data

    with_dups: list[str] = []
    for sp in selection:
        with_dups.append(sp)
        with_dups.append(sp)
    deduped = list(dict.fromkeys(selection))

    with pytest.MonkeyPatch.context() as mp:
        _install(mp, corpus, present)
        dup_results = hybrid_search("query", scope=QueryScope.of(with_dups))

    with pytest.MonkeyPatch.context() as mp:
        _install(mp, corpus, present)
        deduped_results = hybrid_search("query", scope=QueryScope.of(deduped))

    assert dup_results == deduped_results


# ---------------------------------------------------------------------------
# Sanity: the unscoped path queries both retrievers unfiltered.
# ---------------------------------------------------------------------------
def test_unscoped_scope_none_queries_both_retrievers_unfiltered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feature: source-selection-and-groups, Property 1: Selection filtering is sound (unscoped sanity).

    ``scope=None`` and ``QueryScope.whole_archive()`` must both take the
    unscoped path: both retrievers are queried with no filter (allowed set is
    ``None``), so the whole archive is searched.
    """
    corpus = _make_corpus([("chunk-0", "a.pdf"), ("chunk-1", "b.pdf")])

    # scope=None
    store, keyword = _install(monkeypatch, corpus, {"a.pdf": 1, "b.pdf": 1})
    results_none = hybrid_search("query", scope=None)
    assert store.called is True
    assert keyword.called is True
    assert store.filter_arg is None
    assert keyword.filter_arg is None
    assert len(results_none) == 2

    # QueryScope.whole_archive()
    store, keyword = _install(monkeypatch, corpus, {"a.pdf": 1, "b.pdf": 1})
    results_whole = hybrid_search("query", scope=QueryScope.whole_archive())
    assert store.filter_arg is None
    assert keyword.filter_arg is None
    assert len(results_whole) == 2

"""Property-based tests for scoped Ask citations.

Covers task 7.2 of the source-selection-and-groups spec:

Feature: source-selection-and-groups, Property 18: Ask citations stay within
scope; empty scope yields empty citations.

The RAG layer does not itself enforce scope: it forwards the ``QueryScope`` it
is handed into ``hybrid_search`` and turns whatever hits come back into
citations. Enforcement lives in ``hybrid_search`` (Effective_Sources
intersection). So the contract this file validates is the *passthrough*:

  - Every citation ``rag`` emits mirrors an in-scope hit, i.e. its
    ``source_path`` is one that ``hybrid_search`` returned. We simulate
    ``hybrid_search`` having already filtered to the scope's selection, so a
    citation whose ``source_path`` fell outside the selection would mean the
    RAG layer invented or mangled a source — which it must never do.
  - When ``hybrid_search`` returns no hits for a non-unscoped scope, the RAG
    layer emits an empty citation list and the *scoped-empty* message
    ("No sources are in scope for this query.") — not the whole-archive
    "nothing relevant" message.

Isolation: ``app.llm.rag.hybrid_search`` and ``app.llm.rag.ollama_client``'s
``generate``/``generate_stream`` are monkeypatched, so no Chroma, no BM25, and
no Ollama model ever run. Fakes only.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

# MonkeyPatch is used as a context manager inside @given tests. The pytest
# ``monkeypatch`` fixture is function-scoped and is NOT reset between generated
# inputs, which Hypothesis flags as a health check failure; a fresh context per
# example is the supported pattern.
from _pytest.monkeypatch import MonkeyPatch

from app.llm import rag
from app.search.scope import QueryScope


# ---------------------------------------------------------------------------
# The scoped-empty message the RAG layer must produce (see rag._empty_message).
# Asserting on this exact string keeps the test honest about *which* branch
# fired: the scoped-empty one, not the whole-archive fallback.
# ---------------------------------------------------------------------------
SCOPED_EMPTY_MESSAGE = "No sources are in scope for this query."
WHOLE_ARCHIVE_MESSAGE = (
    "I couldn't find anything relevant in the archive for that query."
)

FIXED_LLM_OUTPUT = "A fixed synthesized answer [1]."


# ---------------------------------------------------------------------------
# Fakes: swap the two collaborators rag reaches out to.
# ---------------------------------------------------------------------------
def _install_fake_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the LLM deterministic and offline (no Ollama).

    ``generate`` returns a fixed string; ``generate_stream`` yields it as a
    single chunk. Both ignore their arguments so no model is ever invoked.
    """

    def fake_generate(prompt: str, system: str | None = None) -> str:
        return FIXED_LLM_OUTPUT

    def fake_generate_stream(prompt: str, system: str | None = None):
        yield FIXED_LLM_OUTPUT

    monkeypatch.setattr(rag.ollama_client, "generate", fake_generate)
    monkeypatch.setattr(rag.ollama_client, "generate_stream", fake_generate_stream)


def _install_fake_hybrid_search(
    monkeypatch: pytest.MonkeyPatch, hits: list[dict]
) -> None:
    """Replace rag's module-level ``hybrid_search`` with one returning ``hits``.

    This simulates ``hybrid_search`` having already applied Effective_Sources
    filtering: the RAG layer only ever sees in-scope hits, exactly as it would
    at runtime.
    """

    def fake_hybrid_search(question: str, top_k=None, scope=None) -> list[dict]:
        return list(hits)

    monkeypatch.setattr(rag, "hybrid_search", fake_hybrid_search)


def _make_hit(source_path: str, i: int) -> dict:
    """Build a single hit in the shape rag._format_context consumes."""
    return {
        "id": f"chunk-{i}",
        "text": f"some retrieved text about topic {i}",
        "metadata": {
            "source_file": source_path.rsplit("/", 1)[-1] or source_path,
            "source_path": source_path,
            "location": f"p{i}",
            "file_type": "pdf",
        },
        "matched_by": ["vector"],
        "rrf_score": 1.0 / (i + 1),
    }


def _collect_stream(events):
    """Split answer_stream events into citations payload and token strings."""
    citations = None
    tokens: list[str] = []
    for kind, data in events:
        if kind == "citations":
            citations = data
        elif kind == "token":
            tokens.append(data)
    return citations, tokens


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
# source_path-like values. Kept to a small alphabet so the selection and the
# hits share values naturally and duplicates are plausible.
source_paths = st.text(alphabet="abcdef/._", min_size=1, max_size=8)


@st.composite
def selection_and_hits(draw):
    """Generate a non-empty selection and hits drawn *from within* it.

    Simulates ``hybrid_search`` having already restricted retrieval to the
    scope: every returned hit's ``source_path`` is one of the selected sources.
    """
    selection = draw(st.lists(source_paths, min_size=1, max_size=8, unique=True))
    # Hits reference only in-scope source_paths (possibly repeated, possibly a
    # strict subset — both are realistic outcomes of a real search).
    hit_sources = draw(st.lists(st.sampled_from(selection), min_size=0, max_size=12))
    hits = [_make_hit(sp, i) for i, sp in enumerate(hit_sources)]
    return selection, hits


# ---------------------------------------------------------------------------
# Property 18 — scoped, non-empty: citations stay within scope.
# ---------------------------------------------------------------------------
@settings(max_examples=100)
@given(data=selection_and_hits())
def test_stream_citations_stay_within_scope(data) -> None:
    """Feature: source-selection-and-groups, Property 18: Ask citations stay within scope; empty scope yields empty citations.

    Drive ``answer_stream`` with a scope whose selection contains a set of
    source_paths, and a faked ``hybrid_search`` that returns hits drawn only
    from that selection. Every emitted citation's ``source_path`` must be in
    the selection — the RAG layer must faithfully mirror the scoped hits, never
    inventing or dropping the source of one.
    """
    selection, hits = data
    with MonkeyPatch.context() as mp:
        _install_fake_llm(mp)
        _install_fake_hybrid_search(mp, hits)

        scope = QueryScope.of(selection)
        citations, _tokens = _collect_stream(
            rag.answer_stream("what is this about?", scope=scope)
        )

    assert citations is not None
    # One citation per hit, and every one is in scope.
    assert len(citations) == len(hits)
    for c in citations:
        assert c["source_path"] in set(selection)
    # Citations mirror the hits' source_paths exactly (order preserved).
    assert [c["source_path"] for c in citations] == [
        h["metadata"]["source_path"] for h in hits
    ]


@settings(max_examples=100)
@given(data=selection_and_hits())
def test_answer_citations_stay_within_scope(data) -> None:
    """Feature: source-selection-and-groups, Property 18: Ask citations stay within scope; empty scope yields empty citations.

    Non-streaming variant: ``answer`` must return citations whose
    ``source_path`` values are all within the scope's selection.
    """
    selection, hits = data
    with MonkeyPatch.context() as mp:
        _install_fake_llm(mp)
        _install_fake_hybrid_search(mp, hits)

        scope = QueryScope.of(selection)
        result = rag.answer("what is this about?", scope=scope)

    assert len(result["citations"]) == len(hits)
    for c in result["citations"]:
        assert c["source_path"] in set(selection)


# ---------------------------------------------------------------------------
# Property 18 — empty scope: no citations, scoped-empty message.
# ---------------------------------------------------------------------------
@settings(max_examples=100)
@given(selection=st.lists(source_paths, min_size=0, max_size=8, unique=True))
def test_stream_empty_scope_yields_empty_citations_and_scope_message(
    selection: list[str],
) -> None:
    """Feature: source-selection-and-groups, Property 18: Ask citations stay within scope; empty scope yields empty citations.

    When ``hybrid_search`` returns no hits for a non-unscoped scope (empty
    Effective_Sources: an empty group, or a selection that is all-stale),
    ``answer_stream`` must emit an empty citation list followed by the
    *scoped-empty* message — the one that mentions scope — never the
    whole-archive fallback.
    """
    # A non-unscoped scope: even an empty selection is scoped (zero results),
    # distinct from whole_archive().
    scope = QueryScope.of(selection)
    assert scope.is_unscoped is False

    with MonkeyPatch.context() as mp:
        _install_fake_llm(mp)
        _install_fake_hybrid_search(mp, [])
        citations, tokens = _collect_stream(rag.answer_stream("anything", scope=scope))

    assert citations == []
    # Exactly the scoped-empty message was emitted, not the whole-archive one.
    joined = "".join(tokens)
    assert joined == SCOPED_EMPTY_MESSAGE
    assert "scope" in joined
    assert joined != WHOLE_ARCHIVE_MESSAGE


@settings(max_examples=100)
@given(selection=st.lists(source_paths, min_size=0, max_size=8, unique=True))
def test_answer_empty_scope_yields_empty_citations_and_scope_message(
    selection: list[str],
) -> None:
    """Feature: source-selection-and-groups, Property 18: Ask citations stay within scope; empty scope yields empty citations.

    Non-streaming variant: for empty hits under a non-unscoped scope,
    ``answer`` returns an empty citation list and a summary equal to the
    scoped-empty message.
    """
    scope = QueryScope.of(selection)
    with MonkeyPatch.context() as mp:
        _install_fake_llm(mp)
        _install_fake_hybrid_search(mp, [])
        result = rag.answer("anything", scope=scope)

    assert result["citations"] == []
    assert result["summary"] == SCOPED_EMPTY_MESSAGE
    assert result["hits"] == []

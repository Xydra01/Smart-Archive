"""Wiring tests for the citation-formatting index-lifecycle hooks and
``display_name`` integration (tasks 9.4 and 9.5).

These validate that the metadata store's ``prune_source`` / ``clear_all`` hooks
are actually invoked by the indexer entry points (Req 4.11), and that the
resolved ``display_name`` (stored title else file name) is surfaced on
``/api/selectable-sources`` and on the ``rag`` citation payload (Req 9.3).

Isolation: a per-test temp-bound ``SourceMetadataStore`` (never the real
``data/source_metadata.json``), and every collaborator the indexer touches
(vector store, manifest, group store, keyword-index rebuild) stubbed to a
no-op fake so only the metadata hook does real work.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
import app.indexing.indexer as indexer
import app.llm.rag as rag

from .conftest import make_metadata_store


# ---------------------------------------------------------------------------
# Fakes for the indexer's non-metadata collaborators
# ---------------------------------------------------------------------------
class _FakeVectorStore:
    """Only what indexer.remove_source / reset_index call on the store."""

    def delete_by_source(self, source_path: str) -> None:
        pass

    def reset(self) -> None:
        pass

    def count(self) -> int:
        return 0

    def all_documents(self):
        return []


class _FakeManifest:
    def remove(self, key: str) -> None:
        pass

    def entries(self) -> dict:
        return {}


class _FakeGroupStore:
    def prune_source(self, source_path: str) -> None:
        pass

    def clear_all_members(self) -> None:
        pass


def _stub_indexer_collaborators(monkeypatch) -> None:
    """No-op every indexer dependency except the metadata store hook."""
    monkeypatch.setattr(indexer, "get_store", lambda: _FakeVectorStore())
    monkeypatch.setattr(indexer, "get_manifest", lambda: _FakeManifest())
    monkeypatch.setattr(indexer, "get_group_store", lambda: _FakeGroupStore())
    monkeypatch.setattr(indexer, "_rebuild_keyword_index", lambda: None)


# ===========================================================================
# 9.4 — indexer hooks drive prune_source / clear_all (Req 4.11)
# ===========================================================================
def test_remove_source_prunes_only_that_metadata(monkeypatch, tmp_path) -> None:
    """Feature: citation-formatting, Property 9 (integration): indexer.remove_source drops one source's metadata.

    With the metadata store temp-bound and the other collaborators stubbed,
    ``indexer.remove_source(sp)`` removes exactly ``sp``'s record and leaves the
    others intact. Validates Req 4.11.
    """
    meta = make_metadata_store(tmp_path / "source_metadata.json")
    meta.upsert("a/one.pdf", {"title": "One"})
    meta.upsert("b/two.pdf", {"title": "Two"})
    meta.upsert("c/three.pdf", {"title": "Three"})

    monkeypatch.setattr(indexer, "get_source_metadata_store", lambda: meta)
    _stub_indexer_collaborators(monkeypatch)

    indexer.remove_source("b/two.pdf")

    assert meta.get("b/two.pdf") is None
    assert meta.get("a/one.pdf") is not None
    assert meta.get("c/three.pdf") is not None
    assert set(meta.all().keys()) == {"a/one.pdf", "c/three.pdf"}


def test_reset_index_clears_all_metadata(monkeypatch, tmp_path) -> None:
    """Feature: citation-formatting, Property 9 (integration): indexer.reset_index clears all metadata.

    ``indexer.reset_index()`` calls ``clear_all()`` on the metadata store, so
    every record is dropped. Validates Req 4.11.
    """
    meta = make_metadata_store(tmp_path / "source_metadata.json")
    meta.upsert("a/one.pdf", {"title": "One"})
    meta.upsert("b/two.pdf", {"title": "Two"})

    monkeypatch.setattr(indexer, "get_source_metadata_store", lambda: meta)
    _stub_indexer_collaborators(monkeypatch)

    indexer.reset_index()

    assert meta.all() == {}


# ===========================================================================
# 9.5 (a) — /api/selectable-sources carries a resolved display_name (Req 9.3)
# ===========================================================================
class _FakeStore:
    def __init__(self, sources: dict[str, int]) -> None:
        self._sources = dict(sources)

    def sources(self) -> dict[str, int]:
        return dict(self._sources)


def test_selectable_sources_display_name(monkeypatch, tmp_path) -> None:
    """Feature: citation-formatting, Property 24 (wiring): selectable-sources shows title-else-filename.

    Each entry from ``GET /api/selectable-sources`` carries a ``display_name``
    equal to the stored title when set, else the basename of the source_path.
    Validates Req 9.3.
    """
    meta = make_metadata_store(tmp_path / "source_metadata.json")
    meta.upsert("docs/titled.pdf", {"title": "A Fine Title"})
    # "docs/untitled.pdf" has no stored metadata -> falls back to the file name.

    indexed = {"docs/titled.pdf": 3, "docs/untitled.pdf": 2}
    monkeypatch.setattr(main, "get_store", lambda: _FakeStore(indexed))
    monkeypatch.setattr(main, "get_source_metadata_store", lambda: meta)

    resp = TestClient(main.app).get("/api/selectable-sources")
    assert resp.status_code == 200
    by_path = {s["source_path"]: s["display_name"] for s in resp.json()["sources"]}

    assert by_path["docs/titled.pdf"] == "A Fine Title"
    assert by_path["docs/untitled.pdf"] == "untitled.pdf"


# ===========================================================================
# 9.5 (b) — rag._format_context adds a resolved display_name per citation
# ===========================================================================
def _make_hit(source_path: str, source_file: str, i: int) -> dict:
    """A hit in the shape rag._format_context consumes."""
    return {
        "id": f"chunk-{i}",
        "text": f"retrieved text {i}",
        "metadata": {
            "source_file": source_file,
            "source_path": source_path,
            "location": f"p{i}",
            "file_type": "pdf",
        },
    }


def test_format_context_citation_display_name(monkeypatch, tmp_path) -> None:
    """Feature: citation-formatting, Property 24 (wiring): rag citation display_name is title-else-filename.

    Calling ``rag._format_context`` directly (no model needed), each citation
    dict carries ``display_name`` equal to the stored title when set for its
    source_path, else the citation's ``source_file``. Validates Req 9.3.
    """
    meta = make_metadata_store(tmp_path / "source_metadata.json")
    meta.upsert("lib/titled.pdf", {"title": "Stored Title"})
    monkeypatch.setattr(rag, "get_source_metadata_store", lambda: meta)

    hits = [
        _make_hit("lib/titled.pdf", "titled.pdf", 1),
        _make_hit("lib/plain.pdf", "plain.pdf", 2),
    ]

    _context, citations = rag._format_context(hits)

    by_path = {c["source_path"]: c for c in citations}
    assert by_path["lib/titled.pdf"]["display_name"] == "Stored Title"
    # No stored title -> falls back to the citation's source_file.
    assert by_path["lib/plain.pdf"]["display_name"] == "plain.pdf"

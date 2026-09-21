"""Integration tests for the indexer's group-pruning hooks.

Covers task 11.2 (Feature: source-selection-and-groups). These exercise
Property 14 (source-removal pruning) and Property 15 (index-reset member
clearing) *through the indexer entry points* — verifying the wiring in
``remove_source`` and ``reset_index`` actually invokes the GroupStore hooks.

The indexer's collaborators are patched where they are *used*
(``app.indexing.indexer``) so no real data, Chroma, or Ollama is touched:

- ``get_group_store`` → a real GroupStore bound to a temp file (so the actual
  ``prune_source`` / ``clear_all_members`` logic runs, just isolated).
- ``get_store`` → a fake vector store (delete_by_source / count / reset /
  all_documents).
- ``get_manifest`` → a fake manifest (remove / entries).
- ``_rebuild_keyword_index`` → a no-op so the real BM25 index is never built.

These are example-based (not Hypothesis) since they test the wiring, not the
set semantics already proven in test_group_store.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.indexing import indexer

from .conftest import make_store


class _FakeStore:
    """Minimal stand-in for the Chroma-backed VectorStore."""

    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.was_reset = False

    def delete_by_source(self, source_path: str) -> None:
        self.deleted.append(source_path)

    def reset(self) -> None:
        self.was_reset = True

    def count(self) -> int:
        return 0

    def all_documents(self) -> list:
        return []


class _FakeManifest:
    """Minimal stand-in for the Manifest."""

    def __init__(self) -> None:
        self.removed: list[str] = []

    def remove(self, source_path: str) -> None:
        self.removed.append(source_path)

    def entries(self) -> dict:
        return {}


@pytest.fixture
def wired_indexer(tmp_path: Path, monkeypatch):
    """Patch the indexer's collaborators and yield the temp-backed GroupStore.

    The GroupStore is real (so prune_source / clear_all_members truly run) but
    bound to a temp groups.json; everything else is faked to avoid Chroma,
    Ollama, the manifest file, and the keyword index.
    """
    group_store = make_store(tmp_path / "groups.json")
    fake_store = _FakeStore()
    fake_manifest = _FakeManifest()

    monkeypatch.setattr(indexer, "get_group_store", lambda: group_store)
    monkeypatch.setattr(indexer, "get_store", lambda: fake_store)
    monkeypatch.setattr(indexer, "get_manifest", lambda: fake_manifest)
    monkeypatch.setattr(indexer, "_rebuild_keyword_index", lambda: None)

    return group_store


def test_remove_source_prunes_from_all_groups(wired_indexer):
    """Feature: source-selection-and-groups, Property 14 (via indexer).

    remove_source(S) drops S from every group's members while preserving each
    group's group_id and name.
    """
    store = wired_indexer
    target = "b/target.pdf"

    g1 = store.create("Alpha", members={target, "a/one.pdf"})
    g2 = store.create("Beta", members={target, "c/two.pdf", "d/three.pdf"})
    g3 = store.create("Gamma", members={"e/four.pdf"})  # does not contain target

    ids = {g1.group_id: g1.name, g2.group_id: g2.name, g3.group_id: g3.name}

    result = indexer.remove_source(target)
    assert result["removed"] == target

    for group in store.list():
        # Property 14: the removed path is gone from every group.
        assert target not in group.members
        # Group identity (id + name) is retained.
        assert group.group_id in ids
        assert group.name == ids[group.group_id]

    # Sanity: unrelated members survive; the group set is unchanged.
    by_id = {g.group_id: g for g in store.list()}
    assert by_id[g1.group_id].members == {"a/one.pdf"}
    assert by_id[g2.group_id].members == {"c/two.pdf", "d/three.pdf"}
    assert by_id[g3.group_id].members == {"e/four.pdf"}
    assert len(store.list()) == 3


def test_reset_index_clears_all_members(wired_indexer):
    """Feature: source-selection-and-groups, Property 15 (via indexer).

    reset_index() empties every group's members but retains all groups with
    their group_id and name intact.
    """
    store = wired_indexer

    g1 = store.create("Alpha", members={"a/one.pdf", "b/two.pdf"})
    g2 = store.create("Beta", members={"c/three.pdf"})
    g3 = store.create("Gamma")  # already empty

    ids = {g1.group_id: g1.name, g2.group_id: g2.name, g3.group_id: g3.name}

    result = indexer.reset_index()
    assert result["reset"] is True

    groups = store.list()
    assert len(groups) == 3
    for group in groups:
        # Property 15: members are cleared.
        assert group.members == set()
        # Groups (id + name) are retained.
        assert group.group_id in ids
        assert group.name == ids[group.group_id]

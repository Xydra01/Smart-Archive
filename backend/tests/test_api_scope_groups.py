"""API-layer tests for scope resolution and group endpoints.

Feature: source-selection-and-groups. Covers tasks 9.3, 9.4, 9.5 (scope
resolution on /api/search) and 10.3, 10.4, 10.5, 10.6 (selectable-sources and
/api/groups CRUD). Property tests use Hypothesis at >=100 examples and are
tagged with the property they validate.

Isolation strategy (the app must never touch real user data):

  * ``resolve_scope`` and the group endpoints reach persistence via
    ``app.main.get_group_store`` (imported into main). We monkeypatch that name
    to return a fresh GroupStore bound to a per-test temp file.
  * ``_group_view`` and ``/api/selectable-sources`` read ``app.main.get_store``.
    We monkeypatch that to a fake store with a controlled ``sources()`` dict, so
    the real Chroma collection is never opened.
  * ``/api/search`` does ``from .search.hybrid import hybrid_search`` *inside*
    the handler, so we patch ``app.search.hybrid.hybrid_search`` with a fake
    that records the ``scope`` it was handed. This lets us assert on the
    resolved QueryScope without running real retrieval.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings as hyp_settings
from hypothesis import strategies as st

import app.main as main
import app.search.hybrid as hybrid_mod
from app.config import settings
from app.groups.store import GroupStore
from app.search.scope import QueryScope

from .conftest import make_store


# ---------------------------------------------------------------------------
# Fakes / fixtures
# ---------------------------------------------------------------------------
class FakeStore:
    """A stand-in for VectorStore exposing only what the API layer touches."""

    def __init__(self, sources: dict[str, int] | None = None) -> None:
        self._sources = dict(sources or {})

    def sources(self) -> dict[str, int]:
        return dict(self._sources)


class ScopeRecorder:
    """A fake hybrid_search that records the scope it was called with.

    The real handler calls ``hybrid_search(query, top_k=..., scope=...)`` and
    returns the hits verbatim. We capture ``scope`` for assertions and return an
    empty hit list so nothing downstream runs.
    """

    def __init__(self) -> None:
        self.scope: QueryScope | None = None
        self.calls = 0

    def __call__(self, query, top_k=None, scope=None):
        self.calls += 1
        self.scope = scope
        return []


@pytest.fixture
def group_store(tmp_path: Path, monkeypatch) -> GroupStore:
    """A fresh, temp-backed GroupStore wired in wherever main.py uses it."""
    store = make_store(tmp_path / "groups.json")
    monkeypatch.setattr(main, "get_group_store", lambda: store)
    return store


@pytest.fixture
def fake_store(monkeypatch) -> FakeStore:
    """A controllable fake Vector_Store wired into main.get_store."""
    fake = FakeStore()
    monkeypatch.setattr(main, "get_store", lambda: fake)
    return fake


@pytest.fixture
def recorder(monkeypatch) -> ScopeRecorder:
    """Patch the hybrid_search that /api/search imports at call time."""
    rec = ScopeRecorder()
    monkeypatch.setattr(hybrid_mod, "hybrid_search", rec)
    return rec


@pytest.fixture
def client() -> TestClient:
    return TestClient(main.app)


# A path-like alphabet keeps generated source_path values realistic.
source_paths = st.text(alphabet="abcdefgh._/", min_size=1, max_size=8)
source_lists = st.lists(source_paths, max_size=8)
nonempty_source_lists = st.lists(source_paths, min_size=1, max_size=8)


# ===========================================================================
# Task 9.3 / Property 5 — group scoping equals ad-hoc selection of members
# ===========================================================================
@hyp_settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(members=source_lists)
def test_group_equals_adhoc_selection(members, group_store, recorder, client):
    """Feature: source-selection-and-groups, Property 5: Group scoping equals
    ad-hoc selection of the same members.

    The scope resolved from ``{group_id}`` must equal the scope resolved from
    ``{sources: members}`` — both a QueryScope over frozenset(members)."""
    group = group_store.create("g", members=members)

    # Scope resolved via group_id.
    r1 = client.post("/api/search", json={"query": "q", "group_id": group.group_id})
    assert r1.status_code == 200
    group_scope = recorder.scope

    # Scope resolved via an equivalent ad-hoc selection.
    if members:
        r2 = client.post("/api/search", json={"query": "q", "sources": members})
        assert r2.status_code == 200
        adhoc_scope = recorder.scope
        assert group_scope.selection == adhoc_scope.selection
        assert group_scope.selection == frozenset(members)
    else:
        # An empty group resolves to an explicit empty selection (zero results),
        # which is distinct from an empty ad-hoc list (whole archive). Verify
        # the group side directly.
        assert group_scope.selection == frozenset()
        assert not group_scope.is_unscoped


# ===========================================================================
# Task 9.4 / Property 6 — ad-hoc selection takes precedence over group_id
# ===========================================================================
@hyp_settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(sources=nonempty_source_lists, group_members=source_lists)
def test_adhoc_precedence_over_group(
    sources, group_members, group_store, recorder, client
):
    """Feature: source-selection-and-groups, Property 6: Ad-hoc selection takes
    precedence over group_id.

    When both a non-empty ``sources`` list and a ``group_id`` are supplied, the
    resolved scope equals the ad-hoc selection and ignores the group entirely."""
    group = group_store.create("g", members=group_members)

    resp = client.post(
        "/api/search",
        json={"query": "q", "sources": sources, "group_id": group.group_id},
    )
    assert resp.status_code == 200
    assert recorder.scope.selection == frozenset(sources)


# ===========================================================================
# Task 9.5 — query-path error and boundary behaviour
# ===========================================================================
def test_selection_at_max_is_accepted(group_store, recorder, client):
    """Feature: source-selection-and-groups: a selection of exactly
    settings.max_selection sources is accepted (no 400)."""
    sources = [f"s{i}.txt" for i in range(settings.max_selection)]
    resp = client.post("/api/search", json={"query": "q", "sources": sources})
    assert resp.status_code == 200
    assert recorder.scope.selection == frozenset(sources)


def test_selection_over_max_is_rejected(group_store, recorder, client):
    """Feature: source-selection-and-groups: a selection exceeding
    settings.max_selection is rejected with 400 and never reaches retrieval."""
    sources = [f"s{i}.txt" for i in range(settings.max_selection + 1)]
    resp = client.post("/api/search", json={"query": "q", "sources": sources})
    assert resp.status_code == 400
    assert recorder.calls == 0


def test_unknown_group_id_on_search_is_404(group_store, recorder, client):
    """Feature: source-selection-and-groups: an unknown group_id on /api/search
    returns 404 and never reaches retrieval."""
    resp = client.post(
        "/api/search", json={"query": "q", "group_id": "does-not-exist"}
    )
    assert resp.status_code == 404
    assert recorder.calls == 0


def test_empty_sources_resolves_to_whole_archive(group_store, recorder, client):
    """Feature: source-selection-and-groups: an empty ad-hoc sources list maps
    to the whole archive (no selection)."""
    resp = client.post("/api/search", json={"query": "q", "sources": []})
    assert resp.status_code == 200
    assert recorder.scope.is_unscoped
    assert recorder.scope.selection is None


def test_omitted_selection_resolves_to_whole_archive(group_store, recorder, client):
    """Feature: source-selection-and-groups: omitting both sources and group_id
    maps to the whole archive (no selection)."""
    resp = client.post("/api/search", json={"query": "q"})
    assert resp.status_code == 200
    assert recorder.scope.is_unscoped
    assert recorder.scope.selection is None


# ===========================================================================
# Task 10.3 / Property 8 — selectable sources equal the Vector_Store contents
# ===========================================================================
@hyp_settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    sources=st.dictionaries(
        keys=source_paths, values=st.integers(min_value=1, max_value=9999), max_size=8
    )
)
def test_selectable_sources_match_store(sources, fake_store, client):
    """Feature: source-selection-and-groups, Property 8: Selectable sources
    equal the Vector_Store contents with counts."""
    fake_store._sources = dict(sources)

    resp = client.get("/api/selectable-sources")
    assert resp.status_code == 200
    returned = {
        item["source_path"]: item["chunks"] for item in resp.json()["sources"]
    }
    assert returned == dict(sources)


# ===========================================================================
# Task 10.4 / Property 16 — presence flags reflect the Vector_Store
# ===========================================================================
@hyp_settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    present=st.lists(source_paths, max_size=6),
    absent=st.lists(source_paths, max_size=6),
)
def test_presence_flags_reflect_store(
    present, absent, group_store, fake_store, client
):
    """Feature: source-selection-and-groups, Property 16: Presence flags reflect
    the Vector_Store — a flag is true exactly when the member is present, and
    every member (present or not) is still reported."""
    # Members present in the store carry chunk counts; absent members do not.
    fake_store._sources = {sp: 1 for sp in present}
    members = list(present) + list(absent)
    group = group_store.create("g", members=members)

    resp = client.get(f"/api/groups/{group.group_id}")
    assert resp.status_code == 200
    body = resp.json()

    expected_members = sorted(set(members))
    assert body["members"] == expected_members  # all members reported

    present_set = set(present)
    for m in expected_members:
        assert body["present"][m] == (m in present_set)


# ===========================================================================
# Task 10.5 / Property 11 — whitespace-only group names are rejected
# ===========================================================================
whitespace_names = st.text(alphabet=" \t\n\r\f\v", max_size=6)


@hyp_settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(bad_name=whitespace_names)
def test_create_rejects_blank_names(bad_name, group_store, fake_store, client):
    """Feature: source-selection-and-groups, Property 11: Whitespace-only group
    names are rejected on create with 400 and no group is created."""
    before = len(group_store.list())
    resp = client.post("/api/groups", json={"name": bad_name})
    assert resp.status_code == 400
    assert len(group_store.list()) == before  # nothing created


@hyp_settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(bad_name=whitespace_names)
def test_rename_rejects_blank_names(bad_name, group_store, fake_store, client):
    """Feature: source-selection-and-groups, Property 11: Whitespace-only group
    names are rejected on rename with 400 and the group is not renamed."""
    group = group_store.create("original", members=[])
    resp = client.patch(f"/api/groups/{group.group_id}", json={"name": bad_name})
    assert resp.status_code == 400
    assert group_store.get(group.group_id).name == "original"  # unchanged


def test_valid_name_is_accepted_on_create_and_rename(group_store, fake_store, client):
    """Feature: source-selection-and-groups: a valid (non-blank) name is
    accepted on create and rename with 200."""
    created = client.post("/api/groups", json={"name": "  Calculus  "})
    assert created.status_code == 200
    gid = created.json()["group_id"]
    # Name is trimmed by the API's _require_name.
    assert created.json()["name"] == "Calculus"

    renamed = client.patch(f"/api/groups/{gid}", json={"name": "Programming"})
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Programming"


# ===========================================================================
# Task 10.6 — group-route 404s for unknown ids
# ===========================================================================
def test_group_routes_404_on_unknown_id(group_store, fake_store, client):
    """Feature: source-selection-and-groups: an unknown group id returns 404 on
    GET one / PATCH / DELETE / add-sources / remove-sources."""
    gid = "nope-not-a-real-id"

    assert client.get(f"/api/groups/{gid}").status_code == 404
    assert client.patch(f"/api/groups/{gid}", json={"name": "x"}).status_code == 404
    assert client.delete(f"/api/groups/{gid}").status_code == 404
    assert (
        client.post(f"/api/groups/{gid}/sources", json={"sources": ["a"]}).status_code
        == 404
    )
    assert (
        client.request(
            "DELETE", f"/api/groups/{gid}/sources", json={"sources": ["a"]}
        ).status_code
        == 404
    )

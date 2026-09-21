"""Tests for GroupStore (Feature: source-selection-and-groups).

Covers tasks 1.3, 3.3-3.10. Property tests use Hypothesis at >=100 examples and
are tagged with the property they validate. Every GroupStore is bound to a
per-test temp file (see conftest.make_store) so the real data/groups.json is
never touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
)

from app.config import settings
from app.groups.store import GroupStore

from .conftest import make_store

# A small path-like alphabet keeps the input space realistic (source_path
# values) while giving Hypothesis a good chance of hitting collisions across
# groups, which is what exercises union/difference/prune semantics.
source_paths = st.text(
    alphabet="abcdefgh._/",
    min_size=1,
    max_size=8,
)
source_lists = st.lists(source_paths, max_size=8)
names = st.text(min_size=1, max_size=12).filter(lambda s: s.strip() != "")


# ---------------------------------------------------------------------------
# Task 1.3 — smoke test for the groups persistence path
# ---------------------------------------------------------------------------
def test_groups_file_under_data_and_distinct_from_manifest():
    """Feature: source-selection-and-groups — groups.json lives under data/ and
    is a separate file from the index manifest."""
    groups_file = settings.groups_file
    manifest_path = settings.archive_root / "data" / "index_manifest.json"

    # Resolves under a "data" directory.
    assert groups_file.parent.name == "data"
    assert "data" in groups_file.parts

    # Distinct from the manifest path.
    assert groups_file != manifest_path
    assert groups_file.resolve() != manifest_path.resolve()


# ---------------------------------------------------------------------------
# Task 3.3 / Property 9 — add-sources is set union and idempotent
# ---------------------------------------------------------------------------
@hyp_settings(max_examples=100)
@given(initial=source_lists, added=source_lists)
def test_add_sources_is_union_and_idempotent(tmp_path_factory, initial, added):
    """Feature: source-selection-and-groups, Property 9: Adding sources to a
    group is set union (and idempotent)."""
    path = tmp_path_factory.mktemp("grp") / "groups.json"
    store = make_store(path)
    group = store.create("g", members=initial)
    prior = set(group.members)

    after = store.add_sources(group.group_id, added)
    assert after.members == prior | set(added)

    # Idempotence: adding the same members again is a no-op.
    again = store.add_sources(group.group_id, added)
    assert again.members == prior | set(added)

    # Adding already-present members leaves the set unchanged.
    unchanged = store.add_sources(group.group_id, list(prior))
    assert unchanged.members == prior | set(added)


# ---------------------------------------------------------------------------
# Task 3.4 / Property 10 — remove-sources is set difference and idempotent
# ---------------------------------------------------------------------------
@hyp_settings(max_examples=100)
@given(initial=source_lists, removed=source_lists)
def test_remove_sources_is_difference_and_idempotent(
    tmp_path_factory, initial, removed
):
    """Feature: source-selection-and-groups, Property 10: Removing sources from
    a group is set difference (and idempotent)."""
    path = tmp_path_factory.mktemp("grp") / "groups.json"
    store = make_store(path)
    group = store.create("g", members=initial)
    prior = set(group.members)

    after = store.remove_sources(group.group_id, removed)
    assert after.members == prior - set(removed)

    # Idempotence: removing again changes nothing.
    again = store.remove_sources(group.group_id, removed)
    assert again.members == prior - set(removed)

    # Removing non-members is a no-op.
    non_members = {"zzz_not_a_member_1", "zzz_not_a_member_2"}
    unchanged = store.remove_sources(group.group_id, non_members)
    assert unchanged.members == prior - set(removed)


# ---------------------------------------------------------------------------
# Task 3.6 / Property 13 — corrupt or missing load yields empty, never raises
# ---------------------------------------------------------------------------
corrupt_bytes = st.one_of(
    st.binary(max_size=64),  # arbitrary bytes (incl. not-JSON)
    st.text(max_size=64).map(str.encode),  # arbitrary text
    st.just(b"{"),  # truncated JSON object
    st.just(b"[1, 2, 3"),  # truncated JSON array
    st.just(b"[1, 2, 3]"),  # a JSON array (wrong top-level type)
    st.just(b'{"no_groups_key": true}'),  # object missing "groups"
    st.just(b"null"),
    st.just(b"42"),
    st.just(b'"a string"'),
    st.just(b'{"groups": [1, 2, 3]}'),  # "groups" is a list, not a mapping
    st.just(b'{"groups": {"g1": {"name": "x"}}}'),  # entry missing fields
)


@hyp_settings(max_examples=100)
@given(data=corrupt_bytes)
def test_corrupt_load_is_empty_and_does_not_raise(tmp_path_factory, data):
    """Feature: source-selection-and-groups, Property 13: Corrupt or missing
    persistence loads as empty (and never raises)."""
    path = tmp_path_factory.mktemp("grp") / "groups.json"
    path.write_bytes(data)

    store = GroupStore()
    store._path = path
    store._groups = {}
    store._load()  # must not raise for any input

    assert store.list() == []


def test_missing_file_loads_empty(tmp_path):
    """Feature: source-selection-and-groups, Property 13: A missing groups file
    loads as an empty store without raising."""
    path = tmp_path / "does_not_exist.json"
    assert not path.exists()

    store = GroupStore()
    store._path = path
    store._groups = {}
    store._load()

    assert store.list() == []


# ---------------------------------------------------------------------------
# Task 3.7 / Property 14 — prune_source drops path everywhere, keeps identity
# ---------------------------------------------------------------------------
@hyp_settings(max_examples=100)
@given(
    group_members=st.lists(source_lists, min_size=1, max_size=5),
    target=source_paths,
)
def test_prune_source_drops_everywhere_and_preserves_identity(
    tmp_path_factory, group_members, target
):
    """Feature: source-selection-and-groups, Property 14: Source-removal pruning
    drops the path everywhere and preserves group identity."""
    path = tmp_path_factory.mktemp("grp") / "groups.json"
    store = make_store(path)

    created = []
    for i, members in enumerate(group_members):
        # Sprinkle the target into some groups to guarantee overlap.
        m = list(members)
        if i % 2 == 0:
            m.append(target)
        g = store.create(f"g{i}", members=m)
        created.append((g.group_id, g.name))

    store.prune_source(target)

    for gid, name in created:
        g = store.get(gid)
        assert g is not None
        assert target not in g.members  # dropped everywhere
        assert g.group_id == gid  # identity preserved
        assert g.name == name


# ---------------------------------------------------------------------------
# Task 3.8 / Property 15 — clear_all_members empties members, keeps groups
# ---------------------------------------------------------------------------
@hyp_settings(max_examples=100)
@given(group_members=st.lists(source_lists, min_size=1, max_size=5))
def test_clear_all_members_keeps_groups(tmp_path_factory, group_members):
    """Feature: source-selection-and-groups, Property 15: Index reset clears all
    members but keeps groups."""
    path = tmp_path_factory.mktemp("grp") / "groups.json"
    store = make_store(path)

    created = []
    for i, members in enumerate(group_members):
        g = store.create(f"g{i}", members=members)
        created.append((g.group_id, g.name))

    store.clear_all_members()

    for gid, name in created:
        g = store.get(gid)
        assert g is not None
        assert g.members == set()  # emptied
        assert g.group_id == gid  # retained
        assert g.name == name


# ---------------------------------------------------------------------------
# Task 3.10 — atomic write mechanism (unit test)
# ---------------------------------------------------------------------------
def test_save_produces_valid_json_and_no_leftover_tmp(store, store_path):
    """Feature: source-selection-and-groups: a save writes valid JSON to the
    target and leaves no .json.tmp file behind."""
    g1 = store.create("Calculus", members=["calculus_eighth_edition.pdf"])
    g2 = store.create("Programming", members=["Walter_J_Savitch_Problem_Solving.pdf"])

    # Target exists and is valid JSON.
    assert store_path.exists()
    payload = json.loads(store_path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert set(payload["groups"].keys()) == {g1.group_id, g2.group_id}

    # No leftover temp file after a successful save.
    tmp = store_path.with_suffix(".json.tmp")
    assert not tmp.exists()

    # The written JSON parses back to the same groups.
    fresh = make_store(store_path)
    reloaded = {g.group_id: (g.name, g.members) for g in fresh.list()}
    assert reloaded == {
        g1.group_id: (g1.name, g1.members),
        g2.group_id: (g2.name, g2.members),
    }


# ---------------------------------------------------------------------------
# Task 3.5 / Property 12 — persistence round-trips over an op sequence
# ---------------------------------------------------------------------------
# Operations are generated as tagged tuples so the sequence is deterministic and
# reproducible. group_index selects an existing group by position (mod count).
op_strategy = st.one_of(
    st.tuples(st.just("create"), names, source_lists),
    st.tuples(st.just("rename"), st.integers(0, 20), names),
    st.tuples(st.just("delete"), st.integers(0, 20)),
    st.tuples(st.just("add"), st.integers(0, 20), source_lists),
    st.tuples(st.just("remove"), st.integers(0, 20), source_lists),
    st.tuples(st.just("prune"), source_paths),
    st.tuples(st.just("clear")),
)


def _apply_op(store: GroupStore, op) -> None:
    kind = op[0]
    ids = [g.group_id for g in store.list()]
    if kind == "create":
        store.create(op[1], members=op[2])
    elif kind == "rename" and ids:
        store.rename(ids[op[1] % len(ids)], op[2])
    elif kind == "delete" and ids:
        store.delete(ids[op[1] % len(ids)])
    elif kind == "add" and ids:
        store.add_sources(ids[op[1] % len(ids)], op[2])
    elif kind == "remove" and ids:
        store.remove_sources(ids[op[1] % len(ids)], op[2])
    elif kind == "prune":
        store.prune_source(op[1])
    elif kind == "clear":
        store.clear_all_members()


@hyp_settings(max_examples=100)
@given(ops=st.lists(op_strategy, max_size=25))
def test_persistence_round_trips(tmp_path_factory, ops):
    """Feature: source-selection-and-groups, Property 12: Group persistence
    round-trips — a fresh store loaded from disk reproduces id/name/members."""
    path = tmp_path_factory.mktemp("grp") / "groups.json"
    store = make_store(path)

    for op in ops:
        _apply_op(store, op)

    in_memory = {
        g.group_id: (g.name, set(g.members)) for g in store.list()
    }

    fresh = make_store(path)
    on_disk = {g.group_id: (g.name, set(g.members)) for g in fresh.list()}

    assert on_disk == in_memory


# ---------------------------------------------------------------------------
# Task 3.9 / Property 17 — model-based state machine against a reference model
# ---------------------------------------------------------------------------
class GroupStoreModel(RuleBasedStateMachine):
    """Feature: source-selection-and-groups, Property 17: Group store matches a
    reference set model — a dict of group_id -> (name, set(members)).

    Each rule applies the same operation to the real GroupStore and to the
    reference model; the invariant asserts list()/get() agree with the model on
    group_id, name, and members after every step.
    """

    def __init__(self) -> None:
        super().__init__()
        self._tmp = Path(self._make_tmp_dir()) / "groups.json"
        self.store = make_store(self._tmp)
        self.model: dict[str, tuple[str, set[str]]] = {}

    @staticmethod
    def _make_tmp_dir() -> str:
        import tempfile

        return tempfile.mkdtemp(prefix="gsm_")

    def _ids(self) -> list[str]:
        return sorted(self.model.keys())

    @rule(name=names, members=source_lists)
    def create(self, name, members):
        g = self.store.create(name, members=members)
        self.model[g.group_id] = (name, set(members))

    @precondition(lambda self: bool(self.model))
    @rule(idx=st.integers(0, 50), name=names)
    def rename(self, idx, name):
        gid = self._ids()[idx % len(self._ids())]
        self.store.rename(gid, name)
        _, members = self.model[gid]
        self.model[gid] = (name, members)

    @precondition(lambda self: bool(self.model))
    @rule(idx=st.integers(0, 50))
    def delete(self, idx):
        gid = self._ids()[idx % len(self._ids())]
        self.store.delete(gid)
        del self.model[gid]

    @precondition(lambda self: bool(self.model))
    @rule(idx=st.integers(0, 50), sources=source_lists)
    def add(self, idx, sources):
        gid = self._ids()[idx % len(self._ids())]
        self.store.add_sources(gid, sources)
        name, members = self.model[gid]
        self.model[gid] = (name, members | set(sources))

    @precondition(lambda self: bool(self.model))
    @rule(idx=st.integers(0, 50), sources=source_lists)
    def remove(self, idx, sources):
        gid = self._ids()[idx % len(self._ids())]
        self.store.remove_sources(gid, sources)
        name, members = self.model[gid]
        self.model[gid] = (name, members - set(sources))

    @rule(source=source_paths)
    def prune(self, source):
        self.store.prune_source(source)
        for gid, (name, members) in self.model.items():
            self.model[gid] = (name, members - {source})

    @rule()
    def clear(self):
        self.store.clear_all_members()
        for gid, (name, _) in self.model.items():
            self.model[gid] = (name, set())

    @invariant()
    def store_agrees_with_model(self):
        listed = {
            g.group_id: (g.name, set(g.members)) for g in self.store.list()
        }
        assert listed == self.model
        # get() agrees per-id.
        for gid, (name, members) in self.model.items():
            g = self.store.get(gid)
            assert g is not None
            assert g.name == name
            assert set(g.members) == members


TestGroupStoreModel = GroupStoreModel.TestCase
TestGroupStoreModel.settings = hyp_settings(max_examples=100)

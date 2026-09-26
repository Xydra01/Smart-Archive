"""Tests for SourceMetadataStore (Feature: citation-formatting).

Covers tasks 2.2-2.5. Property tests use Hypothesis at >=100 examples and are
tagged with the property they validate. Every SourceMetadataStore is bound to a
per-test temp file (see conftest.make_metadata_store) so the real
data/source_metadata.json is never touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st

from app.references.store import (
    SOURCE_TYPES,
    SourceMetadata,
    SourceMetadataStore,
)

from .conftest import make_metadata_store

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
source_paths = st.text(alphabet="abcdefgh._/", min_size=1, max_size=10)

# Optional scalar string fields: either absent (None) or a non-empty-ish string.
opt_str = st.one_of(st.none(), st.text(max_size=20))
author_lists = st.lists(st.text(max_size=12), max_size=4)
source_types = st.sampled_from(SOURCE_TYPES)
styles = st.sampled_from(["MLA", "APA"])
overrides = st.dictionaries(styles, st.text(min_size=1, max_size=30), max_size=2)


@st.composite
def field_dicts(draw):
    """A dict of upsert fields with arbitrary subsets present."""
    fields: dict = {}
    if draw(st.booleans()):
        fields["source_type"] = draw(source_types)
    if draw(st.booleans()):
        fields["authors"] = draw(author_lists)
    for name in (
        "title",
        "container",
        "publisher",
        "publication_date",
        "url",
        "access_date",
    ):
        if draw(st.booleans()):
            fields[name] = draw(opt_str)
    if draw(st.booleans()):
        fields["verbatim_overrides"] = draw(overrides)
    return fields


def _reload(path: Path) -> SourceMetadataStore:
    """Load a fresh store from ``path`` (a second store on the same file)."""
    return make_metadata_store(path)


# ---------------------------------------------------------------------------
# Task 2.2 / Property 7 — round-trip and absent-stays-absent
# ---------------------------------------------------------------------------
@hyp_settings(max_examples=150)
@given(
    entries=st.lists(
        st.tuples(source_paths, field_dicts()),
        max_size=6,
    )
)
def test_store_round_trips_and_absent_stays_absent(tmp_path_factory, entries):
    """Feature: citation-formatting, Property 7: Store round-trips, and absent
    fields stay absent."""
    path = tmp_path_factory.mktemp("meta") / "source_metadata.json"
    store = make_metadata_store(path)

    for sp, fields in entries:
        store.upsert(sp, fields)

    in_memory = store.all()

    # Reload via a second store on the same path and compare record-for-record.
    fresh = _reload(path)
    on_disk = fresh.all()

    assert set(on_disk.keys()) == set(in_memory.keys())
    for sp, rec in in_memory.items():
        assert on_disk[sp] == rec

    # Absent fields never appear as null on disk. Checked structurally below
    # (per-field: an absent field is omitted from the record, never written as
    # JSON null). A raw ``"null" not in text`` substring scan would be wrong —
    # it false-positives on a legitimate stored *value* that contains "null"
    # (e.g. a verbatim override whose text is the string "null").
    if path.exists():
        raw_text = path.read_text(encoding="utf-8")
        payload = json.loads(raw_text)
        # No field is serialized as a JSON null anywhere in the records.
        for _sp, raw_rec in payload["records"].items():
            for _k, _v in raw_rec.items():
                assert _v is not None
        for sp, raw_rec in payload["records"].items():
            rec = in_memory[sp]
            # authors omitted when empty; present (a list) otherwise.
            if not rec.authors:
                assert "authors" not in raw_rec
            else:
                assert raw_rec["authors"] == rec.authors
            # verbatim_overrides omitted when empty.
            if not rec.verbatim_overrides:
                assert "verbatim_overrides" not in raw_rec
            # Absent scalar fields are omitted, present ones are written.
            for name in (
                "title",
                "container",
                "publisher",
                "publication_date",
                "url",
                "access_date",
            ):
                value = getattr(rec, name)
                if value is None:
                    assert name not in raw_rec
                else:
                    assert raw_rec[name] == value


# ---------------------------------------------------------------------------
# Task 2.3 / Property 8 — corrupt/missing load tolerance
# ---------------------------------------------------------------------------
corrupt_bytes = st.one_of(
    st.binary(max_size=64),  # arbitrary bytes (incl. not-JSON)
    st.text(max_size=64).map(str.encode),  # arbitrary text
    st.just(b"{"),  # truncated JSON object
    st.just(b"[1, 2, 3"),  # truncated JSON array
    st.just(b"[1, 2, 3]"),  # a JSON array (wrong top-level type)
    st.just(b'{"no_records_key": true}'),  # object missing "records"
    st.just(b"null"),
    st.just(b"42"),
    st.just(b'"a string"'),
    st.just(b'{"records": [1, 2, 3]}'),  # "records" is a list, not a mapping
    st.just(b'{"records": {"a": [1, 2]}}'),  # a record that is not a dict
    st.just(b'{"records": {"a": "not a dict"}}'),  # record not a mapping
    st.just(b'{"version": 1, "records": {}}'),  # valid-but-empty
)


@hyp_settings(max_examples=150)
@given(data=corrupt_bytes)
def test_corrupt_load_is_empty_and_does_not_raise(tmp_path_factory, data):
    """Feature: citation-formatting, Property 8: Missing or corrupt persistence
    loads as empty and never raises."""
    path = tmp_path_factory.mktemp("meta") / "source_metadata.json"
    path.write_bytes(data)

    store = SourceMetadataStore()
    store._path = path
    store._records = {}
    store._load()  # must not raise for any input

    # Empty unless the bytes happened to be a valid non-empty document; the
    # crafted corrupt inputs above are all empty, and the one valid document is
    # the empty {} case.
    assert isinstance(store.all(), dict)
    # None of the crafted inputs carry records, so the store is empty.
    assert store.all() == {}


def test_missing_file_loads_empty(tmp_path):
    """Feature: citation-formatting, Property 8: A missing metadata file loads as
    an empty store without raising."""
    path = tmp_path / "does_not_exist.json"
    assert not path.exists()

    store = SourceMetadataStore()
    store._path = path
    store._records = {}
    store._load()

    assert store.all() == {}


# ---------------------------------------------------------------------------
# Task 2.4 / Property 9 — prune/clear lifecycle hooks
# ---------------------------------------------------------------------------
@hyp_settings(max_examples=100)
@given(
    paths=st.lists(source_paths, min_size=1, max_size=8, unique=True),
    target_idx=st.integers(min_value=0, max_value=50),
)
def test_prune_removes_one_and_keeps_rest(tmp_path_factory, paths, target_idx):
    """Feature: citation-formatting, Property 9: prune_source removes one record
    and leaves the rest; clear_all removes every record."""
    path = tmp_path_factory.mktemp("meta") / "source_metadata.json"
    store = make_metadata_store(path)

    for sp in paths:
        store.upsert(sp, {"title": "T"})

    target = paths[target_idx % len(paths)]
    before = set(store.all().keys())
    store.prune_source(target)
    after = set(store.all().keys())

    assert target not in after
    assert after == before - {target}

    # Pruning a non-existent path is a no-op.
    store.prune_source("zzz_not_a_real_path")
    assert set(store.all().keys()) == after

    # clear_all removes every record.
    store.clear_all()
    assert store.all() == {}


# ---------------------------------------------------------------------------
# Task 2.5 — atomic-write mechanism (unit test)
# ---------------------------------------------------------------------------
def test_save_produces_valid_json_and_no_leftover_tmp(
    metadata_store, metadata_store_path
):
    """Feature: citation-formatting: a save writes valid JSON to the target and
    leaves no .json.tmp file behind (atomic replace)."""
    metadata_store.upsert("calculus.pdf", {"source_type": "book", "title": "Calculus"})
    metadata_store.upsert("paper.pdf", {"source_type": "article", "title": "A Paper"})

    # Target exists and is valid JSON.
    assert metadata_store_path.exists()
    payload = json.loads(metadata_store_path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert set(payload["records"].keys()) == {"calculus.pdf", "paper.pdf"}

    # No leftover temp file after a successful save.
    tmp = metadata_store_path.with_suffix(".json.tmp")
    assert not tmp.exists()

    # The written JSON parses back to the same records via a fresh store.
    fresh = make_metadata_store(metadata_store_path)
    reloaded = fresh.all()
    assert set(reloaded.keys()) == {"calculus.pdf", "paper.pdf"}
    assert reloaded["calculus.pdf"].source_type == "book"
    assert reloaded["calculus.pdf"].title == "Calculus"

"""Shared fixtures for the backend test suite.

The GroupStore tests must never touch the real ``data/groups.json``. Every
fixture here binds a fresh ``GroupStore`` to a unique temp file so tests are
isolated from real user data and from each other.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.groups.store import Group, GroupStore
from app.references.store import SourceMetadataStore


def make_store(path: Path) -> GroupStore:
    """Construct a GroupStore bound to ``path`` instead of settings.groups_file.

    We build the instance directly (not the get_group_store() singleton) and
    re-point it at a temp file before any save happens. Constructing then
    clearing is safe: __init__ only reads settings.groups_file via _load, and
    if that file is missing _load leaves _groups empty. We overwrite _path and
    _groups regardless so nothing from the real store leaks in.
    """
    store = GroupStore()
    store._path = path
    store._groups = {}
    # Re-load from the (usually absent) temp file so the store reflects path.
    store._load()
    return store


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    """A unique groups.json path under a per-test temp directory."""
    return tmp_path / "groups.json"


@pytest.fixture
def store(store_path: Path) -> GroupStore:
    """A fresh, empty GroupStore backed by a unique temp file."""
    return make_store(store_path)


def make_metadata_store(path: Path) -> SourceMetadataStore:
    """Construct a SourceMetadataStore bound to ``path`` instead of
    settings.source_metadata_file.

    Mirrors make_store: build the instance directly (not the
    get_source_metadata_store() singleton), then re-point it at a temp file
    before any save happens. __init__ only reads settings.source_metadata_file
    via _load; if that file is missing _load leaves _records empty. We overwrite
    _path and _records regardless so nothing from the real store leaks in, then
    reload from the (usually absent) temp file so the store reflects path.
    """
    store = SourceMetadataStore()
    store._path = path
    store._records = {}
    store._load()
    return store


@pytest.fixture
def metadata_store_path(tmp_path: Path) -> Path:
    """A unique source_metadata.json path under a per-test temp directory."""
    return tmp_path / "source_metadata.json"


@pytest.fixture
def metadata_store(metadata_store_path: Path) -> SourceMetadataStore:
    """A fresh, empty SourceMetadataStore backed by a unique temp file."""
    return make_metadata_store(metadata_store_path)

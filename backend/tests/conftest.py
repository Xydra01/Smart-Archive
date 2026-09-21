"""Shared fixtures for the backend test suite.

The GroupStore tests must never touch the real ``data/groups.json``. Every
fixture here binds a fresh ``GroupStore`` to a unique temp file so tests are
isolated from real user data and from each other.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.groups.store import Group, GroupStore


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

"""Persistent source groups.

A group is a named, reusable selection of source files. It lets a user scope
searches and questions to a fixed set of documents (e.g. "Calculus project")
without re-picking the sources each time.

Groups are stored as JSON at data/groups.json, beside index_manifest.json but
separate from the Manifest. Like the manifest it's derived, user-owned state:
git-ignored along with the rest of data/, and safe to delete (that just drops
the saved groups). The on-disk shape mirrors index_manifest.json so the
load/save code stays familiar and the round-trip is total.

Membership is a set semantically (a source belongs to a group or it doesn't),
persisted as a sorted list for deterministic output. Stale members — sources
that were removed or never indexed — are retained in the group and simply
ignored at query time.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Iterable, Optional

from ..config import settings


@dataclass
class Group:
    group_id: str  # uuid4 hex; stable, name-independent
    name: str  # display name
    members: set[str]  # source_path values; stored as a sorted list on disk
    created_at: float
    updated_at: float


class GroupStore:
    """In-memory group registry backed by an atomically-written JSON file.

    Mirrors ``Manifest``: a lazily-created module-level singleton, a
    ``threading.Lock`` guarding every mutation, a corrupt/missing-tolerant
    ``_load`` that never raises, and a ``_save`` that writes a temp file then
    atomically replaces the target. Membership is a set in memory and a sorted
    list on disk for deterministic output.
    """

    def __init__(self) -> None:
        self._path = settings.groups_file
        self._groups: dict[str, Group] = {}  # keyed by group_id
        self._lock = threading.Lock()
        self._load()

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            # Corrupt or unreadable groups file (bad JSON, I/O error, or a
            # non-object document): treat as empty. Groups are derived,
            # user-owned state, so starting empty is safe and never raises.
            self._groups = {}
            return
        groups: dict[str, Group] = {}
        try:
            for gid, g in raw.get("groups", {}).items():
                groups[gid] = Group(
                    group_id=g["group_id"],
                    name=g["name"],
                    members=set(g.get("members", [])),
                    created_at=g["created_at"],
                    updated_at=g["updated_at"],
                )
        except Exception:
            # Structurally invalid document: treat as empty, never raise.
            self._groups = {}
            return
        self._groups = groups

    def _save(self) -> None:
        payload = {
            "version": 1,
            "groups": {
                gid: {
                    "group_id": g.group_id,
                    "name": g.name,
                    "members": sorted(g.members),
                    "created_at": g.created_at,
                    "updated_at": g.updated_at,
                }
                for gid, g in self._groups.items()
            },
        }
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._path)  # atomic on the same filesystem

    # -- queries -----------------------------------------------------------
    def list(self) -> list[Group]:
        with self._lock:
            return list(self._groups.values())

    def get(self, group_id: str) -> Optional[Group]:
        return self._groups.get(group_id)

    # -- mutations ---------------------------------------------------------
    def create(self, name: str, members: Iterable[str] = ()) -> Group:
        now = time.time()
        group = Group(
            group_id=uuid.uuid4().hex,
            name=name,
            members=set(members),
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._groups[group.group_id] = group
            self._save()
        return group

    def rename(self, group_id: str, name: str) -> Group:
        with self._lock:
            group = self._groups.get(group_id)
            if group is None:
                raise KeyError(group_id)
            group.name = name
            group.updated_at = time.time()
            self._save()
        return group

    def delete(self, group_id: str) -> None:
        with self._lock:
            if group_id not in self._groups:
                raise KeyError(group_id)
            del self._groups[group_id]
            self._save()

    def add_sources(self, group_id: str, sources: Iterable[str]) -> Group:
        with self._lock:
            group = self._groups.get(group_id)
            if group is None:
                raise KeyError(group_id)
            group.members |= set(sources)  # union; idempotent
            group.updated_at = time.time()
            self._save()
        return group

    def remove_sources(self, group_id: str, sources: Iterable[str]) -> Group:
        with self._lock:
            group = self._groups.get(group_id)
            if group is None:
                raise KeyError(group_id)
            group.members -= set(sources)  # difference; idempotent
            group.updated_at = time.time()
            self._save()
        return group

    # -- index-lifecycle hooks --------------------------------------------
    def prune_source(self, source_path: str) -> None:
        """Drop ``source_path`` from every group's members; persist once.

        Each group's id and name are preserved; only membership changes.
        """
        with self._lock:
            changed = False
            for group in self._groups.values():
                if source_path in group.members:
                    group.members.discard(source_path)
                    group.updated_at = time.time()
                    changed = True
            if changed:
                self._save()

    def clear_all_members(self) -> None:
        """Empty every group's members; persist once. Keeps id and name."""
        with self._lock:
            changed = False
            for group in self._groups.values():
                if group.members:
                    group.members = set()
                    group.updated_at = time.time()
                    changed = True
            if changed:
                self._save()


_store: Optional[GroupStore] = None


def get_group_store() -> GroupStore:
    global _store
    if _store is None:
        _store = GroupStore()
    return _store

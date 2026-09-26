"""Persistent per-source bibliographic metadata.

Each record holds the citation-relevant facts a user has entered (or imported)
for a single indexed source: its type, authors, title, container, publisher,
dates, URL, and any pre-formatted verbatim overrides. Records are keyed by
``source_path`` and persisted as JSON at ``data/source_metadata.json``, beside
``groups.json`` and ``index_manifest.json`` but in a separate file.

Like the manifest and the group store this is derived, user-owned state:
git-ignored along with the rest of ``data/`` and safe to delete (that just
drops the saved metadata). The on-disk shape mirrors ``groups.json`` so the
load/save code stays familiar and the round-trip is total.

The store persists *stored* data verbatim — absent fields stay absent (Req 2.8),
so the JSON never carries ``null`` for an unset field. Merging with computed
defaults (the title fallback, and the read-time presentation of ``access_date``)
happens in a thin effective-record layer, not here. The one exception is
``access_date`` on *first creation* (Req 3.5), which the store persists so the
consulted date is stable rather than drifting to "today" on every read.
"""

from __future__ import annotations

import datetime
import json
import threading
from dataclasses import dataclass, field
from typing import Optional

from ..config import settings

# The five allowed source types. "other" is the default and the fallback for
# any unknown/absent type on read.
SOURCE_TYPES = ("book", "article", "website", "report", "other")


@dataclass
class SourceMetadata:
    source_path: str
    source_type: str = "other"  # one of SOURCE_TYPES
    authors: list[str] = field(default_factory=list)  # ordered; may be empty
    title: Optional[str] = None
    container: Optional[str] = None
    publisher: Optional[str] = None
    publication_date: Optional[str] = None  # ISO YYYY-MM-DD or None
    url: Optional[str] = None
    access_date: Optional[str] = None  # ISO YYYY-MM-DD or None
    # Citation_Style -> pre-formatted text; absent styles omitted.
    verbatim_overrides: dict[str, str] = field(default_factory=dict)


class SourceMetadataStore:
    """In-memory metadata registry backed by an atomically-written JSON file.

    Mirrors ``GroupStore``: a lazily-created module-level singleton, a
    ``threading.Lock`` guarding every mutation, a corrupt/missing-tolerant
    ``_load`` that never raises, and a ``_save`` that writes a temp file then
    atomically replaces the target. Absent fields are omitted from the JSON
    (never written as ``null``) so a read merges them back to "absent".
    """

    def __init__(self) -> None:
        self._path = settings.source_metadata_file
        self._records: dict[str, SourceMetadata] = {}  # keyed by source_path
        self._lock = threading.Lock()
        self._load()

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            # Corrupt or unreadable file (bad JSON, I/O error): treat as empty.
            # Metadata is derived, user-owned state, so starting empty is safe
            # and never raises.
            self._records = {}
            return
        if not isinstance(raw, dict):
            # Non-object document: treat as empty, never raise.
            self._records = {}
            return
        records_raw = raw.get("records", {})
        if not isinstance(records_raw, dict):
            # "records" is not a mapping: treat as empty, never raise.
            self._records = {}
            return
        records: dict[str, SourceMetadata] = {}
        try:
            for sp, r in records_raw.items():
                if not isinstance(r, dict):
                    # A structurally invalid record: treat the whole document
                    # as empty, never raise.
                    self._records = {}
                    return
                records[sp] = _record_from_raw(sp, r)
        except Exception:
            # Any structural surprise: treat as empty, never raise.
            self._records = {}
            return
        self._records = records

    def _save(self) -> None:
        payload = {
            "version": 1,
            "records": {
                sp: _record_to_raw(rec) for sp, rec in self._records.items()
            },
        }
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._path)  # atomic on the same filesystem

    # -- queries -----------------------------------------------------------
    def get(self, source_path: str) -> Optional[SourceMetadata]:
        return self._records.get(source_path)

    def all(self) -> dict[str, SourceMetadata]:
        """Return a copy of the stored records only (no computed defaults)."""
        with self._lock:
            return dict(self._records)

    # -- mutations ---------------------------------------------------------
    def upsert(self, source_path: str, fields: dict) -> SourceMetadata:
        """Merge ``fields`` into the record for ``source_path``, persist, return it.

        Creates the record if it does not exist. On *creation* only, when the
        caller supplies no ``access_date``, ``access_date`` defaults to today in
        the server's local time zone (Req 3.5); on updates it is left alone
        unless ``fields`` supplies it. A supplied ``verbatim_overrides`` mapping
        is merged per-style into any existing map rather than clobbering it.
        """
        with self._lock:
            existing = self._records.get(source_path)
            creating = existing is None
            record = existing if existing is not None else SourceMetadata(
                source_path=source_path
            )

            for key, value in fields.items():
                if key == "source_path":
                    # The key is authoritative; never let a field override it.
                    continue
                if key == "verbatim_overrides":
                    if isinstance(value, dict):
                        # Per-style replace: merge new entries into the existing
                        # map rather than clobbering the whole map.
                        record.verbatim_overrides.update(value)
                    continue
                if hasattr(record, key):
                    setattr(record, key, value)

            if creating and "access_date" not in fields:
                # Stabilize the consulted access date at creation time so it
                # doesn't drift to "today" on every read (Req 3.5).
                record.access_date = datetime.date.today().isoformat()

            self._records[source_path] = record
            self._save()
            return record

    # -- index-lifecycle hooks --------------------------------------------
    def prune_source(self, source_path: str) -> None:
        """Drop the record for ``source_path`` if present; persist once."""
        with self._lock:
            if source_path in self._records:
                del self._records[source_path]
                self._save()

    def clear_all(self) -> None:
        """Drop every record; persist once. No-op when already empty."""
        with self._lock:
            if self._records:
                self._records = {}
                self._save()


def _record_from_raw(source_path: str, r: dict) -> SourceMetadata:
    """Build a ``SourceMetadata`` from a raw JSON record, tolerating absent or
    wrongly-typed fields (absent -> None / empty).
    """
    source_type = r.get("source_type")
    if not isinstance(source_type, str):
        source_type = "other"

    authors = r.get("authors")
    if not isinstance(authors, list):
        authors = []
    else:
        authors = [a for a in authors if isinstance(a, str)]

    overrides_raw = r.get("verbatim_overrides")
    overrides: dict[str, str] = {}
    if isinstance(overrides_raw, dict):
        overrides = {
            k: v
            for k, v in overrides_raw.items()
            if isinstance(k, str) and isinstance(v, str)
        }

    def _opt_str(name: str) -> Optional[str]:
        v = r.get(name)
        return v if isinstance(v, str) else None

    return SourceMetadata(
        source_path=source_path,
        source_type=source_type,
        authors=authors,
        title=_opt_str("title"),
        container=_opt_str("container"),
        publisher=_opt_str("publisher"),
        publication_date=_opt_str("publication_date"),
        url=_opt_str("url"),
        access_date=_opt_str("access_date"),
        verbatim_overrides=overrides,
    )


def _record_to_raw(rec: SourceMetadata) -> dict:
    """Serialize a record to its JSON form, omitting absent fields entirely
    (never writing ``null``) so a read merges them back to "absent" (Req 2.8).
    """
    out: dict = {
        # Keyed by source_path; also stored inside for symmetry with GroupStore.
        "source_path": rec.source_path,
        "source_type": rec.source_type,  # always written
    }
    if rec.authors:
        out["authors"] = list(rec.authors)
    if rec.title is not None:
        out["title"] = rec.title
    if rec.container is not None:
        out["container"] = rec.container
    if rec.publisher is not None:
        out["publisher"] = rec.publisher
    if rec.publication_date is not None:
        out["publication_date"] = rec.publication_date
    if rec.url is not None:
        out["url"] = rec.url
    if rec.access_date is not None:
        out["access_date"] = rec.access_date
    if rec.verbatim_overrides:
        out["verbatim_overrides"] = dict(rec.verbatim_overrides)
    return out


_store: Optional[SourceMetadataStore] = None


def get_source_metadata_store() -> SourceMetadataStore:
    global _store
    if _store is None:
        _store = SourceMetadataStore()
    return _store

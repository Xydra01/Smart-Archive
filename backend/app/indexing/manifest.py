"""Persistent index manifest.

Records which files have been indexed and a signature of their content, so that
on restart (or a re-run of bulk ingest) we skip files that haven't changed
instead of re-embedding them. Embedding is the expensive step, so this saves
minutes per large document.

Change detection uses a cheap signature first (file size + modification time).
If those match a recorded entry, the file is considered unchanged. Size+mtime
is the same heuristic build tools use; it's fast and correct for the common
case. A content hash is also stored (computed once at index time) so a future
"verify" pass could detect edits that preserve size and mtime.

Stored as JSON at data/index_manifest.json. It's derived state — safe to delete
(the next index run rebuilds it), and it's git-ignored along with the rest of
data/.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from ..config import settings


@dataclass
class ManifestEntry:
    source_path: str  # path relative to raw_dir
    size: int
    mtime: float
    content_hash: str
    chunk_count: int
    indexed_at: float


def _hash_file(path: Path, chunk_size: int = 1 << 20) -> str:
    """SHA-256 of file contents, read in chunks to stay memory-friendly."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            block = f.read(chunk_size)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


class Manifest:
    def __init__(self) -> None:
        self._path = settings.archive_root / "data" / "index_manifest.json"
        self._entries: dict[str, ManifestEntry] = {}
        self._lock = threading.Lock()
        self._load()

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            # Corrupt or unreadable manifest (bad JSON or I/O error): treat as
            # empty; it will be rebuilt on the next index run.
            self._entries = {}
            return
        self._entries = {
            k: ManifestEntry(**v) for k, v in raw.get("entries", {}).items()
        }

    def _save(self) -> None:
        payload = {
            "version": 1,
            "entries": {k: asdict(v) for k, v in self._entries.items()},
        }
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._path)  # atomic on the same filesystem

    # -- change detection --------------------------------------------------
    def is_unchanged(self, path: Path, rel: str) -> bool:
        """True if ``path`` matches its recorded signature (size + mtime)."""
        entry = self._entries.get(rel)
        if entry is None:
            return False
        try:
            st = path.stat()
        except OSError:
            return False
        return int(st.st_size) == entry.size and float(st.st_mtime) == entry.mtime

    def record(self, path: Path, rel: str, chunk_count: int) -> None:
        import time

        st = path.stat()
        entry = ManifestEntry(
            source_path=rel,
            size=int(st.st_size),
            mtime=float(st.st_mtime),
            content_hash=_hash_file(path),
            chunk_count=chunk_count,
            indexed_at=time.time(),
        )
        with self._lock:
            self._entries[rel] = entry
            self._save()

    def record_imported(self, rel: str, chunk_count: int) -> None:
        """Mark a source present when its chunks came from an imported bundle.

        Imported sources may have no local raw file, so we record a sentinel
        signature (size -1, mtime 0, hash "imported"). Because that signature
        can never match a real file's stat, ``is_unchanged`` returns False for
        it — so if the user later drops the actual file into data/raw and
        reindexes, it is treated as new and indexed locally rather than skipped.
        """
        import time

        with self._lock:
            self._entries[rel] = ManifestEntry(
                source_path=rel,
                size=-1,
                mtime=0.0,
                content_hash="imported",
                chunk_count=chunk_count,
                indexed_at=time.time(),
            )
            self._save()

    def remove(self, rel: str) -> None:
        with self._lock:
            if rel in self._entries:
                del self._entries[rel]
                self._save()

    def prune_missing(self, existing_rels: set[str]) -> list[str]:
        """Drop entries whose files no longer exist. Returns removed rels."""
        removed = []
        with self._lock:
            for rel in list(self._entries.keys()):
                if rel not in existing_rels:
                    del self._entries[rel]
                    removed.append(rel)
            if removed:
                self._save()
        return removed

    def entries(self) -> dict[str, ManifestEntry]:
        with self._lock:
            return dict(self._entries)

    def get(self, rel: str) -> Optional[ManifestEntry]:
        return self._entries.get(rel)


_manifest: Optional[Manifest] = None


def get_manifest() -> Manifest:
    global _manifest
    if _manifest is None:
        _manifest = Manifest()
    return _manifest

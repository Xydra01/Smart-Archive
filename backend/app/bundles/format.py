"""Portable bundle format: gzip JSONL of indexed chunks.

A bundle is a single file that moves indexed chunks (text + embedding vector +
metadata) between Smart Archive installations, so a powerful machine can build
an index once and weaker machines can query it without re-embedding.

On-disk layout (UTF-8, gzip by default, one JSON object per line):

    line 1: {"_bundle": { ...BundleHeader... }}      # self-describing header
    line 2+: {"id","text","embedding","metadata"}    # one ChunkRecord per line

The writer streams records to a temp file while accumulating the count and a
sha256 over the record lines, then writes the final file with the completed
header prepended (so chunk_count/checksum are accurate without buffering every
record in memory). The reader is a generator that validates and yields the
header first, then records one at a time — bundles may be large and may come
from other people, so nothing is loaded whole and every line is size-capped and
validated before parsing. Bundle content is always treated as data: never
executed, never unpickled.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator, Optional

from ..config import settings

BUNDLE_FORMAT_VERSION = 1

# Content types considered "vision-derived" for the header's vision_included flag.
_VISION_CONTENT_TYPES = frozenset({"chart", "figure", "ocr"})

_HEADER_KEY = "_bundle"
_REQUIRED_RECORD_FIELDS = ("id", "text", "embedding", "metadata")


class BundleError(Exception):
    """Raised for unrecoverable bundle problems (bad/absent/unsupported header)."""


@dataclass
class BundleHeader:
    embed_model: str
    embed_dim: int
    chunk_count: int
    checksum: str  # "sha256:<hex>" over the concatenated record lines
    vision_included: bool = False
    sources: dict[str, int] = field(default_factory=dict)  # source_path -> count
    created_at: float = field(default_factory=time.time)
    format_version: int = BUNDLE_FORMAT_VERSION

    def to_line(self) -> str:
        return json.dumps({_HEADER_KEY: asdict(self)}, ensure_ascii=False)


@dataclass
class ChunkRecord:
    id: str
    text: str
    embedding: list[float]
    metadata: dict

    def to_line(self) -> str:
        return json.dumps(
            {
                "id": self.id,
                "text": self.text,
                "embedding": self.embedding,
                "metadata": self.metadata,
            },
            ensure_ascii=False,
        )


def _open_write(path: Path, gzip_output: bool):
    if gzip_output:
        return gzip.open(path, "wt", encoding="utf-8")
    return open(path, "w", encoding="utf-8")


def _open_read(path: Path):
    """Open a bundle for reading, transparently handling gzip vs plain text.

    Sniffs the gzip magic bytes rather than trusting the extension.
    """
    with open(path, "rb") as f:
        magic = f.read(2)
    if magic == b"\x1f\x8b":
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, "r", encoding="utf-8")


class BundleWriter:
    """Streams chunk records to a bundle, finalizing an accurate header.

    Usage:
        with BundleWriter(out_path, embed_model, gzip_output=True) as w:
            for rec in records:
                w.add(rec)
        report = w.report  # counts, sources, path
    """

    def __init__(
        self,
        out_path: Path,
        embed_model: str,
        *,
        gzip_output: Optional[bool] = None,
    ) -> None:
        self.out_path = Path(out_path)
        self.embed_model = embed_model
        self.gzip_output = (
            settings.bundle_default_gzip if gzip_output is None else gzip_output
        )
        self._tmp_records = self.out_path.with_suffix(self.out_path.suffix + ".records.tmp")
        self._fh = None
        self._hasher = hashlib.sha256()
        self._count = 0
        self._embed_dim: Optional[int] = None
        self._vision = False
        self._sources: dict[str, int] = {}
        self.report: dict = {}

    def __enter__(self) -> "BundleWriter":
        # Records are written uncompressed to a temp file first; the final
        # (optionally gzipped) bundle is assembled on close once the header is
        # known.
        self._fh = open(self._tmp_records, "w", encoding="utf-8")
        return self

    def add(self, record: ChunkRecord) -> None:
        if self._embed_dim is None:
            self._embed_dim = len(record.embedding)
        line = record.to_line()
        self._fh.write(line)
        self._fh.write("\n")
        # Checksum covers exactly the record lines (with their trailing newline)
        # so the reader can recompute it identically.
        self._hasher.update((line + "\n").encode("utf-8"))
        self._count += 1
        ct = record.metadata.get("content_type", "text")
        if ct in _VISION_CONTENT_TYPES:
            self._vision = True
        sp = record.metadata.get("source_path", "unknown")
        self._sources[sp] = self._sources.get(sp, 0) + 1

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self._fh.close()
            if exc_type is not None:
                return  # leave temp for debugging; do not write a partial bundle
            header = BundleHeader(
                embed_model=self.embed_model,
                embed_dim=self._embed_dim or 0,
                chunk_count=self._count,
                checksum="sha256:" + self._hasher.hexdigest(),
                vision_included=self._vision,
                sources=self._sources,
            )
            final_tmp = self.out_path.with_suffix(self.out_path.suffix + ".tmp")
            with _open_write(final_tmp, self.gzip_output) as out:
                out.write(header.to_line())
                out.write("\n")
                with open(self._tmp_records, "r", encoding="utf-8") as rec:
                    for line in rec:
                        out.write(line)
            os.replace(final_tmp, self.out_path)
            self.report = {
                "path": str(self.out_path),
                "chunk_count": self._count,
                "embed_model": self.embed_model,
                "embed_dim": self._embed_dim or 0,
                "vision_included": self._vision,
                "sources": self._sources,
            }
        finally:
            if self._tmp_records.exists():
                self._tmp_records.unlink()


def read_header(path: Path) -> BundleHeader:
    """Read and validate just the header line. Raises BundleError if invalid."""
    with _open_read(path) as f:
        first = f.readline()
    return _parse_header(first)


def _parse_header(line: str) -> BundleHeader:
    if not line.strip():
        raise BundleError("bundle is empty or has no header line")
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as e:
        raise BundleError(f"header is not valid JSON: {e}") from None
    if not isinstance(obj, dict) or _HEADER_KEY not in obj:
        raise BundleError("first line is not a bundle header")
    h = obj[_HEADER_KEY]
    if not isinstance(h, dict):
        raise BundleError("bundle header is malformed")
    try:
        version = int(h["format_version"])
        header = BundleHeader(
            embed_model=str(h["embed_model"]),
            embed_dim=int(h["embed_dim"]),
            chunk_count=int(h["chunk_count"]),
            checksum=str(h["checksum"]),
            vision_included=bool(h.get("vision_included", False)),
            sources=dict(h.get("sources", {})),
            created_at=float(h.get("created_at", 0.0)),
            format_version=version,
        )
    except (KeyError, TypeError, ValueError) as e:
        raise BundleError(f"bundle header missing/invalid fields: {e}") from None
    if header.format_version != BUNDLE_FORMAT_VERSION:
        raise BundleError(
            f"unsupported bundle format version {header.format_version} "
            f"(this build supports {BUNDLE_FORMAT_VERSION})"
        )
    return header


@dataclass
class ReadResult:
    header: BundleHeader
    records: Iterator  # yields ChunkRecord; invalid lines counted, not yielded
    # These are populated as the generator is consumed:
    invalid_count_holder: dict


def read_bundle(path: Path, max_record_bytes: Optional[int] = None):
    """Open a bundle: return (header, record_generator, stats).

    ``header`` is validated eagerly (raises BundleError on a bad/unsupported
    header — an unrecoverable, whole-bundle problem). The generator yields valid
    ChunkRecords; malformed, oversized, or wrong-dimension lines are skipped and
    tallied in ``stats`` (a dict updated as iteration proceeds):

        stats = {"invalid": int, "checksum_ok": bool | None}

    ``checksum_ok`` is None until the generator is exhausted, then set by
    comparing a recomputed sha256 over the record lines to the header checksum.
    """
    cap = settings.bundle_max_record_bytes if max_record_bytes is None else max_record_bytes
    header = read_header(path)
    stats: dict = {"invalid": 0, "checksum_ok": None}

    def _gen() -> Iterator[ChunkRecord]:
        hasher = hashlib.sha256()
        with _open_read(path) as f:
            f.readline()  # skip header line (already parsed)
            for raw in f:
                # Size-cap before parsing so an oversized untrusted line can't
                # blow up memory. len(str) is a proxy; lines are UTF-8 text.
                if len(raw.encode("utf-8")) > cap:
                    stats["invalid"] += 1
                    continue
                hasher.update(raw.encode("utf-8"))
                s = raw.strip()
                if not s:
                    continue
                try:
                    obj = json.loads(s)
                except json.JSONDecodeError:
                    stats["invalid"] += 1
                    continue
                if not isinstance(obj, dict) or any(
                    k not in obj for k in _REQUIRED_RECORD_FIELDS
                ):
                    stats["invalid"] += 1
                    continue
                emb = obj["embedding"]
                if (
                    not isinstance(emb, list)
                    or len(emb) != header.embed_dim
                    or not all(isinstance(x, (int, float)) for x in emb)
                ):
                    stats["invalid"] += 1
                    continue
                if not isinstance(obj["metadata"], dict) or not isinstance(
                    obj["id"], str
                ):
                    stats["invalid"] += 1
                    continue
                yield ChunkRecord(
                    id=obj["id"],
                    text=str(obj["text"]),
                    embedding=[float(x) for x in emb],
                    metadata=obj["metadata"],
                )
        expected = header.checksum.split(":", 1)[-1]
        stats["checksum_ok"] = hasher.hexdigest() == expected

    return header, _gen(), stats

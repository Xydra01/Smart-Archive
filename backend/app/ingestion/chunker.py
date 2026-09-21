"""Systematic, token-aware chunking with rich metadata labeling.

Every chunk gets:
  - a stable, content-derived id (so re-ingesting a file is idempotent)
  - source provenance (file name, relative path, file type)
  - location within the source (page / chapter / row range / heading)
  - ordering info (chunk_index, total_chunks) for navigation
  - token_count

Chunking is measured in tokens (tiktoken cl100k_base) rather than characters so
that long documents split predictably regardless of formatting or language.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, asdict
from pathlib import Path

import tiktoken

from .content_types import CONTENT_TEXT
from .loaders import LoadedSection

# cl100k_base is a good general-purpose tokenizer; it only informs chunk sizing,
# not the actual model, so an exact match to Bonsai's tokenizer isn't required.
_ENC = tiktoken.get_encoding("cl100k_base")


@dataclass
class Chunk:
    id: str
    text: str
    # --- provenance / labels ---
    source_file: str
    source_path: str
    file_type: str
    location: str
    chunk_index: int
    total_chunks: int
    token_count: int
    extra: dict

    def to_metadata(self) -> dict:
        """Flatten to a Chroma-compatible metadata dict (scalars only)."""
        md = {
            "source_file": self.source_file,
            "source_path": self.source_path,
            "file_type": self.file_type,
            "location": self.location,
            "chunk_index": self.chunk_index,
            "total_chunks": self.total_chunks,
            "token_count": self.token_count,
            # Origin of this chunk's text: text | table | chart | figure | ocr.
            "content_type": self.extra.get("content_type", CONTENT_TEXT),
        }
        # Merge scalar extras (page number, chapter, row range, heading…).
        for k, v in self.extra.items():
            if isinstance(v, (str, int, float, bool)):
                md[k] = v
        return md


def _split_tokens(text: str, chunk_tokens: int, overlap: int) -> list[str]:
    """Split text into overlapping token windows, decoded back to strings."""
    tokens = _ENC.encode(text)
    if not tokens:
        return []
    if len(tokens) <= chunk_tokens:
        return [text]

    step = max(1, chunk_tokens - overlap)
    pieces: list[str] = []
    for start in range(0, len(tokens), step):
        window = tokens[start : start + chunk_tokens]
        if not window:
            break
        pieces.append(_ENC.decode(window))
        if start + chunk_tokens >= len(tokens):
            break
    return pieces


def _chunk_id(source_path: str, location: str, index: int, text: str) -> str:
    h = hashlib.sha1()
    h.update(source_path.encode("utf-8"))
    h.update(b"\x00")
    h.update(location.encode("utf-8"))
    h.update(b"\x00")
    h.update(str(index).encode("utf-8"))
    h.update(b"\x00")
    h.update(text.encode("utf-8"))
    return h.hexdigest()


def chunk_sections(
    sections: list[LoadedSection],
    *,
    source_path: Path,
    root: Path,
    chunk_tokens: int,
    overlap_tokens: int,
) -> list[Chunk]:
    """Turn loaded sections into labeled, token-sized chunks."""
    source_file = source_path.name
    file_type = source_path.suffix.lower().lstrip(".")
    try:
        rel = str(source_path.relative_to(root))
    except ValueError:
        rel = str(source_path)

    # First pass: expand every section into raw (text, location, extra) pieces.
    raw: list[tuple[str, str, dict]] = []
    for section in sections:
        pieces = _split_tokens(section.text, chunk_tokens, overlap_tokens)
        for piece in pieces:
            if piece.strip():
                raw.append((piece, section.location, section.meta))

    total = len(raw)
    chunks: list[Chunk] = []
    for idx, (text, location, extra) in enumerate(raw):
        chunks.append(
            Chunk(
                id=_chunk_id(rel, location, idx, text),
                text=text,
                source_file=source_file,
                source_path=rel,
                file_type=file_type,
                location=location,
                chunk_index=idx,
                total_chunks=total,
                token_count=len(_ENC.encode(text)),
                extra=extra,
            )
        )
    return chunks

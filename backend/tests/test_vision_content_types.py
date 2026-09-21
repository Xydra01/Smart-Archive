"""Tests for the Smart Archive "vision-ingest" content_type feature.

These tests validate the metadata-level contract that vision-ingest introduces:
every chunk carries exactly one ``content_type`` from a fixed set, that type is
propagated faithfully from the producing ``LoadedSection``, and the pre-existing
metadata shape is preserved when vision is off.

Isolation: no test touches the real ``data/`` tree. Sections are built in
memory and any file paths use pytest's ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.ingestion.content_types import (
    CONTENT_CHART,
    CONTENT_FIGURE,
    CONTENT_OCR,
    CONTENT_TABLE,
    CONTENT_TEXT,
    CONTENT_TYPES,
)
from app.ingestion.chunker import chunk_sections
from app.ingestion.loaders import LoadedSection

# Chunking parameters kept small so short generated text still yields chunks
# without needing huge inputs; the exact sizes are irrelevant to these tests.
CHUNK_TOKENS = 64
OVERLAP_TOKENS = 8

CONTENT_TYPE_VALUES = sorted(CONTENT_TYPES)


# --------------------------------------------------------------------------
# Hypothesis strategies
# --------------------------------------------------------------------------
# Non-trivial text: at least one non-whitespace character so the chunker keeps
# the piece (it drops pieces whose ``.strip()`` is empty).
_text_strategy = st.text(min_size=1, max_size=400).filter(lambda s: s.strip() != "")


def _section_strategy() -> st.SearchStrategy[LoadedSection]:
    """A LoadedSection with random text and a random valid content_type."""
    return st.builds(
        lambda text, ct: LoadedSection(
            text=text,
            location="loc",
            meta={"content_type": ct},
        ),
        text=_text_strategy,
        ct=st.sampled_from(CONTENT_TYPE_VALUES),
    )


# --------------------------------------------------------------------------
# Task 1.3 / content types
# --------------------------------------------------------------------------
def test_content_types_exact_five() -> None:
    """Feature: vision-ingest, Property 3: content type set is exactly five values."""
    assert CONTENT_TYPES == {"text", "table", "chart", "figure", "ocr"}


# --------------------------------------------------------------------------
# Property 2: Every chunk has a valid content_type; non-text keep provenance
# --------------------------------------------------------------------------
@settings(max_examples=100)
@given(sections=st.lists(_section_strategy(), min_size=1, max_size=8))
def test_property_2_every_chunk_valid_content_type(
    sections: list[LoadedSection],
) -> None:
    """Feature: vision-ingest, Property 2: every chunk has a valid content_type and keeps provenance."""
    # Paths are only used to derive metadata strings; no filesystem access, so
    # a plain constructed path keeps the property test free of fixtures.
    root = Path("/tmp/vision-ingest-test")
    source_path = root / "doc.pdf"
    chunks = chunk_sections(
        sections,
        source_path=source_path,
        root=root,
        chunk_tokens=CHUNK_TOKENS,
        overlap_tokens=OVERLAP_TOKENS,
    )

    for chunk in chunks:
        md = chunk.to_metadata()
        assert md["content_type"] in CONTENT_TYPES
        # Provenance is present for every chunk, including non-text ones.
        assert "source_path" in md and md["source_path"]
        assert "location" in md
        assert "chunk_index" in md and isinstance(md["chunk_index"], int)


# --------------------------------------------------------------------------
# Property 3: content_type matches the producer
# --------------------------------------------------------------------------
@settings(max_examples=100)
@given(
    content_type=st.sampled_from(CONTENT_TYPE_VALUES),
    text=_text_strategy,
)
def test_property_3_content_type_matches_producer(content_type: str, text: str) -> None:
    """Feature: vision-ingest, Property 3: chunk content_type equals the producing section's kind."""
    section = LoadedSection(
        text=text, location="loc", meta={"content_type": content_type}
    )
    root = Path("/tmp/vision-ingest-test")
    source_path = root / "doc.pdf"
    chunks = chunk_sections(
        [section],
        source_path=source_path,
        root=root,
        chunk_tokens=CHUNK_TOKENS,
        overlap_tokens=OVERLAP_TOKENS,
    )

    assert chunks, "non-trivial text should produce at least one chunk"
    for chunk in chunks:
        assert chunk.to_metadata()["content_type"] == content_type


def test_property_3_missing_content_type_defaults_to_text() -> None:
    """Feature: vision-ingest, Property 3: a section with no content_type defaults to text."""
    section = LoadedSection(text="hello world", location="loc")
    # __post_init__ populates the default without the caller supplying one.
    assert section.meta["content_type"] == CONTENT_TEXT

    # And that default rides through to the chunk metadata.
    root = Path("/tmp")
    chunks = chunk_sections(
        [section],
        source_path=root / "doc.txt",
        root=root,
        chunk_tokens=CHUNK_TOKENS,
        overlap_tokens=OVERLAP_TOKENS,
    )
    assert chunks
    assert all(c.to_metadata()["content_type"] == CONTENT_TEXT for c in chunks)


# --------------------------------------------------------------------------
# Property 1: Vision off is behavior-preserving (metadata-only delta)
# --------------------------------------------------------------------------
# The metadata fields that must always be present and unchanged in shape when
# no content_type is supplied (i.e. the pre-feature invariant plus the added
# content_type == "text").
_REQUIRED_METADATA_FIELDS = {
    "source_file",
    "source_path",
    "file_type",
    "location",
    "chunk_index",
    "total_chunks",
    "token_count",
}


@settings(max_examples=100)
@given(
    texts=st.lists(_text_strategy, min_size=1, max_size=6),
)
def test_property_1_vision_off_metadata_shape_preserved(
    texts: list[str],
) -> None:
    """Feature: vision-ingest, Property 1: vision off preserves metadata shape; content_type defaults to text."""
    # Build sections WITHOUT any content_type key in meta.
    sections = [
        LoadedSection(text=t, location=f"loc-{i}", meta={}) for i, t in enumerate(texts)
    ]
    root = Path("/tmp/vision-ingest-test")
    source_path = root / "doc.pdf"
    chunks = chunk_sections(
        sections,
        source_path=source_path,
        root=root,
        chunk_tokens=CHUNK_TOKENS,
        overlap_tokens=OVERLAP_TOKENS,
    )

    assert chunks
    for chunk in chunks:
        md = chunk.to_metadata()
        # The only content-type-related metadata is content_type == "text".
        assert md["content_type"] == CONTENT_TEXT
        # No other content-type dimension leaked in.
        assert not any(k != "content_type" and k.startswith("content_type") for k in md)
        # All pre-feature fields present and of the expected scalar shapes.
        assert _REQUIRED_METADATA_FIELDS.issubset(md.keys())
        assert isinstance(md["source_file"], str)
        assert isinstance(md["source_path"], str)
        assert isinstance(md["file_type"], str)
        assert isinstance(md["location"], str)
        assert isinstance(md["chunk_index"], int)
        assert isinstance(md["total_chunks"], int)
        assert isinstance(md["token_count"], int)


# --------------------------------------------------------------------------
# Property 7: Native tables preserve structure + type (vision off)
# --------------------------------------------------------------------------
class _FakePage:
    """A stand-in pdfplumber page yielding one table and some body text."""

    def extract_tables(self):
        return [[["A", "B"], ["1", "2"]]]

    def extract_text(self):
        return "body text"


class _FakePDF:
    """A stand-in pdfplumber document exposing a single fake page."""

    def __init__(self) -> None:
        self.pages = [_FakePage()]

    def __enter__(self) -> "_FakePDF":
        return self

    def __exit__(self, *exc) -> bool:
        return False


class _FakePdfplumberModule:
    """Minimal fake for the ``pdfplumber`` module used inside load_pdf."""

    @staticmethod
    def open(_path):
        return _FakePDF()


def test_property_7_native_table_structure_and_type(
    monkeypatch, tmp_path: Path
) -> None:
    """Feature: vision-ingest, Property 7: native PDF tables preserve row structure and content_type table."""
    # load_pdf does ``import pdfplumber`` locally, so inject a fake into
    # sys.modules to intercept it without adding a real dependency. Vision is
    # never enabled or referenced here.
    import sys

    monkeypatch.setitem(sys.modules, "pdfplumber", _FakePdfplumberModule())

    from app.ingestion.loaders import load_pdf

    fake_path = tmp_path / "doc.pdf"
    sections = load_pdf(fake_path)

    table_sections = [
        s for s in sections if s.meta.get("content_type") == CONTENT_TABLE
    ]
    text_sections = [s for s in sections if s.meta.get("content_type") == CONTENT_TEXT]

    assert table_sections, "expected a table section from the fake page"
    table = table_sections[0]
    # Row structure preserved: each row rendered pipe-delimited, rows separated.
    assert "A | B" in table.text
    assert "1 | 2" in table.text

    assert text_sections, "expected a text section from the fake page"
    assert any("body text" in s.text for s in text_sections)

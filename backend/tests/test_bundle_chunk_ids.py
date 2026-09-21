"""Property-based tests for the portable-index-bundles content-based chunk id.

These pin down the three id-derivation properties from the design doc:

  * Property 1 — the id is path-independent (depends on the file *name*, not
    its directory), so identical content dedups across machine layouts.
  * Property 2 — a document's text-chunk ids are the same whether or not vision
    ran, because vision only *appends* new sections.
  * Property 3 — chunks that differ in text/location/content_type get distinct
    ids, and genuine duplicates within one source are disambiguated by an
    intra-source occurrence counter.

Everything is pure and in-memory: ``chunk_sections`` only derives strings from
the Paths it is given, so we construct Paths without ever touching the disk.
"""

from __future__ import annotations

from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from app.ingestion.chunker import _chunk_id, chunk_sections
from app.ingestion.content_types import (
    CONTENT_CHART,
    CONTENT_FIGURE,
    CONTENT_TEXT,
)
from app.ingestion.loaders import LoadedSection

# Chunk sizing: big enough that our short (<400 char) texts never split, so each
# section deterministically yields exactly one chunk.
CHUNK_TOKENS = 512
OVERLAP_TOKENS = 64

# Short, mostly-printable text that is guaranteed non-blank after .strip() (the
# chunker drops blank pieces), and short enough to stay a single chunk.
_text = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126),
    min_size=1,
    max_size=120,
).filter(lambda s: s.strip() != "")

# Location labels resembling "p. 3", "Chapter 2", etc.
_location = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126),
    min_size=1,
    max_size=40,
).filter(lambda s: s.strip() != "")

_vision_type = st.sampled_from([CONTENT_CHART, CONTENT_FIGURE])


def _text_section(text: str, location: str) -> LoadedSection:
    """A plain text section (content_type defaults to 'text')."""
    return LoadedSection(text=text, location=location, meta={})


def _vision_section(text: str, location: str, content_type: str) -> LoadedSection:
    return LoadedSection(
        text=text, location=location, meta={"content_type": content_type}
    )


def _ids(sections, source_path: Path, root: Path) -> list[str]:
    chunks = chunk_sections(
        sections,
        source_path=source_path,
        root=root,
        chunk_tokens=CHUNK_TOKENS,
        overlap_tokens=OVERLAP_TOKENS,
    )
    return [c.id for c in chunks]


# ---------------------------------------------------------------------------
# Property 1 — content-based id is path-independent
# ---------------------------------------------------------------------------
@settings(max_examples=100)
@given(
    entries=st.lists(
        st.tuples(_text, _location),
        min_size=1,
        max_size=8,
    )
)
def test_chunk_id_is_path_independent(entries):
    """Feature: portable-index-bundles, Property 1: content-based id is path-independent"""
    sections = [_text_section(text, loc) for text, loc in entries]

    # Same file name, different directories and different roots.
    ids_a = _ids(list(sections), Path("/a/x/book.pdf"), Path("/a/x"))
    ids_b = _ids(list(sections), Path("/z/deep/nested/book.pdf"), Path("/z"))

    assert ids_a == ids_b


@settings(max_examples=100)
@given(
    source_file=st.sampled_from(["book.pdf", "report.pdf", "notes.docx"]),
    other_file=st.sampled_from(["other.pdf", "book2.pdf", "notes.txt"]),
    location=_location,
    content_type=st.sampled_from([CONTENT_TEXT, CONTENT_CHART, CONTENT_FIGURE]),
    text=_text,
    occurrence=st.integers(min_value=0, max_value=5),
)
def test_chunk_id_direct_depends_only_on_filename(
    source_file, other_file, location, content_type, text, occurrence
):
    """Feature: portable-index-bundles, Property 1: content-based id is path-independent"""
    base = _chunk_id(source_file, location, content_type, text, occurrence)

    # Same inputs -> same id (deterministic, no hidden path/global state).
    assert base == _chunk_id(source_file, location, content_type, text, occurrence)

    # A different file *name* changes the id.
    if other_file != source_file:
        assert base != _chunk_id(
            other_file, location, content_type, text, occurrence
        )


def test_chunk_id_unit_filename_sensitivity():
    """Feature: portable-index-bundles, Property 1: content-based id is path-independent"""
    a = _chunk_id("book.pdf", "p.1", "text", "hello", 0)
    assert a == _chunk_id("book.pdf", "p.1", "text", "hello", 0)
    # Only the file name differs -> different id.
    assert a != _chunk_id("other.pdf", "p.1", "text", "hello", 0)


# ---------------------------------------------------------------------------
# Property 2 — text-chunk ids are vision-invariant
# ---------------------------------------------------------------------------
@settings(max_examples=100)
@given(
    text_entries=st.lists(
        st.tuples(_text, _location),
        min_size=1,
        max_size=8,
    ),
    vision_entries=st.lists(
        st.tuples(_text, _location, _vision_type),
        min_size=1,
        max_size=6,
    ),
)
def test_text_chunk_ids_are_vision_invariant(text_entries, vision_entries):
    """Feature: portable-index-bundles, Property 2: text-chunk ids are vision-invariant"""
    source_path = Path("/lib/book.pdf")
    root = Path("/lib")

    text_sections = [_text_section(t, loc) for t, loc in text_entries]
    vision_sections = [
        _vision_section(t, loc, ct) for t, loc, ct in vision_entries
    ]

    # Run 1: text only. Run 2: same text sections plus appended vision sections.
    chunks_text_only = chunk_sections(
        list(text_sections),
        source_path=source_path,
        root=root,
        chunk_tokens=CHUNK_TOKENS,
        overlap_tokens=OVERLAP_TOKENS,
    )
    chunks_with_vision = chunk_sections(
        list(text_sections) + list(vision_sections),
        source_path=source_path,
        root=root,
        chunk_tokens=CHUNK_TOKENS,
        overlap_tokens=OVERLAP_TOKENS,
    )

    text_ids_only = {
        c.id for c in chunks_text_only if c.extra.get("content_type") == CONTENT_TEXT
    }
    text_ids_with_vision = {
        c.id
        for c in chunks_with_vision
        if c.extra.get("content_type") == CONTENT_TEXT
    }

    # The set of text-chunk ids is identical whether or not vision ran.
    assert text_ids_only == text_ids_with_vision

    # The vision run has strictly more ids overall (vision only adds).
    all_ids_only = {c.id for c in chunks_text_only}
    all_ids_with_vision = {c.id for c in chunks_with_vision}
    assert len(all_ids_with_vision) > len(all_ids_only)
    assert all_ids_only <= all_ids_with_vision


# ---------------------------------------------------------------------------
# Property 3 — distinct chunks keep distinct ids; duplicates disambiguated
# ---------------------------------------------------------------------------
@settings(max_examples=100)
@given(
    a=st.tuples(_text, _location, st.sampled_from([CONTENT_TEXT, CONTENT_CHART])),
    b=st.tuples(_text, _location, st.sampled_from([CONTENT_TEXT, CONTENT_FIGURE])),
)
def test_distinct_chunks_get_distinct_ids(a, b):
    """Feature: portable-index-bundles, Property 3: distinct chunks keep distinct ids"""
    # Only exercise the property when the two differ in at least one of
    # text / location / content_type.
    if a == b:
        return
    id_a = _chunk_id(source_file="book.pdf", location=a[1], content_type=a[2], text=a[0], occurrence=0)
    id_b = _chunk_id(source_file="book.pdf", location=b[1], content_type=b[2], text=b[0], occurrence=0)
    assert id_a != id_b


@settings(max_examples=100)
@given(text=_text, location=_location)
def test_duplicate_chunks_disambiguated_by_occurrence(text, location):
    """Feature: portable-index-bundles, Property 3: distinct chunks keep distinct ids"""
    # A single source with two sections that produce an identical
    # (location, content_type, text) chunk. The occurrence counter must give
    # them different ids so both survive in the store.
    sections = [
        _text_section(text, location),
        _text_section(text, location),
    ]
    chunks = chunk_sections(
        sections,
        source_path=Path("/lib/book.pdf"),
        root=Path("/lib"),
        chunk_tokens=CHUNK_TOKENS,
        overlap_tokens=OVERLAP_TOKENS,
    )
    assert len(chunks) == 2
    assert chunks[0].id != chunks[1].id

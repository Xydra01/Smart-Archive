"""Tests for effective_record and display_name (Feature: citation-formatting).

Covers tasks 3.3-3.4. Property tests use Hypothesis at >=100 examples and are
tagged with the property they validate. Both modules are pure functions with no
I/O, so no store/temp-file wiring is needed here.
"""

from __future__ import annotations

import os

from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st

from app.references.display_name import display_name
from app.references.effective import effective_record
from app.references.store import SourceMetadata

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
# Path segments that always have a non-empty basename so the fallbacks yield a
# non-empty title. We avoid trailing separators for the "final segment" cases.
segment = st.text(alphabet="abcdefgABCDEF0123456789", min_size=1, max_size=10)
extension = st.sampled_from(["", ".pdf", ".txt", ".md", ".HTML", ".tar.gz"])


@st.composite
def source_paths(draw):
    """A source_path with a non-empty final segment (optionally nested)."""
    depth = draw(st.integers(min_value=0, max_value=3))
    parts = [draw(segment) for _ in range(depth)]
    last = draw(segment) + draw(extension)
    parts.append(last)
    return "/".join(parts)


file_names = st.builds(lambda s, e: s + e, segment, extension)
opt_file = st.one_of(st.none(), file_names)
# Titles: None, blank/whitespace-only (treated as absent), or non-blank.
blank_title = st.sampled_from(["", "   ", "\t", "\n  "])
nonblank_title = st.text(min_size=1, max_size=20).filter(lambda s: s.strip() != "")
opt_title = st.one_of(st.none(), blank_title, nonblank_title)


def _strip_ext(name: str) -> str:
    root, _ext = os.path.splitext(name)
    return root


def _final_segment(source_path: str) -> str:
    base = os.path.basename(source_path)
    if base:
        return base
    parts = [p for p in source_path.rsplit("/") if p]
    return parts[-1] if parts else source_path


# ---------------------------------------------------------------------------
# Task 3.3 / Property 4 — title fallback
# ---------------------------------------------------------------------------
@hyp_settings(max_examples=150)
@given(
    source_path=source_paths(),
    source_file=opt_file,
    stored_title=opt_title,
    source_type=st.one_of(st.none(), st.sampled_from(["book", "article", "website"])),
)
def test_title_fallback_is_correct_and_non_empty(
    source_path, source_file, stored_title, source_type
):
    """Feature: citation-formatting, Property 4: Title fallback is correct and
    non-empty."""
    stored = None
    if stored_title is not None or source_type is not None:
        stored = SourceMetadata(
            source_path=source_path,
            source_type=source_type or "other",
            title=stored_title,
        )

    eff = effective_record(source_path, stored, source_file)

    if stored_title is not None and stored_title.strip():
        # A non-blank stored title is preserved verbatim.
        assert eff.title == stored_title
    elif source_file is not None and source_file.strip():
        # source_file present -> file name minus extension.
        assert eff.title == _strip_ext(source_file)
    else:
        # Fall back to the final path segment minus extension.
        assert eff.title == _strip_ext(_final_segment(source_path))

    # Effective title is always non-empty for a normal source_path.
    assert isinstance(eff.title, str)
    assert eff.title != ""


# ---------------------------------------------------------------------------
# Task 3.4 / Property 24 — display_name resolution
# ---------------------------------------------------------------------------
@hyp_settings(max_examples=150)
@given(
    source_path=source_paths(),
    stored_title=opt_title,
    source_file=opt_file,
)
def test_display_name_is_total_and_title_preferring(
    source_path, stored_title, source_file
):
    """Feature: citation-formatting, Property 24: Display-name resolution is
    total and title-preferring."""
    result = display_name(source_path, stored_title, source_file)

    # Total: always a non-empty string, never raises.
    assert isinstance(result, str)
    assert result != ""

    if stored_title is not None and stored_title.strip():
        # Non-whitespace stored title -> that title, trimmed.
        assert result == stored_title.strip()
    elif source_file is not None and source_file.strip():
        # Else the file name, WITH its extension (unlike the title fallback).
        assert result == source_file
    else:
        # Else the final path segment (extension kept).
        assert result == _final_segment(source_path)

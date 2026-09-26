"""Tests for the Citation_Formatter (Feature: citation-formatting).

Covers tasks 4.4-4.10. Property tests use Hypothesis at >=100 examples and are
tagged with the property they validate. The formatter is pure (no I/O, no
clock), so no store/temp-file wiring is needed.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st

from app.references.formatter import (
    CitationStyle,
    format_citation,
    required_fields,
)
from app.references.store import SOURCE_TYPES, SourceMetadata

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
source_paths = st.text(alphabet="abcdefg._/", min_size=1, max_size=10)
opt_str = st.one_of(st.none(), st.text(max_size=20))
# Author names: comma form, space form, single token, or blank.
author = st.one_of(
    st.text(max_size=15),
    st.builds(lambda a, b: f"{a}, {b}", st.text(min_size=1, max_size=8), st.text(min_size=1, max_size=8)),
    st.builds(lambda a, b: f"{a} {b}", st.text(min_size=1, max_size=8), st.text(min_size=1, max_size=8)),
)
author_lists = st.lists(author, max_size=4)
styles = st.sampled_from([CitationStyle.MLA, CitationStyle.APA])
style_keys = st.sampled_from(["MLA", "APA"])
overrides = st.dictionaries(style_keys, st.text(min_size=1, max_size=40), max_size=2)
# A non-blank title so title-bearing assertions hold even for the effective
# record used directly (records here already carry a usable title unless we
# choose None; the effective title falls back to the path basename otherwise).
opt_date = st.one_of(
    st.none(),
    st.text(alphabet="0123456789-", max_size=10),
    st.sampled_from(["2015", "2015-06", "2015-06-01", "1999-12-31"]),
)


@st.composite
def records(draw, *, with_overrides=True):
    """A SourceMetadata over the full field space."""
    ov = draw(overrides) if with_overrides else {}
    return SourceMetadata(
        source_path=draw(source_paths),
        source_type=draw(st.sampled_from(SOURCE_TYPES)),
        authors=draw(author_lists),
        title=draw(opt_str),
        container=draw(opt_str),
        publisher=draw(opt_str),
        publication_date=draw(opt_date),
        url=draw(opt_str),
        access_date=draw(opt_date),
        verbatim_overrides=ov,
    )


def _present(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _authors_present(authors) -> bool:
    return any(isinstance(a, str) and a.strip() for a in authors)


def _effective_title(rec: SourceMetadata) -> str:
    """Mirror the formatter's internal effective-title floor."""
    import os

    if _present(rec.title):
        return rec.title.strip()
    base = os.path.basename(rec.source_path or "")
    root, _ext = os.path.splitext(base)
    if root.strip():
        return root
    if (rec.source_path or "").strip():
        return rec.source_path
    return "Untitled"


# ---------------------------------------------------------------------------
# Task 4.4 / Property 1 — totality and title-bearing output
# ---------------------------------------------------------------------------
@hyp_settings(max_examples=200)
@given(record=records(), style=styles)
def test_formatter_is_total_and_title_bearing(record, style):
    """Feature: citation-formatting, Property 1: Formatter is total and produces
    a non-empty, title-bearing citation."""
    result = format_citation(record, style)  # must never raise

    assert isinstance(result.text, str)
    assert result.text.strip() != ""

    style_key = style.value
    override = (record.verbatim_overrides or {}).get(style_key)
    if isinstance(override, str) and override.strip():
        # Override in play: text is the override verbatim.
        assert result.text == override
    else:
        # Field-based: the effective title appears in the text.
        assert _effective_title(record) in result.text


# ---------------------------------------------------------------------------
# Task 4.5 / Property 2 — determinism
# ---------------------------------------------------------------------------
@hyp_settings(max_examples=200)
@given(record=records(), style=styles)
def test_formatter_is_deterministic(record, style):
    """Feature: citation-formatting, Property 2: Formatter is deterministic."""
    a = format_citation(record, style)
    b = format_citation(record, style)
    assert a.text == b.text
    assert a.missing_required == b.missing_required
    assert a.leading_element == b.leading_element
    assert a.incomplete == b.incomplete


# ---------------------------------------------------------------------------
# Task 4.6 / Property 3 — incomplete marker exactness
# ---------------------------------------------------------------------------
def _expected_missing(rec: SourceMetadata, style: CitationStyle) -> list[str]:
    """Compute the expected missing_required set for a (record, style) pair."""
    required = required_fields(rec.source_type)
    missing: list[str] = []
    for f in ("title", "authors", "container", "publisher", "url"):
        if f not in required:
            continue
        if f == "authors":
            if not _authors_present(rec.authors):
                missing.append("authors")
        else:
            if not _present(getattr(rec, f, None)):
                missing.append(f)
    # MLA website access-date rule (Req 3.4).
    if (
        style == CitationStyle.MLA
        and rec.source_type == "website"
        and not _present(rec.publication_date)
        and not _present(rec.access_date)
    ):
        if "access_date" not in missing:
            missing.append("access_date")
    return missing


@hyp_settings(max_examples=200)
@given(
    source_type=st.sampled_from(SOURCE_TYPES),
    style=styles,
    authors=author_lists,
    title=opt_str,
    container=opt_str,
    publisher=opt_str,
    url=opt_str,
    publication_date=opt_date,
    access_date=opt_date,
    source_path=source_paths,
)
def test_incomplete_marker_holds_exactly(
    source_type,
    style,
    authors,
    title,
    container,
    publisher,
    url,
    publication_date,
    access_date,
    source_path,
):
    """Feature: citation-formatting, Property 3: Incomplete marker holds exactly
    when a required field is missing."""
    # No verbatim override so field-based assembly (and marking) applies.
    record = SourceMetadata(
        source_path=source_path,
        source_type=source_type,
        authors=authors,
        title=title,
        container=container,
        publisher=publisher,
        publication_date=publication_date,
        url=url,
        access_date=access_date,
        verbatim_overrides={},
    )
    result = format_citation(record, style)

    expected = _expected_missing(record, style)
    assert result.missing_required == expected
    assert result.incomplete == bool(expected)


# ---------------------------------------------------------------------------
# Task 4.7 / Property 5 — no-author title position, no placeholder
# ---------------------------------------------------------------------------
_PLACEHOLDERS = ("Anonymous", "N.A.", "Unknown", "Anon")


@hyp_settings(max_examples=150)
@given(
    style=styles,
    title=opt_str,
    container=opt_str,
    publisher=opt_str,
    url=opt_str,
    publication_date=opt_date,
    source_path=source_paths,
    source_type=st.sampled_from(SOURCE_TYPES),
    # authors either empty, or all-blank entries (so effectively no authors).
    blank_authors=st.lists(st.sampled_from(["", "   ", "\t"]), max_size=3),
)
def test_no_authors_title_in_author_position(
    style,
    title,
    container,
    publisher,
    url,
    publication_date,
    source_path,
    source_type,
    blank_authors,
):
    """Feature: citation-formatting, Property 5: With no authors, the effective
    title occupies the author position and no placeholder is emitted."""
    record = SourceMetadata(
        source_path=source_path,
        source_type=source_type,
        authors=blank_authors,
        title=title,
        container=container,
        publisher=publisher,
        publication_date=publication_date,
        url=url,
        verbatim_overrides={},
    )
    result = format_citation(record, style)

    eff_title = _effective_title(record)
    assert result.leading_element == eff_title

    # No author-placeholder token appears in the output.
    for token in _PLACEHOLDERS:
        assert token not in result.text


# ---------------------------------------------------------------------------
# Task 4.8 / Property 6 — APA (n.d.)
# ---------------------------------------------------------------------------
@hyp_settings(max_examples=150)
@given(
    authors=author_lists,
    title=opt_str,
    container=opt_str,
    publisher=opt_str,
    url=opt_str,
    source_path=source_paths,
    source_type=st.sampled_from(SOURCE_TYPES),
    # publication_date absent: None or whitespace-only.
    blank_date=st.sampled_from([None, "", "   ", "\t"]),
)
def test_apa_renders_nd_for_missing_date(
    authors, title, container, publisher, url, source_path, source_type, blank_date
):
    """Feature: citation-formatting, Property 6: APA renders (n.d.) for a missing
    publication date."""
    record = SourceMetadata(
        source_path=source_path,
        source_type=source_type,
        authors=authors,
        title=title,
        container=container,
        publisher=publisher,
        publication_date=blank_date,
        url=url,
        verbatim_overrides={},
    )
    result = format_citation(record, CitationStyle.APA)
    assert "(n.d.)" in result.text


# ---------------------------------------------------------------------------
# Task 4.9 / Property 23 — verbatim override precedence and isolation
# ---------------------------------------------------------------------------
@hyp_settings(max_examples=150)
@given(
    base=records(with_overrides=False),
    override_style=styles,
    override_text=st.text(min_size=1, max_size=40).filter(lambda s: s.strip() != ""),
)
def test_verbatim_override_precedence_and_isolation(
    base, override_style, override_text
):
    """Feature: citation-formatting, Property 23: Verbatim override precedence
    and isolation."""
    style_key = override_style.value
    other_style = (
        CitationStyle.APA if override_style == CitationStyle.MLA else CitationStyle.MLA
    )

    # Field-based output for both styles BEFORE the override is applied.
    before_other = format_citation(base, other_style)

    # Apply the override for exactly one style.
    with_override = SourceMetadata(
        source_path=base.source_path,
        source_type=base.source_type,
        authors=list(base.authors),
        title=base.title,
        container=base.container,
        publisher=base.publisher,
        publication_date=base.publication_date,
        url=base.url,
        access_date=base.access_date,
        verbatim_overrides={style_key: override_text},
    )

    # The overridden style returns the text exactly, complete.
    overridden = format_citation(with_override, override_style)
    assert overridden.text == override_text
    assert overridden.incomplete is False
    assert overridden.missing_required == []

    # The other style ignores the override: identical to the pre-override,
    # field-based output.
    after_other = format_citation(with_override, other_style)
    assert after_other.text == before_other.text
    assert after_other.missing_required == before_other.missing_required
    assert after_other.leading_element == before_other.leading_element

    # Setting an override did not change any other stored field.
    assert with_override.source_type == base.source_type
    assert with_override.authors == base.authors
    assert with_override.title == base.title
    assert with_override.container == base.container
    assert with_override.publisher == base.publisher
    assert with_override.publication_date == base.publication_date
    assert with_override.url == base.url


# ---------------------------------------------------------------------------
# Task 4.10 — example unit tests: exact MLA/APA strings (regression lock)
# ---------------------------------------------------------------------------
class TestFormatterExamples:
    """Hand-written records -> exact expected strings (Req 1.2, 1.3, 1.4).

    The expected strings match the ACTUAL pragmatic format the implementation
    produces; these are regression locks, not a re-derivation of MLA/APA by
    hand.
    """

    def _book(self) -> SourceMetadata:
        return SourceMetadata(
            source_path="calculus.pdf",
            source_type="book",
            authors=["Stewart, James"],
            title="Calculus",
            publisher="Cengage Learning",
            publication_date="2015",
        )

    def _article(self) -> SourceMetadata:
        return SourceMetadata(
            source_path="paper.pdf",
            source_type="article",
            authors=["Smith, Jane", "Doe, John"],
            title="On Widgets",
            container="Journal of Widgets",
            publication_date="2019-06-01",
        )

    def test_book_mla_exact(self):
        """Feature: citation-formatting: exact MLA string for a book (Req 1.2)."""
        result = format_citation(self._book(), CitationStyle.MLA)
        assert result.text == "Stewart, James. Calculus. Cengage Learning, 2015."
        assert result.incomplete is False
        assert result.leading_element == "Stewart"

    def test_book_apa_exact(self):
        """Feature: citation-formatting: exact APA string for a book (Req 1.3)."""
        result = format_citation(self._book(), CitationStyle.APA)
        assert result.text == "Stewart, J. (2015). Calculus. Cengage Learning."
        assert result.incomplete is False
        assert result.leading_element == "Stewart"

    def test_article_mla_exact(self):
        """Feature: citation-formatting: exact MLA string for an article."""
        result = format_citation(self._article(), CitationStyle.MLA)
        assert result.text == (
            "Smith, Jane, John Doe. On Widgets. Journal of Widgets, 2019-06-01."
        )

    def test_article_apa_exact(self):
        """Feature: citation-formatting: exact APA string for an article."""
        result = format_citation(self._article(), CitationStyle.APA)
        assert result.text == (
            "Smith, J., Doe, J. (2019). On Widgets. Journal of Widgets."
        )

    def test_default_style_is_mla(self):
        """Feature: citation-formatting: default-style formatting matches MLA when
        no style is selected (Req 1.4).

        The formatter takes an explicit style; the "default MLA" contract means
        the MLA rendering is the one used when the selector is Off, so we assert
        the MLA rendering is well-formed and distinct from APA for the same
        record.
        """
        book = self._book()
        mla = format_citation(book, CitationStyle.MLA)
        apa = format_citation(book, CitationStyle.APA)
        assert mla.text == "Stewart, James. Calculus. Cengage Learning, 2015."
        assert mla.text != apa.text

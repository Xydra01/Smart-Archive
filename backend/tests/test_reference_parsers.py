"""Tests for the Reference_Import parsers (Feature: citation-formatting).

Covers tasks 5.4-5.8. Property tests use Hypothesis at >=100 examples and are
tagged with the property they validate. Store-touching parts bind to a per-test
temp file via conftest.make_metadata_store.
"""

from __future__ import annotations

import json

import pytest
from hypothesis import assume, given, settings as hyp_settings
from hypothesis import strategies as st

from app.references.import_parsers import (
    ParsedEntry,
    ParsedVerbatim,
    ParseError,
    parse_bibtex,
    parse_csljson,
    parse_ris,
    parse_verbatim,
)

from .conftest import make_metadata_store

# ---------------------------------------------------------------------------
# Type-inference mapping tables (mirror the implementation for the assertions).
# ---------------------------------------------------------------------------
BIBTEX_TYPE = {"article": "article", "book": "book", "techreport": "report", "online": "website"}
RIS_TYPE = {"JOUR": "article", "BOOK": "book", "RPRT": "report", "WEB": "website"}
CSL_TYPE = {"article-journal": "article", "book": "book", "report": "report", "webpage": "website"}

# ---------------------------------------------------------------------------
# Field strategies for the "known field set" used by Property 20.
# ---------------------------------------------------------------------------
# Simple, delimiter-free text so the small serializers round-trip cleanly. We
# avoid characters that our test serializers use as structural delimiters
# (braces, quotes, commas, equals, and " and " author separators, newlines).
plain = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ 0123456789",
    min_size=1,
    max_size=20,
).map(str.strip).filter(lambda s: s != "" and " and " not in s)

opt_plain = st.one_of(st.none(), plain)
# Author names in "Family, Given" form so all serializers agree.
author = st.builds(
    lambda f, g: f"{f}, {g}",
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=8),
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=8),
)
authors_st = st.lists(author, max_size=3)


@st.composite
def known_fields(draw):
    """A dict of the mappable bibliographic fields, arbitrary subsets present."""
    return {
        "authors": draw(authors_st),
        "title": draw(opt_plain),
        "container": draw(opt_plain),
        "publisher": draw(opt_plain),
        "url": draw(opt_plain),
        "publication_date": draw(
            st.one_of(st.none(), st.sampled_from(["2015", "2015-06", "2015-06-01"]))
        ),
    }


# ---------------------------------------------------------------------------
# Small serializers for each structured format (test-only).
# ---------------------------------------------------------------------------
def _date_parts(date: str) -> list[int]:
    return [int(p) for p in date.split("-")]


def render_bibtex(fields: dict, entry_type: str, key: str) -> str:
    lines = [f"@{entry_type}{{{key},"]
    if fields["authors"]:
        lines.append(f'  author = {{{" and ".join(fields["authors"])}}},')
    if fields["title"] is not None:
        lines.append(f'  title = {{{fields["title"]}}},')
    if fields["container"] is not None:
        lines.append(f'  journal = {{{fields["container"]}}},')
    if fields["publisher"] is not None:
        lines.append(f'  publisher = {{{fields["publisher"]}}},')
    if fields["url"] is not None:
        lines.append(f'  url = {{{fields["url"]}}},')
    if fields["publication_date"] is not None:
        lines.append(f'  date = {{{fields["publication_date"]}}},')
    lines.append("}")
    return "\n".join(lines)


def render_ris(fields: dict, ty: str) -> str:
    lines = [f"TY  - {ty}"]
    for a in fields["authors"]:
        lines.append(f"AU  - {a}")
    if fields["title"] is not None:
        lines.append(f'TI  - {fields["title"]}')
    if fields["container"] is not None:
        lines.append(f'JO  - {fields["container"]}')
    if fields["publisher"] is not None:
        lines.append(f'PB  - {fields["publisher"]}')
    if fields["url"] is not None:
        lines.append(f'UR  - {fields["url"]}')
    if fields["publication_date"] is not None:
        lines.append(f'DA  - {fields["publication_date"]}')
    lines.append("ER  - ")
    return "\n".join(lines)


def render_csljson(fields: dict, csl_type: str, key: str) -> str:
    obj: dict = {"id": key, "type": csl_type}
    if fields["authors"]:
        obj["author"] = [
            {"family": a.split(", ")[0], "given": a.split(", ")[1]}
            for a in fields["authors"]
        ]
    if fields["title"] is not None:
        obj["title"] = fields["title"]
    if fields["container"] is not None:
        obj["container-title"] = fields["container"]
    if fields["publisher"] is not None:
        obj["publisher"] = fields["publisher"]
    if fields["url"] is not None:
        obj["URL"] = fields["url"]
    if fields["publication_date"] is not None:
        obj["issued"] = {"date-parts": [_date_parts(fields["publication_date"])]}
    return json.dumps([obj])


def _assert_entry_matches(entry: ParsedEntry, fields: dict, expected_type):
    """Assert a ParsedEntry reflects the known field set."""
    assert entry.source_type == expected_type
    assert entry.authors == fields["authors"]
    # Omitted fields map to absent (None).
    assert entry.title == fields["title"]
    assert entry.container == fields["container"]
    assert entry.publisher == fields["publisher"]
    assert entry.url == fields["url"]
    assert entry.publication_date == fields["publication_date"]


# ---------------------------------------------------------------------------
# Task 5.4 / Property 20 — structured field mapping and type inference
# ---------------------------------------------------------------------------
@hyp_settings(max_examples=150)
@given(fields=known_fields(), bib_type=st.sampled_from(sorted(BIBTEX_TYPE)))
def test_bibtex_maps_fields_and_infers_type(fields, bib_type):
    """Feature: citation-formatting, Property 20: Structured parse maps entry
    fields and infers source_type."""
    text = render_bibtex(fields, bib_type, "key1")
    result = parse_bibtex(text)
    assert isinstance(result, list)
    assert len(result) == 1
    _assert_entry_matches(result[0], fields, BIBTEX_TYPE[bib_type])


@hyp_settings(max_examples=150)
@given(fields=known_fields(), ris_type=st.sampled_from(sorted(RIS_TYPE)))
def test_ris_maps_fields_and_infers_type(fields, ris_type):
    """Feature: citation-formatting, Property 20: Structured parse maps entry
    fields and infers source_type."""
    text = render_ris(fields, ris_type)
    result = parse_ris(text)
    assert isinstance(result, list)
    assert len(result) == 1
    _assert_entry_matches(result[0], fields, RIS_TYPE[ris_type])


@hyp_settings(max_examples=150)
@given(fields=known_fields(), csl_type=st.sampled_from(sorted(CSL_TYPE)))
def test_csljson_maps_fields_and_infers_type(fields, csl_type):
    """Feature: citation-formatting, Property 20: Structured parse maps entry
    fields and infers source_type."""
    text = render_csljson(fields, csl_type, "key1")
    result = parse_csljson(text)
    assert isinstance(result, list)
    assert len(result) == 1
    _assert_entry_matches(result[0], fields, CSL_TYPE[csl_type])


def test_unknown_type_maps_to_other_and_no_indicator_is_none():
    """Feature: citation-formatting, Property 20: unknown indicator -> other;
    no indicator -> source_type None (leave existing unchanged)."""
    # Unknown BibTeX type -> "other".
    res = parse_bibtex("@thesis{k, title = {T}}")
    assert isinstance(res, list) and res[0].source_type == "other"
    # RIS without a TY tag -> no indicator -> None.
    res = parse_ris("TI  - Something\nER  - ")
    assert isinstance(res, list) and res[0].source_type is None
    # CSL without a type -> None.
    res = parse_csljson('[{"id": "x", "title": "T"}]')
    assert isinstance(res, list) and res[0].source_type is None


# ---------------------------------------------------------------------------
# Task 5.5 / Property 21 — parsers are total and never raise
# ---------------------------------------------------------------------------
noisy_text = st.one_of(
    st.text(max_size=200),
    st.binary(max_size=200).map(lambda b: b.decode("latin-1")),
)


@hyp_settings(max_examples=200)
@given(text=noisy_text)
def test_parsers_are_total_and_never_raise(text):
    """Feature: citation-formatting, Property 21: Parsers are total and never
    raise."""
    for parser in (parse_bibtex, parse_ris, parse_csljson):
        result = parser(text)
        assert isinstance(result, (list, ParseError))
        if isinstance(result, list):
            assert all(isinstance(e, ParsedEntry) for e in result)

    for style in ("MLA", "APA", "bogus"):
        vresult = parse_verbatim(text, style)
        assert isinstance(vresult, (ParsedVerbatim, ParseError))


# ---------------------------------------------------------------------------
# Task 5.6 / Property 22 — date normalization preserves precision
# ---------------------------------------------------------------------------
@hyp_settings(max_examples=150)
@given(
    year=st.integers(min_value=1000, max_value=2999),
    month=st.integers(min_value=1, max_value=12),
    day=st.integers(min_value=1, max_value=28),
    precision=st.sampled_from(["ymd", "ym", "y"]),
)
def test_date_normalization_preserves_precision(year, month, day, precision):
    """Feature: citation-formatting, Property 22: Date normalization preserves
    precision."""
    if precision == "ymd":
        given_date = f"{year:04d}-{month:02d}-{day:02d}"
        expected = f"{year:04d}-{month:02d}-{day:02d}"
    elif precision == "ym":
        given_date = f"{year:04d}-{month:02d}"
        expected = f"{year:04d}-{month:02d}"
    else:
        given_date = f"{year:04d}"
        expected = f"{year:04d}"

    fields = {
        "authors": [],
        "title": "T",
        "container": None,
        "publisher": None,
        "url": None,
        "publication_date": given_date,
    }

    # Each format round-trips the date at the given precision, no fabrication.
    bib = parse_bibtex(render_bibtex(fields, "book", "k"))
    assert isinstance(bib, list) and bib[0].publication_date == expected

    ris = parse_ris(render_ris(fields, "BOOK"))
    assert isinstance(ris, list) and ris[0].publication_date == expected

    csl_obj = {"id": "k", "type": "book", "title": "T",
               "issued": {"date-parts": [_date_parts(given_date)]}}
    csl = parse_csljson(json.dumps([csl_obj]))
    assert isinstance(csl, list) and csl[0].publication_date == expected


# ---------------------------------------------------------------------------
# Task 5.7 / Property 25 — replaceable / idempotent one-at-a-time import
# ---------------------------------------------------------------------------
def _entry_fields(entry: ParsedEntry) -> dict:
    """The mappable fields of a ParsedEntry, dropping absent ones and any
    None source_type (which means "leave existing unchanged")."""
    fields: dict = {"authors": entry.authors}
    for name in ("title", "container", "publisher", "publication_date", "url"):
        value = getattr(entry, name)
        if value is not None:
            fields[name] = value
    if entry.source_type is not None:
        fields["source_type"] = entry.source_type
    return fields


@hyp_settings(max_examples=100)
@given(fields=known_fields(), bib_type=st.sampled_from(sorted(BIBTEX_TYPE)))
def test_import_is_idempotent(tmp_path_factory, fields, bib_type):
    """Feature: citation-formatting, Property 25: Import is replaceable and
    idempotent, one entry at a time (idempotent re-import)."""
    path = tmp_path_factory.mktemp("meta") / "source_metadata.json"
    store = make_metadata_store(path)

    parsed = parse_bibtex(render_bibtex(fields, bib_type, "k1"))
    assert isinstance(parsed, list)
    entry_fields = _entry_fields(parsed[0])

    sp = "some/source.pdf"
    first = store.upsert(sp, entry_fields)
    after_first = store.all()[sp]

    # Re-import the same parsed entry: idempotent.
    store.upsert(sp, entry_fields)
    after_second = store.all()[sp]
    assert after_second == after_first


@hyp_settings(max_examples=100)
@given(
    text_a=st.text(min_size=1, max_size=30).filter(lambda s: s.strip() != ""),
    text_b=st.text(min_size=1, max_size=30).filter(lambda s: s.strip() != ""),
    style=st.sampled_from(["MLA", "APA"]),
)
def test_verbatim_override_replaces_and_isolates(tmp_path_factory, text_a, text_b, style):
    """Feature: citation-formatting, Property 25: a second override for a style
    replaces the first and leaves the other style untouched."""
    path = tmp_path_factory.mktemp("meta") / "source_metadata.json"
    store = make_metadata_store(path)
    other = "APA" if style == "MLA" else "MLA"
    sp = "doc.pdf"

    # Seed the other style so we can prove isolation.
    store.upsert(sp, {"verbatim_overrides": {other: "OTHER-STYLE-TEXT"}})
    store.upsert(sp, {"verbatim_overrides": {style: text_a}})
    assert store.all()[sp].verbatim_overrides[style] == text_a

    # A second override for the same style replaces the first.
    store.upsert(sp, {"verbatim_overrides": {style: text_b}})
    rec = store.all()[sp]
    assert rec.verbatim_overrides[style] == text_b
    # The other style's override is untouched.
    assert rec.verbatim_overrides[other] == "OTHER-STYLE-TEXT"


@hyp_settings(max_examples=100)
@given(
    fields_a=known_fields(),
    fields_b=known_fields(),
)
def test_multi_entry_files_yield_distinct_keys(fields_a, fields_b):
    """Feature: citation-formatting, Property 25: multi-entry files return every
    entry with a distinct key."""
    text = "\n".join(
        [
            render_bibtex(fields_a, "book", "keyA"),
            render_bibtex(fields_b, "article", "keyB"),
        ]
    )
    result = parse_bibtex(text)
    assert isinstance(result, list)
    assert len(result) == 2
    keys = [e.key for e in result]
    assert keys == ["keyA", "keyB"]
    assert len(set(keys)) == 2


# ---------------------------------------------------------------------------
# Task 5.8-adjacent — parse_verbatim example/unit tests
# ---------------------------------------------------------------------------
class TestParseVerbatim:
    def test_valid_mla(self):
        """Feature: citation-formatting: a valid MLA string wraps as
        ParsedVerbatim."""
        text = 'Stewart, James. Calculus. Cengage Learning, 2015.'
        result = parse_verbatim(text, "MLA")
        assert isinstance(result, ParsedVerbatim)
        assert result.style == "MLA"
        assert result.text == text

    def test_valid_apa(self):
        """Feature: citation-formatting: a valid APA string wraps as
        ParsedVerbatim."""
        text = "Stewart, J. (2015). Calculus (8th ed.). Cengage Learning."
        result = parse_verbatim(text, "APA")
        assert isinstance(result, ParsedVerbatim)
        assert result.style == "APA"
        assert result.text == text

    def test_blank_text_is_parse_error(self):
        """Feature: citation-formatting: blank verbatim text -> ParseError."""
        for blank in ("", "   ", "\n\t "):
            result = parse_verbatim(blank, "MLA")
            assert isinstance(result, ParseError)
            assert result.format == "verbatim"

    def test_bad_style_is_parse_error(self):
        """Feature: citation-formatting: an unsupported style -> ParseError."""
        result = parse_verbatim("Some citation.", "Chicago")
        assert isinstance(result, ParseError)
        assert result.format == "verbatim"

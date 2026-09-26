"""API tests for the citation-formatting Metadata_API endpoints.

Covers tasks 8.6-8.12 of the citation-formatting spec: the FastAPI
``/api/references*`` endpoints (list / get / update / format / import).

Isolation strategy (no real Chroma index, no real ``data/source_metadata.json``)
-------------------------------------------------------------------------------
The endpoints read the set of indexed sources via ``app.main.get_store()``'s
``.sources()`` and read/write per-source metadata via
``app.main.get_source_metadata_store()``. We drive them through a FastAPI
``TestClient`` with both of those monkeypatched:

* ``main.get_store`` -> a ``FakeStore`` whose ``.sources()`` returns a controlled
  ``dict[source_path, chunk_count]`` (the "indexed sources"). The format/build
  paths only ever call ``.sources()``; they never touch real chunks.
* ``main.get_source_metadata_store`` -> a ``SourceMetadataStore`` bound to a
  per-test temp file via ``conftest.make_metadata_store`` (never the real file).
  The other modules that imported the singleton (``app.references.store``,
  ``app.indexing.indexer``, ``app.llm.rag``) are patched to the same instance so
  nothing leaks to real data.

Hypothesis-based property tests build the temp-bound store and stub inside a
``MonkeyPatch.context()`` (the pytest ``monkeypatch`` fixture is not reset
between generated examples), mirroring ``test_rag_scope.py``.
"""

from __future__ import annotations

import contextlib
import json
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st
from _pytest.monkeypatch import MonkeyPatch

import app.main as main
import app.references.store as store_mod
import app.indexing.indexer as indexer_mod
import app.llm.rag as rag_mod

from app.references.store import SOURCE_TYPES

from .conftest import make_metadata_store


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------
class FakeStore:
    """Stand-in for VectorStore exposing only ``.sources()`` (what these
    endpoints touch). ``sources()`` returns the controlled indexed set."""

    def __init__(self, sources: dict[str, int] | None = None) -> None:
        self._sources = dict(sources or {})

    def sources(self) -> dict[str, int]:
        return dict(self._sources)


def _wire(mp, indexed: dict[str, int], meta_store) -> None:
    """Wire a fake index and a temp-bound metadata store into every module
    that reads them, using the given MonkeyPatch (fixture or context)."""
    fake = FakeStore(indexed)
    mp.setattr(main, "get_store", lambda: fake)
    # Patch the singleton accessor everywhere it was imported so no reference
    # falls through to the real data/source_metadata.json.
    mp.setattr(main, "get_source_metadata_store", lambda: meta_store)
    mp.setattr(store_mod, "get_source_metadata_store", lambda: meta_store)
    mp.setattr(indexer_mod, "get_source_metadata_store", lambda: meta_store)
    mp.setattr(rag_mod, "get_source_metadata_store", lambda: meta_store)


@contextlib.contextmanager
def temp_metadata_store(mp):
    """Yield a fresh temp-bound SourceMetadataStore and its on-disk path.

    Used inside ``@given`` tests instead of the function-scoped ``tmp_path``
    fixture, which Hypothesis refuses to reset between generated examples. A
    unique temp directory per call keeps every example isolated.
    """
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "source_metadata.json"
        yield make_metadata_store(path), path


@pytest.fixture
def client() -> TestClient:
    return TestClient(main.app)


# A path-like alphabet keeps generated source_path values realistic while
# staying URL-safe for the ``{source_path:path}`` routes (no ?, #, spaces).
_PATH_ALPHABET = "abcdef0123456789._-"


_segment = st.text(alphabet=_PATH_ALPHABET, min_size=1, max_size=6).filter(
    # Exclude the RFC 3986 dot-segments: a segment of "." or ".." is collapsed
    # during URL normalization, so it would never reach the route as-is. That
    # is a URL-encoding artifact, not part of what these endpoints validate.
    lambda s: s not in (".", "..")
)


@st.composite
def source_path(draw) -> str:
    """A non-empty, URL-safe source_path with 1..3 non-dot segments."""
    segments = draw(st.lists(_segment, min_size=1, max_size=3))
    return "/".join(segments)


def _read_bytes(path: Path) -> bytes | None:
    """Capture a file's exact bytes, or ``None`` when it does not exist."""
    return path.read_bytes() if path.exists() else None


# ===========================================================================
# Property 10 — List-all covers exactly the indexed sources, each effective
# ===========================================================================
@settings(max_examples=100)
@given(
    indexed=st.dictionaries(
        source_path(), st.integers(min_value=1, max_value=50), max_size=8
    )
)
def test_list_all_covers_exactly_indexed_sources(indexed) -> None:
    """Feature: citation-formatting, Property 10: List-all covers exactly the indexed sources, each effective.

    ``GET /api/references`` returns one entry per indexed source_path (exactly
    the set from ``get_store().sources()``), each an effective record with a
    derived ``display_name`` / ``missing_required`` / ``is_complete``; an empty
    index yields ``{"references": []}``. Validates Req 4.1, 4.2.
    """
    with MonkeyPatch.context() as mp, temp_metadata_store(mp) as (meta, _path):
        _wire(mp, indexed, meta)
        client = TestClient(main.app)

        resp = client.get("/api/references")
        assert resp.status_code == 200
        body = resp.json()

    refs = body["references"]
    if not indexed:
        assert refs == []
        return

    returned_paths = {r["source_path"] for r in refs}
    assert returned_paths == set(indexed)
    # Exactly one entry per indexed source (no dupes, none missing).
    assert len(refs) == len(indexed)

    for r in refs:
        # Effective/derived fields are present and internally consistent.
        assert r["source_type"] in SOURCE_TYPES
        assert isinstance(r["display_name"], str) and r["display_name"] != ""
        assert isinstance(r["missing_required"], list)
        assert r["is_complete"] == (len(r["missing_required"]) == 0)
        # No stored metadata was set, so title falls back (non-empty effective).
        assert r["title"] is not None and r["title"] != ""


# ===========================================================================
# Property 11 — Update-then-read round-trips on an indexed source
# ===========================================================================
_ISO_DATE = st.dates(
    min_value=__import__("datetime").date(1000, 1, 1),
    max_value=__import__("datetime").date(9999, 12, 31),
).map(lambda d: d.isoformat())

_short_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=80
).filter(lambda s: s.strip() != "")


@st.composite
def update_fields(draw) -> dict:
    """A dict of valid, submittable metadata fields (all optional)."""
    fields: dict = {}
    if draw(st.booleans()):
        fields["source_type"] = draw(st.sampled_from(SOURCE_TYPES))
    if draw(st.booleans()):
        fields["authors"] = draw(st.lists(_short_text, min_size=1, max_size=3))
    for name in ("title", "container", "publisher", "url"):
        if draw(st.booleans()):
            fields[name] = draw(_short_text)
    if draw(st.booleans()):
        fields["publication_date"] = draw(_ISO_DATE)
    if draw(st.booleans()):
        fields["access_date"] = draw(_ISO_DATE)
    return fields


@settings(max_examples=100)
@given(sp=source_path(), fields=update_fields())
def test_update_then_read_round_trips(sp, fields) -> None:
    """Feature: citation-formatting, Property 11: Update-then-read round-trips on an indexed source.

    PUT the submitted fields on an indexed source, then GET it back: the
    reloaded effective record reflects exactly the submitted fields (title,
    authors, container, publisher, dates, url). Validates Req 4.5.
    """
    with MonkeyPatch.context() as mp, temp_metadata_store(mp) as (meta, _path):
        _wire(mp, {sp: 3}, meta)
        client = TestClient(main.app)

        put = client.put(f"/api/references/{sp}", json=fields)
        assert put.status_code == 200, put.text

        got = client.get(f"/api/references/{sp}").json()

    for key, value in fields.items():
        assert got[key] == value, f"field {key!r} did not round-trip"


# ===========================================================================
# Property 12 — Unknown source is a 404 and leaves the store unchanged
# ===========================================================================
@st.composite
def indexed_and_unknown(draw):
    """An indexed set plus a source_path guaranteed NOT in it."""
    indexed = draw(
        st.dictionaries(
            source_path(), st.integers(min_value=1, max_value=9), max_size=5
        )
    )
    unknown = draw(source_path().filter(lambda p: p not in indexed))
    return indexed, unknown


@settings(max_examples=100)
@given(data=indexed_and_unknown())
def test_unknown_source_404_leaves_store_unchanged(data) -> None:
    """Feature: citation-formatting, Property 12: Unknown source is a 404 and leaves the store unchanged.

    For a source_path not in the index, GET-one, PUT, POST import, and a POST
    format batch that contains it all return 404, and the on-disk
    source_metadata.json is byte-for-byte unchanged across every call.
    Validates Req 4.4, 4.6, 4.10, 8.2.
    """
    indexed, unknown = data
    with MonkeyPatch.context() as mp, temp_metadata_store(mp) as (meta, path):
        _wire(mp, indexed, meta)
        client = TestClient(main.app)

        before = _read_bytes(path)

        assert client.get(f"/api/references/{unknown}").status_code == 404
        assert (
            client.put(f"/api/references/{unknown}", json={"title": "X"}).status_code
            == 404
        )
        assert (
            client.post(
                f"/api/references/{unknown}/import",
                json={"format": "verbatim", "payload": "Some text.", "style": "MLA"},
            ).status_code
            == 404
        )
        # A format batch containing the unknown path (mixed with any indexed
        # ones) must 404 without mutating the store.
        batch = list(indexed.keys()) + [unknown]
        assert (
            client.post(
                "/api/references/format",
                json={"source_paths": batch, "style": "MLA"},
            ).status_code
            == 404
        )

        after = _read_bytes(path)

    assert before == after


# ===========================================================================
# Property 13 — The style gate accepts exactly MLA and APA
# ===========================================================================
_junk_style = st.text(min_size=0, max_size=8).filter(lambda s: s not in ("MLA", "APA"))


@settings(max_examples=100)
@given(sp=source_path(), style=_junk_style)
def test_style_gate_rejects_non_mla_apa(sp, style) -> None:
    """Feature: citation-formatting, Property 13: The style gate accepts exactly MLA and APA.

    ``POST /api/references/format`` with a style outside {MLA, APA} returns 400
    naming both MLA and APA; MLA and APA themselves return 200. Validates
    Req 1.1, 1.5, 4.9. (Export 10.2 is out of scope until task 15.)
    """
    with MonkeyPatch.context() as mp, temp_metadata_store(mp) as (meta, _path):
        _wire(mp, {sp: 1}, meta)
        client = TestClient(main.app)

        bad = client.post(
            "/api/references/format", json={"source_paths": [sp], "style": style}
        )
        assert bad.status_code == 400
        detail = bad.json()["detail"]
        assert "MLA" in detail and "APA" in detail

        for good in ("MLA", "APA"):
            ok = client.post(
                "/api/references/format",
                json={"source_paths": [sp], "style": good},
            )
            assert ok.status_code == 200, ok.text
            assert ok.json()["style"] == good


# ===========================================================================
# Property 14 — A disallowed source_type is a 400 and leaves the store unchanged
# ===========================================================================
_junk_type = st.text(min_size=1, max_size=12).filter(lambda s: s not in SOURCE_TYPES)


@settings(max_examples=100)
@given(sp=source_path(), bad_type=_junk_type)
def test_disallowed_source_type_400_store_unchanged(sp, bad_type) -> None:
    """Feature: citation-formatting, Property 14: A disallowed source_type is a 400 and leaves the store unchanged.

    ``PUT`` with a ``source_type`` outside the allowed five returns 400 naming
    the allowed types, and the on-disk store is byte-for-byte unchanged.
    Validates Req 4.7.
    """
    with MonkeyPatch.context() as mp, temp_metadata_store(mp) as (meta, path):
        _wire(mp, {sp: 1}, meta)
        client = TestClient(main.app)

        before = _read_bytes(path)
        resp = client.put(f"/api/references/{sp}", json={"source_type": bad_type})
        after = _read_bytes(path)

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    for allowed in SOURCE_TYPES:
        assert allowed in detail
    assert before == after


# ===========================================================================
# Property 15 — Batch format returns one citation per requested source
# ===========================================================================
@settings(max_examples=100)
@given(
    paths=st.lists(source_path(), min_size=1, max_size=100, unique=True),
    style=st.sampled_from(["MLA", "APA"]),
)
def test_batch_format_one_citation_per_source(paths, style) -> None:
    """Feature: citation-formatting, Property 15: Batch format returns one citation per requested source.

    For 1..100 indexed sources and a valid style, the format response has
    exactly one citation per requested source, in request order (source_path
    matches position-by-position). Validates Req 4.8.
    """
    indexed = {p: 1 for p in paths}
    with MonkeyPatch.context() as mp, temp_metadata_store(mp) as (meta, _path):
        _wire(mp, indexed, meta)
        client = TestClient(main.app)

        resp = client.post(
            "/api/references/format",
            json={"source_paths": paths, "style": style},
        )
        assert resp.status_code == 200, resp.text
        citations = resp.json()["citations"]

    assert len(citations) == len(paths)
    assert [c["source_path"] for c in citations] == paths


@settings(max_examples=50)
@given(
    n=st.one_of(st.just(0), st.integers(min_value=101, max_value=140)),
    style=st.sampled_from(["MLA", "APA"]),
)
def test_batch_format_size_bounds_400(n, style) -> None:
    """Feature: citation-formatting, Property 15: Batch format returns one citation per requested source.

    A batch with fewer than 1 or more than 100 entries is a 400. Validates
    Req 4.8 (the 1..100 bound).
    """
    paths = [f"src/{i}.pdf" for i in range(n)]
    indexed = {p: 1 for p in paths}
    with MonkeyPatch.context() as mp, temp_metadata_store(mp) as (meta, _path):
        _wire(mp, indexed, meta)
        client = TestClient(main.app)

        resp = client.post(
            "/api/references/format",
            json={"source_paths": paths, "style": style},
        )

    assert resp.status_code == 400


# ===========================================================================
# 8.12 — Import API example tests
# ===========================================================================
_BIBTEX_BOOK = (
    "@book{stewart2015,\n"
    "  author = {Stewart, James},\n"
    "  title = {Calculus},\n"
    "  publisher = {Cengage Learning},\n"
    "  year = {2015}\n"
    "}\n"
)

_RIS_ARTICLE = (
    "TY  - JOUR\n"
    "AU  - Doe, Jane\n"
    "TI  - On Widgets\n"
    "JO  - Journal of Widgets\n"
    "PY  - 2020\n"
    "ER  - \n"
)

_CSLJSON_WEBPAGE = json.dumps(
    [
        {
            "id": "site1",
            "type": "webpage",
            "title": "A Web Page",
            "author": [{"family": "Roe", "given": "Richard"}],
            "URL": "https://example.com/page",
        }
    ]
)

_VERBATIM_APA = "Stewart, J. (2015). Calculus (8th ed.). Cengage Learning."


class TestImportAPI:
    """Example-based coverage of ``POST /api/references/{sp}/import``."""

    SP = "src/doc.pdf"

    def _client_with(self, mp, tmp_path):
        meta = make_metadata_store(tmp_path / "source_metadata.json")
        _wire(mp, {self.SP: 5}, meta)
        return TestClient(main.app), tmp_path / "source_metadata.json"

    def test_each_format_accepted_and_dispatched(self, monkeypatch, tmp_path) -> None:
        """Req 8.1: each of the four import formats is accepted and dispatched."""
        cases = [
            {"format": "bibtex", "payload": _BIBTEX_BOOK},
            {"format": "ris", "payload": _RIS_ARTICLE},
            {"format": "csljson", "payload": _CSLJSON_WEBPAGE},
            {"format": "verbatim", "payload": _VERBATIM_APA, "style": "APA"},
        ]
        for body in cases:
            with MonkeyPatch.context() as mp:
                client, _ = self._client_with(mp, tmp_path)
                resp = client.post(f"/api/references/{self.SP}/import", json=body)
                assert resp.status_code == 200, (body["format"], resp.text)
                assert resp.json()["source_path"] == self.SP

    def test_unparseable_structured_400_names_format_store_unchanged(
        self, tmp_path
    ) -> None:
        """Req 8.3: a payload that no declared structured format can parse is a
        400 that names that format, and the store is unchanged."""
        with MonkeyPatch.context() as mp:
            client, path = self._client_with(mp, tmp_path)
            before = _read_bytes(path)
            resp = client.post(
                f"/api/references/{self.SP}/import",
                json={"format": "csljson", "payload": "this is not json {{{"},
            )
            after = _read_bytes(path)
        assert resp.status_code == 400
        assert "csljson" in resp.json()["detail"]
        assert before == after

    def test_bibtex_single_entry_persists_and_sets_book_type(self, tmp_path) -> None:
        """Req 8.4, 8.7: a single-entry BibTeX import persists title/authors and
        infers source_type=book; Req 8.8: GET afterwards shows display_name equal
        to the imported title."""
        with MonkeyPatch.context() as mp:
            client, _ = self._client_with(mp, tmp_path)
            resp = client.post(
                f"/api/references/{self.SP}/import",
                json={"format": "bibtex", "payload": _BIBTEX_BOOK},
            )
            assert resp.status_code == 200, resp.text
            view = resp.json()
            assert view["title"] == "Calculus"
            assert view["authors"] == ["Stewart, James"]
            assert view["source_type"] == "book"

            got = client.get(f"/api/references/{self.SP}").json()
        assert got["display_name"] == "Calculus"

    def test_verbatim_writes_only_override_and_leaves_display_name(
        self, tmp_path
    ) -> None:
        """Req 8.9: a verbatim APA import writes ONLY verbatim_overrides["APA"]
        and changes no other field, so display_name stays the filename."""
        with MonkeyPatch.context() as mp:
            client, _ = self._client_with(mp, tmp_path)
            before = client.get(f"/api/references/{self.SP}").json()
            resp = client.post(
                f"/api/references/{self.SP}/import",
                json={"format": "verbatim", "payload": _VERBATIM_APA, "style": "APA"},
            )
            assert resp.status_code == 200, resp.text
            view = resp.json()
        assert view["verbatim_overrides"] == {"APA": _VERBATIM_APA}
        # display_name unchanged (no title was set), and the scalar fields the
        # verbatim import must not touch stay as they were.
        assert view["display_name"] == before["display_name"]
        assert view["title"] == before["title"]
        assert view["authors"] == before["authors"]

    def test_reimport_replaces(self, tmp_path) -> None:
        """Req 8.12: re-import replaces. A second structured import overwrites the
        mapped fields; a second verbatim for the same style replaces that
        override, while a verbatim for the OTHER style leaves the first intact."""
        alt_bibtex = (
            "@article{doe2021,\n"
            "  author = {Doe, John},\n"
            "  title = {Second Title},\n"
            "  journal = {Some Journal},\n"
            "  year = {2021}\n"
            "}\n"
        )
        with MonkeyPatch.context() as mp:
            client, _ = self._client_with(mp, tmp_path)

            # Structured re-import overwrites the mapped fields.
            client.post(
                f"/api/references/{self.SP}/import",
                json={"format": "bibtex", "payload": _BIBTEX_BOOK},
            )
            v2 = client.post(
                f"/api/references/{self.SP}/import",
                json={"format": "bibtex", "payload": alt_bibtex},
            ).json()
            assert v2["title"] == "Second Title"
            assert v2["source_type"] == "article"

            # Verbatim replace for the same style; other style untouched.
            client.post(
                f"/api/references/{self.SP}/import",
                json={"format": "verbatim", "payload": "APA first.", "style": "APA"},
            )
            client.post(
                f"/api/references/{self.SP}/import",
                json={"format": "verbatim", "payload": "MLA one.", "style": "MLA"},
            )
            v3 = client.post(
                f"/api/references/{self.SP}/import",
                json={"format": "verbatim", "payload": "APA second.", "style": "APA"},
            ).json()

        assert v3["verbatim_overrides"]["APA"] == "APA second."
        assert v3["verbatim_overrides"]["MLA"] == "MLA one."

    def test_multi_entry_requires_selector(self, tmp_path) -> None:
        """Req 8.11: a multi-entry BibTeX with no selector is a 400; with a valid
        key or index it succeeds (200) and imports exactly that entry."""
        multi = (
            "@book{first2001,\n  author = {A, One},\n  title = {First Book},\n"
            "  year = {2001}\n}\n"
            "@book{second2002,\n  author = {B, Two},\n  title = {Second Book},\n"
            "  year = {2002}\n}\n"
        )
        with MonkeyPatch.context() as mp:
            client, _ = self._client_with(mp, tmp_path)

            no_sel = client.post(
                f"/api/references/{self.SP}/import",
                json={"format": "bibtex", "payload": multi},
            )
            assert no_sel.status_code == 400

            by_key = client.post(
                f"/api/references/{self.SP}/import",
                json={"format": "bibtex", "payload": multi, "entry": "second2002"},
            )
            assert by_key.status_code == 200, by_key.text
            assert by_key.json()["title"] == "Second Book"

            by_index = client.post(
                f"/api/references/{self.SP}/import",
                json={"format": "bibtex", "payload": multi, "entry": "1"},
            )
            assert by_index.status_code == 200, by_index.text
            assert by_index.json()["title"] == "First Book"

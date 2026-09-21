"""Tests for the vision-ingest visual extraction pipeline (app/ingestion/vision.py).

These exercise the extractor at the ``_extract_visuals`` / ``_extract_one`` and
``_discover_pdf_visuals`` layers, which operate on ``_Visual`` objects (dummy
image bytes) and a faked PyMuPDF — so no real images, no Ollama, and no real
``data/`` are ever touched.

Isolation rules honored here:
  * The VLM is always faked by monkeypatching ``app.ingestion.vision.ollama_client
    .vision_extract`` (patched where it is used: vision.py imports the module via
    ``from ..llm import ollama_client`` and calls ``ollama_client.vision_extract``).
  * PDF discovery is tested against a hand-built fake ``pymupdf`` module registered
    in ``sys.modules`` for the duration of the test (vision.py does ``import pymupdf
    as fitz`` lazily inside ``_discover_pdf_visuals``).
  * ``settings`` caps are overridden per-test via monkeypatch.

Property references are to .kiro/specs/vision-ingest/design.md.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
from hypothesis import given, settings as hyp_settings
from hypothesis import strategies as st

from app.config import settings
from app.ingestion import vision
from app.ingestion.content_types import (
    CONTENT_CHART,
    CONTENT_FIGURE,
    CONTENT_OCR,
    CONTENT_TABLE,
)
from app.indexing.jobs import Job


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _make_job() -> Job:
    """A minimal Job usable as the progress sink for _extract_visuals."""
    return Job(id="test", kind="index")


def _valid_response(kind: str = "figure") -> str:
    """A well-formed VLM response the extractor accepts (TYPE line + body)."""
    return f"TYPE: {kind}\nsome extracted description text"


# --------------------------------------------------------------------------
# Property 4: per-visual failure isolation
# --------------------------------------------------------------------------
@hyp_settings(max_examples=100)
@given(data=st.data(), n=st.integers(min_value=0, max_value=12))
def test_extract_visuals_isolates_per_visual_failures(data, n: int) -> None:
    """Feature: vision-ingest, Property 4: Per-visual failure isolation.

    Given a list of N visuals where an arbitrary subset raise (Exception or
    TimeoutError) and the rest return a valid response, ``_extract_visuals``
    returns sections for exactly the successful visuals, sets skipped_count to
    the number of failures, never raises, and (with a Job) advances
    ``visuals_done`` to len(visuals).

    A per-example ``MonkeyPatch`` context is used (not the function-scoped
    fixture) so the VLM fake is installed and torn down for each Hypothesis
    example.
    """
    visuals = [
        vision._Visual(image_bytes=b"x", page=1, index_on_page=i + 1) for i in range(n)
    ]

    # Pick an arbitrary subset of indices to fail, and whether each fails with a
    # plain Exception or a TimeoutError (both must be isolated identically).
    fail_indices = (
        data.draw(st.sets(st.integers(min_value=0, max_value=n - 1)))
        if n > 0
        else set()
    )
    timeout_indices = {i for i in fail_indices if data.draw(st.booleans())}

    # vision_extract is called once per visual, in order; count calls to know
    # which visual is being processed (all image_bytes are identical b"x").
    call_counter = {"i": 0}

    def fake_vision_extract(image_bytes, prompt, timeout_s=None):
        idx = call_counter["i"]
        call_counter["i"] += 1
        if idx in fail_indices:
            if idx in timeout_indices:
                raise TimeoutError("simulated per-visual timeout")
            raise Exception("simulated per-visual error")
        return _valid_response("figure")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(vision.ollama_client, "vision_extract", fake_vision_extract)

        job = _make_job()
        # Must never raise regardless of how many visuals fail.
        sections, skipped = vision._extract_visuals(visuals, job=job)

    num_failures = len(fail_indices)
    num_successes = n - num_failures

    assert len(sections) == num_successes
    assert skipped == num_failures
    # Job progress reflects every visual attempted, successes and failures.
    assert job.visuals_total == n
    assert job.visuals_done == n
    assert job.current_stage == "extracting visuals"


def test_extract_visuals_all_fail_returns_no_sections(monkeypatch) -> None:
    """Feature: vision-ingest, Property 4: Per-visual failure isolation.

    A concrete edge case: when every visual fails, the result is zero sections,
    skipped equals the count, and no exception escapes.
    """
    visuals = [vision._Visual(b"x", 1, i + 1) for i in range(5)]

    def always_raise(image_bytes, prompt, timeout_s=None):
        raise TimeoutError("boom")

    monkeypatch.setattr(vision.ollama_client, "vision_extract", always_raise)

    sections, skipped = vision._extract_visuals(visuals)
    assert sections == []
    assert skipped == 5


# --------------------------------------------------------------------------
# A fake PyMuPDF for Property 5 (image-cap) discovery tests
# --------------------------------------------------------------------------
class _FakePixmap:
    """Stand-in for fitz.Pixmap with the attributes _discover_pdf_visuals reads."""

    def __init__(self, width=100, height=100, n=3, alpha=0):
        self.width = width
        self.height = height
        self.n = n
        self.alpha = alpha

    def tobytes(self, fmt="png"):
        return b"png-bytes"


class _FakePage:
    """A page exposing get_images / get_text / get_pixmap."""

    def __init__(self, num_images: int, text: str = "lots of extractable text here"):
        self._num_images = num_images
        self._text = text

    def get_images(self, full=True):
        # Each image tuple: xref is element 0; the rest is irrelevant to the code.
        return [(xref, 0, 0, 0, 0, 0, 0) for xref in range(1, self._num_images + 1)]

    def get_text(self, mode="text"):
        return self._text

    def get_pixmap(self, matrix=None):
        return _FakePixmap()


class _FakeDoc:
    def __init__(self, pages: list[_FakePage]):
        self._pages = pages
        self.page_count = len(pages)
        self.closed = False

    def load_page(self, pno: int) -> _FakePage:
        return self._pages[pno]

    def close(self) -> None:
        self.closed = True


def _install_fake_pymupdf(
    monkeypatch, pages: list[_FakePage], min_pixels_ok: bool = True
):
    """Register a fake ``pymupdf`` module in sys.modules for the duration.

    The fake provides open/Pixmap/Matrix/csRGB with just enough behavior for
    ``_discover_pdf_visuals``. Pixmaps report a size above the min-pixel filter
    by default so images are not dropped as decorative.
    """
    fake = types.ModuleType("pymupdf")

    doc = _FakeDoc(pages)

    def _open(path):
        return doc

    def _pixmap(*args):
        # fitz.Pixmap(doc, xref) or fitz.Pixmap(csRGB, pix) — both return a big pixmap.
        if min_pixels_ok:
            return _FakePixmap(width=100, height=100, n=3, alpha=0)
        return _FakePixmap(width=1, height=1, n=3, alpha=0)

    fake.open = _open
    fake.Pixmap = _pixmap
    fake.Matrix = lambda zx, zy: ("matrix", zx, zy)
    fake.csRGB = object()

    monkeypatch.setitem(sys.modules, "pymupdf", fake)
    return doc


# --------------------------------------------------------------------------
# Property 5: image cap is respected
# --------------------------------------------------------------------------
@hyp_settings(max_examples=100, deadline=None)
@given(
    m=st.integers(min_value=1, max_value=20), cap=st.integers(min_value=1, max_value=10)
)
def test_per_page_image_cap_respected(m: int, cap: int) -> None:
    """Feature: vision-ingest, Property 5: Image cap is respected.

    A single page with M embedded images and a per-page cap C yields at most C
    visuals for that page. The per-file cap is set large so only the per-page
    cap can bind. A per-example MonkeyPatch context installs the fake pymupdf
    and cap settings for each Hypothesis example.
    """
    with pytest.MonkeyPatch.context() as mp:
        pages = [_FakePage(num_images=m)]
        _install_fake_pymupdf(mp, pages)

        mp.setattr(settings, "vision_max_images_per_page", cap)
        mp.setattr(settings, "vision_max_images_per_file", 10_000)
        mp.setattr(settings, "vision_min_image_pixels", 4096)

        visuals = vision._discover_pdf_visuals(Path("fake.pdf"))

    assert len(visuals) <= cap
    # With plenty of images available, the cap is the binding limit.
    assert len(visuals) == min(m, cap)


@hyp_settings(max_examples=100, deadline=None)
@given(
    pages_n=st.integers(min_value=1, max_value=8),
    per_page=st.integers(min_value=1, max_value=6),
    file_cap=st.integers(min_value=1, max_value=20),
)
def test_per_file_image_cap_respected(
    pages_n: int, per_page: int, file_cap: int
) -> None:
    """Feature: vision-ingest, Property 5: Image cap is respected.

    Across multiple pages, the total number of visuals never exceeds the
    per-file cap. Each page carries enough images that both caps are in play.
    A per-example MonkeyPatch context installs the fake pymupdf and caps.
    """
    with pytest.MonkeyPatch.context() as mp:
        # Each page has more images than the per-page cap so pages fill to per_page.
        pages = [_FakePage(num_images=per_page + 3) for _ in range(pages_n)]
        _install_fake_pymupdf(mp, pages)

        mp.setattr(settings, "vision_max_images_per_page", per_page)
        mp.setattr(settings, "vision_max_images_per_file", file_cap)
        mp.setattr(settings, "vision_min_image_pixels", 4096)

        visuals = vision._discover_pdf_visuals(Path("fake.pdf"))

    assert len(visuals) <= file_cap
    # The exact expected total is min(file_cap, pages * per_page).
    assert len(visuals) == min(file_cap, pages_n * per_page)


def test_scanned_page_render_respects_min_pixel_ok(monkeypatch) -> None:
    """Feature: vision-ingest, Property 5: Image cap is respected.

    A page with no embedded images and little text is rendered whole as exactly
    one visual, still counted against the caps (here well under them).
    """
    pages = [_FakePage(num_images=0, text="")]  # no images, no text -> scanned
    _install_fake_pymupdf(monkeypatch, pages)

    monkeypatch.setattr(settings, "vision_max_images_per_page", 6)
    monkeypatch.setattr(settings, "vision_max_images_per_file", 400)
    monkeypatch.setattr(settings, "vision_min_image_pixels", 4096)

    visuals = vision._discover_pdf_visuals(Path("fake.pdf"))
    assert len(visuals) == 1
    assert visuals[0].page == 1


# --------------------------------------------------------------------------
# Property 6: VLM unavailability / discovery degrades, never aborts
# --------------------------------------------------------------------------
def test_vision_available_gating_delegates_to_client(monkeypatch) -> None:
    """Feature: vision-ingest, Property 6: VLM unavailability degrades, never aborts.

    ``vision.vision_available`` is a thin re-export of the client's gating
    check; it returns whatever the client reports.
    """
    monkeypatch.setattr(vision.ollama_client, "vision_available", lambda: False)
    assert vision.vision_available() is False

    monkeypatch.setattr(vision.ollama_client, "vision_available", lambda: True)
    assert vision.vision_available() is True


def test_extract_visuals_unsupported_extension_returns_empty(monkeypatch) -> None:
    """Feature: vision-ingest, Property 6: VLM unavailability degrades, never aborts.

    For an extension with no discovery dispatch (e.g. .txt), ``extract_visuals``
    returns ([], 0) and never invokes the VLM.
    """

    def should_not_call(*args, **kwargs):
        raise AssertionError("vision_extract must not be called for unsupported types")

    monkeypatch.setattr(vision.ollama_client, "vision_extract", should_not_call)

    sections, skipped = vision.extract_visuals(Path("notes.txt"))
    assert sections == []
    assert skipped == 0


def test_extract_visuals_discovery_exception_degrades_to_empty(monkeypatch) -> None:
    """Feature: vision-ingest, Property 6: VLM unavailability degrades, never aborts.

    If discovery raises, ``extract_visuals`` swallows it and returns ([], 0)
    rather than propagating the error.
    """

    def boom(path):
        raise RuntimeError("discovery blew up")

    # Point a supported extension's discovery at a raising function.
    monkeypatch.setitem(vision._DISCOVERY, ".pdf", boom)

    sections, skipped = vision.extract_visuals(Path("broken.pdf"))
    assert sections == []
    assert skipped == 0


# --------------------------------------------------------------------------
# _parse_type unit tests
# --------------------------------------------------------------------------
def test_parse_type_chart() -> None:
    """A leading 'TYPE: chart' maps to the chart content type; body strips the TYPE line."""
    content_type, body = vision._parse_type(
        "TYPE: chart\nquarterly revenue, up and to the right"
    )
    assert content_type == CONTENT_CHART
    assert body == "quarterly revenue, up and to the right"
    assert "TYPE:" not in body


def test_parse_type_table() -> None:
    """'TYPE: table' maps to the table content type."""
    content_type, body = vision._parse_type("TYPE: table\na | b | c")
    assert content_type == CONTENT_TABLE
    assert body == "a | b | c"


def test_parse_type_diagram_maps_to_figure() -> None:
    """'TYPE: diagram' maps to the figure content type (diagram/illustration/photo -> figure)."""
    content_type, body = vision._parse_type(
        "TYPE: diagram\na flowchart of the pipeline"
    )
    assert content_type == CONTENT_FIGURE
    assert body == "a flowchart of the pipeline"


def test_parse_type_no_type_line_defaults_to_figure() -> None:
    """With no TYPE line, the content type defaults to figure and the body is unchanged."""
    content_type, body = vision._parse_type("just a description with no type prefix")
    assert content_type == CONTENT_FIGURE
    assert body == "just a description with no type prefix"


def test_parse_type_text_maps_to_ocr() -> None:
    """'TYPE: text' (a full page of text) maps to the ocr content type."""
    content_type, body = vision._parse_type("TYPE: text\nthe transcribed page text")
    assert content_type == CONTENT_OCR
    assert body == "the transcribed page text"


# --------------------------------------------------------------------------
# A small end-to-end sanity check at the _extract_one level: content_type flows
# through and the location label is built from page/index.
# --------------------------------------------------------------------------
def test_extract_one_builds_section_with_content_type_and_location(monkeypatch) -> None:
    """Feature: vision-ingest, Property 4: Per-visual failure isolation.

    A successful extraction produces a LoadedSection whose content_type comes
    from the parsed TYPE line and whose location encodes page and figure index.
    """
    monkeypatch.setattr(
        vision.ollama_client,
        "vision_extract",
        lambda image_bytes, prompt, timeout_s=None: _valid_response("chart"),
    )
    section = vision._extract_one(vision._Visual(b"x", page=7, index_on_page=2))
    assert section is not None
    assert section.meta["content_type"] == CONTENT_CHART
    assert section.location == "p. 7 (figure 2)"
    assert section.meta["page"] == 7
    assert section.meta["figure"] == 2


def test_extract_one_empty_response_is_skipped(monkeypatch) -> None:
    """Feature: vision-ingest, Property 4: Per-visual failure isolation.

    An empty or whitespace-only VLM response yields no section (counted as
    skipped by the caller), not a raised error.
    """
    monkeypatch.setattr(
        vision.ollama_client,
        "vision_extract",
        lambda image_bytes, prompt, timeout_s=None: "   \n  ",
    )
    assert vision._extract_one(vision._Visual(b"x", 1, 1)) is None

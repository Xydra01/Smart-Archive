"""Vision extraction: turn document visuals into searchable text at ingest.

When vision ingest is enabled, this module finds the Visuals in a document —
charts, graphs, diagrams, illustrations, figures, and image-only tables,
including figures on scanned/image-only pages — and asks a local
vision-language model (VLM, default ``qwen2.5vl:3b`` via Ollama) to extract
their information as text. That text is returned as ordinary ``LoadedSection``s
so it flows through the same chunk → embed → index → retrieve → cite path as
everything else, tagged with a ``content_type`` describing what it came from.

Design points:
  * Front-loaded: slow at ingest, free at query time; skipped for unchanged
    files by the manifest.
  * Best-effort per visual: any single image that errors or times out is
    skipped and counted, never aborting the file (see ``_extract_one``).
  * Bounded: per-page and per-file image caps limit worst-case time.

Heavy third-party imports (PyMuPDF) are done lazily inside functions so the
module can be imported (and unit-tested with the VLM faked) even where those
packages or the model are unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from ..config import settings
from ..llm import ollama_client
from .content_types import (
    CONTENT_CHART,
    CONTENT_FIGURE,
    CONTENT_OCR,
    CONTENT_TABLE,
)
from .loaders import LoadedSection


# --------------------------------------------------------------------------
# Prompt + classification
# --------------------------------------------------------------------------
# One structured prompt: the model both classifies the visual and extracts the
# right information for its kind. The leading "TYPE:" line lets us map the
# result to a content_type; the rest is the searchable text we index.
VISION_PROMPT = (
    "You are extracting information from a single image taken from a document "
    "so it can be searched later. First output one line 'TYPE: <kind>' where "
    "<kind> is one of chart, table, diagram, figure, photo, or text. Then, on "
    "the following lines, extract the information a reader would get from it:\n"
    "- chart/graph: its title, axis labels, each data series, notable values, "
    "and the overall trend.\n"
    "- table: transcribe it as rows of cells separated by ' | '.\n"
    "- diagram/figure/illustration: describe what it shows and transcribe every "
    "label or piece of text in it.\n"
    "- photo: describe the scene and read any visible text.\n"
    "- text (a page of mostly text): transcribe the readable text.\n"
    "Be concise and factual. Do not invent details that are not visible."
)

_TYPE_TO_CONTENT = {
    "chart": CONTENT_CHART,
    "graph": CONTENT_CHART,
    "table": CONTENT_TABLE,
    "diagram": CONTENT_FIGURE,
    "figure": CONTENT_FIGURE,
    "illustration": CONTENT_FIGURE,
    "photo": CONTENT_FIGURE,
    "image": CONTENT_FIGURE,
    "text": CONTENT_OCR,
}


def _parse_type(text: str) -> tuple[str, str]:
    """Split a VLM response into (content_type, body).

    Reads the leading ``TYPE: <kind>`` line if present and maps it to a
    content_type; anything unrecognized falls back to ``figure``. The TYPE line
    is stripped from the indexed body.
    """
    body = text.strip()
    content_type = CONTENT_FIGURE
    if body.upper().startswith("TYPE:"):
        first, _, rest = body.partition("\n")
        kind = first.split(":", 1)[1].strip().lower().split()[0] if ":" in first else ""
        content_type = _TYPE_TO_CONTENT.get(kind, CONTENT_FIGURE)
        body = rest.strip()
    return content_type, body


@dataclass
class _Visual:
    """A discovered image to extract, with its page and index for labeling."""

    image_bytes: bytes
    page: int
    index_on_page: int


def vision_available() -> bool:
    """Re-exported for callers that only import this module."""
    return ollama_client.vision_available()


# --------------------------------------------------------------------------
# Extraction of a single visual (failure-isolated)
# --------------------------------------------------------------------------
def _extract_one(visual: _Visual) -> Optional[LoadedSection]:
    """Extract one visual to a LoadedSection, or None on failure/timeout/empty.

    Never raises: any error or timeout returns None so the caller can count it
    as skipped and move on.
    """
    try:
        raw = ollama_client.vision_extract(visual.image_bytes, VISION_PROMPT)
    except Exception:
        return None
    if not raw or not raw.strip():
        return None
    content_type, body = _parse_type(raw)
    if not body.strip():
        return None
    location = f"p. {visual.page} (figure {visual.index_on_page})"
    return LoadedSection(
        text=body,
        location=location,
        meta={
            "page": visual.page,
            "figure": visual.index_on_page,
            "content_type": content_type,
        },
    )


def _extract_visuals(
    visuals: list[_Visual], job=None
) -> tuple[list[LoadedSection], int]:
    """Extract a list of visuals, isolating per-visual failures.

    Returns (sections, skipped_count). Updates ``job`` progress counters when a
    Job is supplied.
    """
    sections: list[LoadedSection] = []
    skipped = 0
    if job is not None:
        job.current_stage = "extracting visuals"
        job.visuals_total = len(visuals)
        job.visuals_done = 0
    for v in visuals:
        section = _extract_one(v)
        if section is None:
            skipped += 1
        else:
            sections.append(section)
        if job is not None:
            job.visuals_done += 1
    return sections, skipped


# --------------------------------------------------------------------------
# Per-format visual discovery
# --------------------------------------------------------------------------
def _discover_pdf_visuals(path: Path) -> list[_Visual]:
    """Find images in a PDF, honoring per-page and per-file caps.

    Enumerates embedded raster images per page. Pages that contain no separable
    embedded images but are likely scanned (little/no extractable text) are
    rendered whole so figures on scanned pages are still captured.
    """
    import pymupdf as fitz  # PyMuPDF (the `fitz` top-level name is deprecated)

    visuals: list[_Visual] = []
    per_file_cap = settings.vision_max_images_per_file
    per_page_cap = settings.vision_max_images_per_page
    min_pixels = settings.vision_min_image_pixels

    doc = fitz.open(str(path))
    try:
        for pno in range(doc.page_count):
            if len(visuals) >= per_file_cap:
                break
            page = doc.load_page(pno)
            page_num = pno + 1
            on_page = 0

            images = page.get_images(full=True)
            embedded_found = False
            for img in images:
                if on_page >= per_page_cap or len(visuals) >= per_file_cap:
                    break
                xref = img[0]
                try:
                    pix = fitz.Pixmap(doc, xref)
                    if pix.width * pix.height < min_pixels:
                        continue
                    if pix.n - pix.alpha >= 4:  # CMYK/other -> convert to RGB
                        pix = fitz.Pixmap(fitz.csRGB, pix)
                    png = pix.tobytes("png")
                except Exception:
                    continue
                embedded_found = True
                on_page += 1
                visuals.append(_Visual(png, page_num, on_page))

            # Scanned/image-only page heuristic: no usable embedded images and
            # little extractable text -> render the whole page as one visual.
            if (
                settings.vision_ocr_scanned_pages
                and not embedded_found
                and len(visuals) < per_file_cap
            ):
                try:
                    text = page.get_text("text") or ""
                except Exception:
                    text = ""
                if len(text.strip()) < 40:
                    try:
                        zoom = settings.vision_render_dpi / 72.0
                        mat = fitz.Matrix(zoom, zoom)
                        pix = page.get_pixmap(matrix=mat)
                        png = pix.tobytes("png")
                        on_page += 1
                        visuals.append(_Visual(png, page_num, on_page))
                    except Exception:
                        pass
        return visuals
    finally:
        doc.close()


def _discover_embedded_images(path: Path, kind: str) -> list[_Visual]:
    """Find embedded images in docx / epub / html documents.

    All three are ZIP/markup containers whose images we surface as visuals. The
    ``page`` field is 0 (no pagination); index_on_page orders them.
    """
    import zipfile

    min_pixels = settings.vision_min_image_pixels
    per_file_cap = settings.vision_max_images_per_file
    image_exts = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff")
    visuals: list[_Visual] = []

    def _add(raw: bytes) -> None:
        if len(visuals) >= per_file_cap:
            return
        try:
            from PIL import Image
            import io

            im = Image.open(io.BytesIO(raw))
            if im.width * im.height < min_pixels:
                return
        except Exception:
            # Undecodable image bytes: skip rather than send garbage to the VLM.
            return
        visuals.append(_Visual(raw, 0, len(visuals) + 1))

    if kind in ("docx", "epub"):
        try:
            with zipfile.ZipFile(str(path)) as zf:
                for name in zf.namelist():
                    if len(visuals) >= per_file_cap:
                        break
                    if name.lower().endswith(image_exts):
                        try:
                            _add(zf.read(name))
                        except Exception:
                            continue
        except Exception:
            return visuals
    elif kind == "html":
        # Inline data-URI images and images sitting beside the html file.
        try:
            from bs4 import BeautifulSoup
            import base64

            soup = BeautifulSoup(path.read_bytes(), "lxml")
            for tag in soup.find_all("img"):
                if len(visuals) >= per_file_cap:
                    break
                src = tag.get("src") or ""
                if src.startswith("data:") and "base64," in src:
                    try:
                        _add(base64.b64decode(src.split("base64,", 1)[1]))
                    except Exception:
                        continue
                else:
                    candidate = (path.parent / src).resolve()
                    try:
                        if (
                            candidate.is_file()
                            and candidate.suffix.lower() in image_exts
                        ):
                            _add(candidate.read_bytes())
                    except Exception:
                        continue
        except Exception:
            return visuals
    return visuals


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------
_DISCOVERY: dict[str, Callable[[Path], list[_Visual]]] = {
    ".pdf": _discover_pdf_visuals,
    ".docx": lambda p: _discover_embedded_images(p, "docx"),
    ".epub": lambda p: _discover_embedded_images(p, "epub"),
    ".html": lambda p: _discover_embedded_images(p, "html"),
    ".htm": lambda p: _discover_embedded_images(p, "html"),
}


def extract_visuals(path: Path, job=None) -> tuple[list[LoadedSection], int]:
    """Extract visual-derived sections from a document.

    Returns (sections, skipped_count). Discovery failures degrade to no
    visuals; per-visual failures are counted in skipped_count. The caller is
    responsible for checking ``vision_available()`` before calling this.
    """
    discover = _DISCOVERY.get(path.suffix.lower())
    if discover is None:
        return [], 0
    try:
        visuals = discover(path)
    except Exception:
        return [], 0
    return _extract_visuals(visuals, job=job)

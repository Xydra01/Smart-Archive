"""Allowed chunk content types.

A single source of truth so loaders, the vision extractor, the chunker, and the
tests all agree on what a chunk's ``content_type`` may be. Every chunk carries
exactly one of these, describing where its text came from:

    text    ordinary extracted body text
    table   a table (DOCX table, pdfplumber native table, or VLM-read table)
    chart   a chart/graph read by the vision model
    figure  a diagram, illustration, or figure read by the vision model
    ocr     text read from a full scanned/image-only page by the vision model
"""
from __future__ import annotations

CONTENT_TEXT = "text"
CONTENT_TABLE = "table"
CONTENT_CHART = "chart"
CONTENT_FIGURE = "figure"
CONTENT_OCR = "ocr"

CONTENT_TYPES = frozenset(
    {CONTENT_TEXT, CONTENT_TABLE, CONTENT_CHART, CONTENT_FIGURE, CONTENT_OCR}
)

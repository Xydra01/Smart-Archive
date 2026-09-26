"""The Display_Name resolver: one label for a source, used app-wide (Req 9).

A single pure, total helper that resolves the label shown for a source
everywhere in the app (the Sources list, search results, the scope picker,
groups, and the References_View). It is computed at read time and surfaced as
an added field on existing response shapes rather than being persisted, so the
change surface stays minimal and backward-compatible.

The rule is deliberately simple (Req 9.1, 9.2): the stored title when it has at
least one non-whitespace character, otherwise the source's file name. Unlike the
title fallback in ``effective_record``, this resolver does *not* strip the file
extension — a source labeled by filename shows the name with its extension
(e.g. ``"28357872.pdf"``), which is what a reader recognises.
"""

from __future__ import annotations

import os
from typing import Optional


def _final_segment(source_path: str) -> str:
    """Return the final path segment of ``source_path``.

    Uses ``os.path.basename`` and falls back to a ``"/"`` split so a path with
    trailing separators still yields a sensible last component. Mirrors the
    helper in ``effective.py`` so both resolvers agree on the file name.
    """
    base = os.path.basename(source_path)
    if base:
        return base
    # Trailing-separator or degenerate path: take the last non-empty piece.
    parts = [p for p in source_path.rsplit("/") if p]
    return parts[-1] if parts else source_path


def display_name(
    source_path: str,
    stored_title: Optional[str],
    source_file: Optional[str],
) -> str:
    """Resolve the label for a source (Req 9).

    - If ``stored_title`` has at least one non-whitespace character, return it
      (trimmed of leading/trailing whitespace) (Req 9.1).
    - Otherwise return the source's file name: ``source_file`` when present and
      non-empty, else the final path segment of ``source_path`` (Req 9.2). The
      file name keeps its extension (unlike the effective-title fallback).

    Pure, total, and deterministic: never raises and always returns a non-empty
    string for a normal (non-empty) ``source_path``.
    """
    if stored_title is not None and stored_title.strip():
        return stored_title.strip()

    if source_file is not None and source_file.strip():
        return source_file

    return _final_segment(source_path)

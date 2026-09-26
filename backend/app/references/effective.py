"""The effective-record layer: merge stored metadata with computed defaults.

This is the boundary between *stored data* (what the user typed, persisted
verbatim by ``SourceMetadataStore`` — absent fields stay absent, Req 2.8) and
*effective data* (stored data merged with the computed defaults the formatter
and API rely on). Keeping the merge in a pure function — no I/O, no clock —
means the formatter and the API agree on the "effective" title and it stays
testable without touching disk.

The one default deliberately *not* applied here is ``access_date``: the store
owns the create-time default (Req 3.5), so ``effective_record`` leaves a
``None`` ``access_date`` as ``None``. That lets the formatter apply the
website/MLA access-date rule and mark incompleteness itself.
"""

from __future__ import annotations

import os
from typing import Optional

from .store import SourceMetadata


def _strip_extension(name: str) -> str:
    """Return ``name`` with a trailing file extension removed."""
    root, _ext = os.path.splitext(name)
    return root


def _final_segment(source_path: str) -> str:
    """Return the final path segment of ``source_path``.

    Uses ``os.path.basename`` and falls back to a ``"/"`` split so a path with
    trailing separators still yields a sensible last component.
    """
    base = os.path.basename(source_path)
    if base:
        return base
    # Trailing-separator or degenerate path: take the last non-empty piece.
    parts = [p for p in source_path.rsplit("/") if p]
    return parts[-1] if parts else source_path


def effective_record(
    source_path: str,
    stored: Optional[SourceMetadata],
    source_file: Optional[str],
) -> SourceMetadata:
    """Merge stored fields with computed defaults.

    - ``source_type`` defaults to ``"other"`` when absent or empty.
    - ``title``, when absent (``None`` or blank/whitespace-only), falls back to
      ``source_file`` with its file extension removed; when ``source_file`` is
      absent, to the final segment of ``source_path`` with its extension
      removed (Req 3.1). Non-empty for a normal ``source_path``.
    - ``access_date`` is left as-is: a ``None`` stays ``None`` (the store owns
      the create-time default), so the formatter can apply the website/MLA
      access-date rule and incompleteness marking.
    - All other fields (``authors``, ``container``, ``publisher``,
      ``publication_date``, ``url``, ``verbatim_overrides``) are carried through
      from ``stored`` as-is.

    Never mutates ``stored``: a fresh ``SourceMetadata`` is built and returned
    so callers' stored records are untouched.
    """
    # source_type: default "other" when absent/empty.
    source_type = "other"
    if stored is not None and isinstance(stored.source_type, str):
        st = stored.source_type.strip()
        if st:
            source_type = stored.source_type

    # Carry-through fields (copied so the returned record is independent).
    authors = list(stored.authors) if stored is not None else []
    container = stored.container if stored is not None else None
    publisher = stored.publisher if stored is not None else None
    publication_date = stored.publication_date if stored is not None else None
    url = stored.url if stored is not None else None
    access_date = stored.access_date if stored is not None else None
    verbatim_overrides = (
        dict(stored.verbatim_overrides) if stored is not None else {}
    )

    # title fallback (Req 3.1): effective title is the stored title when it has
    # a non-whitespace character; otherwise source_file (extension stripped),
    # else the final path segment of source_path (extension stripped).
    stored_title = stored.title if stored is not None else None
    if stored_title is not None and stored_title.strip():
        title = stored_title
    elif source_file is not None and source_file.strip():
        title = _strip_extension(source_file)
    else:
        title = _strip_extension(_final_segment(source_path))

    return SourceMetadata(
        source_path=source_path,
        source_type=source_type,
        authors=authors,
        title=title,
        container=container,
        publisher=publisher,
        publication_date=publication_date,
        url=url,
        access_date=access_date,
        verbatim_overrides=verbatim_overrides,
    )

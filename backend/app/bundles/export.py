"""Export indexed chunks to a portable bundle.

Resolves which chunks to export (all, a set of source paths, or the members of
a group), streams them straight from the vector store — text + embedding +
metadata — into a bundle file, and returns a report. Nothing is re-embedded;
the stored vectors travel as-is so a weaker machine can query without an
embedding pass.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..config import settings
from ..groups.store import get_group_store
from ..indexing.vector_store import get_store
from .format import BundleWriter, ChunkRecord


class ExportError(Exception):
    pass


def _resolve_source_paths(
    sources: Optional[list[str]], group_id: Optional[str]
) -> Optional[set[str]]:
    """Resolve the export selection to a set of source paths, or None for all.

    A group_id resolves to its member source paths; an explicit ``sources``
    list is used as-is; neither means the whole archive (None).
    """
    if group_id is not None:
        group = get_group_store().get(group_id)
        if group is None:
            raise ExportError(f"Group not found: {group_id}")
        return set(group.members)
    if sources:
        return set(sources)
    return None


def export_bundle(
    out_path: Path,
    *,
    sources: Optional[list[str]] = None,
    group_id: Optional[str] = None,
    gzip_output: Optional[bool] = None,
) -> dict:
    """Write a bundle of the selected chunks. Returns a report dict.

    Raises ExportError if the selection matches no chunks (so we never write a
    silent empty bundle).
    """
    out_path = Path(out_path)
    selection = _resolve_source_paths(sources, group_id)

    store = get_store()
    rows = list(store.iter_export(selection))
    if not rows:
        scope = (
            f"group {group_id}"
            if group_id
            else (f"sources {sources}" if sources else "the archive")
        )
        raise ExportError(f"Nothing to export: no chunks matched {scope}.")

    gzip_output = settings.bundle_default_gzip if gzip_output is None else gzip_output
    with BundleWriter(out_path, settings.embed_model, gzip_output=gzip_output) as w:
        for cid, text, meta, emb in rows:
            w.add(ChunkRecord(id=cid, text=text, embedding=emb, metadata=meta))

    return w.report

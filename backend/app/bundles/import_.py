"""Import a portable bundle into this installation's archive.

Import is an idempotent, additive **merge by chunk id**: a record whose id is
already present is skipped; a record with a new id is added. This makes
re-imports and overlapping bundles safe, and lets a vision-enabled bundle
enrich a text-only archive (the text ids already exist and dedup; the
chart/figure/ocr ids are new and get added).

Safety:
  * refuse while an index job is running (memory contention on constrained
    machines);
  * refuse a bundle whose embedding model/dimension does not match this
    installation's — mixing embedding spaces silently corrupts retrieval;
  * treat the bundle as untrusted data — the reader validates and size-caps
    every record; malformed records are skipped and counted, never executed.

After adding chunks, the BM25 keyword index is rebuilt and the manifest is
updated so imported sources are searchable and recorded.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..config import settings
from ..indexing.jobs import manager as job_manager
from ..indexing.manifest import get_manifest
from ..indexing.vector_store import get_store
from ..llm import ollama_client
from .format import BundleError, read_bundle


class ImportError_(Exception):
    """Raised for whole-bundle refusals (lock, bad header, embedding mismatch)."""


def _installation_embed_dim() -> Optional[int]:
    """Best-effort embedding dimension for this installation.

    Prefer a vector already in the store (no model call); fall back to embedding
    a probe string. Returns None if neither is available (empty store + no
    reachable embedder), in which case the dimension gate is skipped but the
    model-name gate still applies.
    """
    for _cid, _text, _meta, emb in get_store().iter_export(None):
        return len(emb)
    try:
        return len(ollama_client.embed_query("dimension probe"))
    except Exception:
        return None


def import_bundle(
    in_path: Path,
    *,
    replace_sources: bool = False,
    batch_size: int = 256,
) -> dict:
    """Merge a bundle into the archive. Returns a report dict.

    Raises ImportError_ for whole-bundle refusals (indexing in progress, bad
    header, unsupported version, embedding mismatch).
    """
    in_path = Path(in_path)

    # 1) Lock: never run concurrently with an index job.
    if job_manager.is_indexing():
        raise ImportError_(
            "Indexing is in progress — import is paused until it finishes."
        )

    # 2) Header (raises BundleError on bad/absent/unsupported header).
    try:
        header, records, stats = read_bundle(in_path)
    except BundleError as e:
        raise ImportError_(str(e)) from None

    # 3) Embedding-model gate.
    if header.embed_model != settings.embed_model:
        raise ImportError_(
            f"Embedding model mismatch: bundle was built with "
            f"'{header.embed_model}' but this installation uses "
            f"'{settings.embed_model}'. Import refused (retrieval would be "
            f"incorrect)."
        )
    local_dim = _installation_embed_dim()
    if local_dim is not None and header.embed_dim != local_dim:
        raise ImportError_(
            f"Embedding dimension mismatch: bundle is {header.embed_dim}-d but "
            f"this installation is {local_dim}-d. Import refused."
        )

    store = get_store()

    # 4) Optional replace-source: clear existing chunks for the bundle's sources
    #    before adding, scoped strictly to sources present in the bundle.
    if replace_sources:
        for sp in header.sources.keys():
            store.delete_by_source(sp)

    # 5) Stream + merge by id. Skip ids already present (post-replace) and ids
    #    seen earlier in this same bundle.
    existing = store.get_all_ids()
    seen_this_run: set[str] = set()
    added = 0
    skipped_existing = 0
    per_source_added: dict[str, int] = {}

    buf_ids: list[str] = []
    buf_text: list[str] = []
    buf_meta: list[dict] = []
    buf_emb: list[list[float]] = []

    def _flush() -> None:
        nonlocal added
        if not buf_ids:
            return
        store.add_precomputed(buf_ids, buf_text, buf_meta, buf_emb, batch_size=batch_size)
        added += len(buf_ids)
        buf_ids.clear()
        buf_text.clear()
        buf_meta.clear()
        buf_emb.clear()

    for rec in records:
        if rec.id in existing or rec.id in seen_this_run:
            skipped_existing += 1
            continue
        seen_this_run.add(rec.id)
        buf_ids.append(rec.id)
        buf_text.append(rec.text)
        buf_meta.append(rec.metadata)
        buf_emb.append(rec.embedding)
        sp = rec.metadata.get("source_path", "unknown")
        per_source_added[sp] = per_source_added.get(sp, 0) + 1
        if len(buf_ids) >= batch_size:
            _flush()
    _flush()

    # 6) Rebuild BM25 and update the manifest only if something changed.
    if added:
        # Import here to avoid a circular import at module load
        # (indexer imports vision/loaders which don't need bundles).
        from ..indexing.indexer import _rebuild_keyword_index

        _rebuild_keyword_index()
        manifest = get_manifest()
        for sp, n in per_source_added.items():
            manifest.record_imported(sp, n)

    return {
        "added": added,
        "skipped_existing": skipped_existing,
        "invalid_skipped": stats.get("invalid", 0),
        "checksum_ok": stats.get("checksum_ok"),
        "sources": per_source_added,
        "replaced": list(header.sources.keys()) if replace_sources else [],
    }

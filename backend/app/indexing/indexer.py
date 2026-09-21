"""Indexing orchestrator: file -> sections -> chunks -> vector + keyword index.

This is the single entry point ingestion calls. It keeps the vector store and
the BM25 keyword index in sync: whenever chunks are added or a source is
removed, the keyword index is rebuilt from the authoritative Chroma contents.
"""

from __future__ import annotations

from pathlib import Path

from ..config import settings
from ..groups.store import get_group_store
from ..ingestion.chunker import chunk_sections
from ..ingestion.loaders import load_document, SUPPORTED_EXTENSIONS, UnsupportedFileType
from .jobs import Job
from .keyword_index import get_keyword_index
from .manifest import get_manifest
from .vector_store import get_store


def _rel_path(path: Path) -> str:
    return (
        str(path.relative_to(settings.raw_dir))
        if _is_under(path, settings.raw_dir)
        else str(path)
    )


def index_file(path: Path, *, progress=None, rebuild_keyword: bool = True) -> dict:
    """Ingest and index a single file. Returns a summary dict.

    ``progress`` is an optional callable(done, total, sections, chunks) used to
    report per-file chunk progress. ``rebuild_keyword`` can be set False by the
    batch job so the BM25 index is rebuilt once at the end instead of per file.

    Always (re)indexes; the manifest is updated to reflect the new content.
    """
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise UnsupportedFileType(
            f"Unsupported file type '{path.suffix}'. "
            f"Supported: {', '.join(SUPPORTED_EXTENSIONS)}"
        )

    sections = load_document(path)
    chunks = chunk_sections(
        sections,
        source_path=path,
        root=settings.raw_dir,
        chunk_tokens=settings.chunk_tokens,
        overlap_tokens=settings.chunk_overlap_tokens,
    )

    store = get_store()
    # Re-ingesting a file replaces its previous chunks (idempotent).
    rel = _rel_path(path)
    store.delete_by_source(rel)

    def _cb(done, total):
        if progress:
            progress(done, total, len(sections), len(chunks))

    added = store.add_chunks(chunks, progress=_cb)

    # Record in the manifest so future runs can skip this unchanged file.
    get_manifest().record(path, rel, added)

    if rebuild_keyword:
        _rebuild_keyword_index()

    return {
        "file": path.name,
        "source_path": rel,
        "sections": len(sections),
        "chunks_indexed": added,
    }


def index_raw_dir() -> dict:
    """Index every supported file currently in the raw data directory (blocking).

    Prefer ``run_index_job`` for large files; this remains for programmatic use.
    """
    results = []
    errors = []
    files = _supported_files()
    for path in files:
        try:
            results.append(index_file(path, rebuild_keyword=False))
        except Exception as e:  # keep going; report per-file failures
            errors.append({"file": path.name, "error": str(e)})
    _rebuild_keyword_index()
    return {
        "files_indexed": len(results),
        "results": results,
        "errors": errors,
        "total_chunks": get_store().count(),
    }


def run_index_job(job: Job, *, force: bool = False) -> None:
    """Bulk-index the raw dir (recursively), updating ``job`` as it goes.

    Incremental by default: files whose size+mtime match the manifest are
    skipped (no re-embedding). Files that are new or changed get indexed.
    Deleted files are pruned from both the vector store and the manifest.
    Set ``force=True`` to re-index everything regardless of the manifest.
    """
    manifest = get_manifest()
    files = _supported_files()
    job.total_files = len(files)

    # 1) Prune files that were indexed before but no longer exist on disk.
    existing_rels = {_rel_path(p) for p in files}
    for rel in manifest.prune_missing(existing_rels):
        get_store().delete_by_source(rel)
        job.removed_files += 1

    if not files:
        job.message = "Building keyword index…"
        _rebuild_keyword_index()
        job.message = (
            f"No supported files in data/raw. "
            f"Pruned {job.removed_files} removed file(s)."
        )
        return

    did_index = False
    for path in files:
        rel = _rel_path(path)

        # Skip unchanged files unless forced.
        if not force and manifest.is_unchanged(path, rel):
            job.skipped_files += 1
            continue

        job.current_file = path.name
        job.file_chunk_done = 0
        job.file_chunk_total = 0

        def _progress(done, total, sections, chunks):
            job.file_chunk_done = done
            job.file_chunk_total = total

        try:
            result = index_file(path, progress=_progress, rebuild_keyword=False)
            job.results.append(result)
            job.total_chunks += result["chunks_indexed"]
            job.processed_files += 1
            did_index = True
        except Exception as e:
            job.results.append({"file": path.name, "error": str(e)})

    # Rebuild the keyword index once, only if the corpus changed.
    if did_index or job.removed_files:
        job.message = "Building keyword index…"
        _rebuild_keyword_index()

    job.current_file = None
    job.message = (
        f"Indexed {job.processed_files}, skipped {job.skipped_files} unchanged, "
        f"removed {job.removed_files}. {get_store().count()} chunks total."
    )


def _supported_files(root: Path | None = None) -> list[Path]:
    base = root or settings.raw_dir
    return [
        p
        for p in sorted(base.rglob("*"))
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
    ]


def import_folder(folder: Path, *, copy: bool = True) -> dict:
    """Copy (or hard-link) supported files from an external folder into data/raw.

    Preserves the folder's relative structure under data/raw so bulk-imported
    documents stay organized and their source paths are meaningful. The actual
    indexing happens afterward via the normal incremental job, so unchanged
    files are still skipped.

    Set ``copy=False`` to hard-link instead of copy (saves disk for large
    collections on the same filesystem; falls back to copy across filesystems).
    """
    import shutil

    folder = folder.expanduser().resolve()
    if not folder.is_dir():
        raise NotADirectoryError(f"Not a directory: {folder}")

    # Refuse to import the archive's own data dir into itself.
    if _is_under(folder, settings.raw_dir) or folder == settings.raw_dir:
        raise ValueError("Cannot import data/raw into itself.")

    src_files = _supported_files(folder)
    imported = []
    skipped = []
    for src in src_files:
        rel = src.relative_to(folder)
        dest = settings.raw_dir / folder.name / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Skip if an identical-size file is already there (cheap dedupe).
        if dest.exists() and dest.stat().st_size == src.stat().st_size:
            skipped.append(str(rel))
            continue
        try:
            if copy:
                shutil.copy2(src, dest)
            else:
                try:
                    if dest.exists():
                        dest.unlink()
                    dest.hardlink_to(src)
                except OSError:
                    shutil.copy2(src, dest)  # cross-filesystem fallback
            imported.append(str(rel))
        except Exception as e:
            skipped.append(f"{rel} (error: {e})")

    return {
        "source_folder": str(folder),
        "into": str(settings.raw_dir / folder.name),
        "found": len(src_files),
        "imported": len(imported),
        "skipped": len(skipped),
        "unsupported_note": (
            f"Scanned recursively; only {len(SUPPORTED_EXTENSIONS)} supported "
            "extensions were imported."
        ),
    }


def remove_source(source_path: str) -> dict:
    store = get_store()
    store.delete_by_source(source_path)
    get_manifest().remove(source_path)
    get_group_store().prune_source(source_path)
    _rebuild_keyword_index()
    return {"removed": source_path, "total_chunks": store.count()}


def reset_index() -> dict:
    store = get_store()
    store.reset()
    # Wipe the manifest so a subsequent index run re-embeds everything.
    manifest = get_manifest()
    for rel in list(manifest.entries().keys()):
        manifest.remove(rel)
    get_group_store().clear_all_members()
    _rebuild_keyword_index()
    return {"reset": True, "total_chunks": store.count()}


def _rebuild_keyword_index() -> None:
    docs = get_store().all_documents()
    get_keyword_index().build(docs)


def _is_under(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False

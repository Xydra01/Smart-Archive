"""Indexing orchestrator: file -> sections -> chunks -> vector + keyword index.

This is the single entry point ingestion calls. It keeps the vector store and
the BM25 keyword index in sync: whenever chunks are added or a source is
removed, the keyword index is rebuilt from the authoritative Chroma contents.
"""

from __future__ import annotations

from pathlib import Path

from ..config import settings
from ..ingestion.chunker import chunk_sections
from ..ingestion.loaders import load_document, SUPPORTED_EXTENSIONS, UnsupportedFileType
from .jobs import Job
from .keyword_index import get_keyword_index
from .vector_store import get_store


def index_file(path: Path, *, progress=None, rebuild_keyword: bool = True) -> dict:
    """Ingest and index a single file. Returns a summary dict.

    ``progress`` is an optional callable(done, total, sections, chunks) used to
    report per-file chunk progress. ``rebuild_keyword`` can be set False by the
    batch job so the BM25 index is rebuilt once at the end instead of per file.
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
    rel = (
        str(path.relative_to(settings.raw_dir))
        if _is_under(path, settings.raw_dir)
        else str(path)
    )
    store.delete_by_source(rel)

    def _cb(done, total):
        if progress:
            progress(done, total, len(sections), len(chunks))

    added = store.add_chunks(chunks, progress=_cb)

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


def run_index_job(job: Job) -> None:
    """Index all supported files in the raw dir, updating ``job`` as it goes."""
    files = _supported_files()
    job.total_files = len(files)
    if not files:
        job.message = "No supported files found in data/raw."
        return

    for path in files:
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
        except Exception as e:
            job.results.append({"file": path.name, "error": str(e)})
        job.processed_files += 1

    # Rebuild the keyword index once from the full authoritative set.
    job.message = "Building keyword index…"
    _rebuild_keyword_index()
    job.message = (
        f"Indexed {job.processed_files} file(s), {get_store().count()} chunks."
    )


def _supported_files() -> list[Path]:
    return [
        p
        for p in sorted(settings.raw_dir.rglob("*"))
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
    ]


def remove_source(source_path: str) -> dict:
    store = get_store()
    store.delete_by_source(source_path)
    _rebuild_keyword_index()
    return {"removed": source_path, "total_chunks": store.count()}


def reset_index() -> dict:
    store = get_store()
    store.reset()
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

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
from .keyword_index import get_keyword_index
from .vector_store import get_store


def index_file(path: Path) -> dict:
    """Ingest and index a single file. Returns a summary dict."""
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
    rel = str(path.relative_to(settings.raw_dir)) if _is_under(path, settings.raw_dir) else str(path)
    store.delete_by_source(rel)
    added = store.add_chunks(chunks)

    # Rebuild keyword index from the full authoritative chunk set.
    _rebuild_keyword_index()

    return {
        "file": path.name,
        "source_path": rel,
        "sections": len(sections),
        "chunks_indexed": added,
    }


def index_raw_dir() -> dict:
    """Index every supported file currently in the raw data directory."""
    results = []
    errors = []
    for path in sorted(settings.raw_dir.rglob("*")):
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
            try:
                results.append(index_file(path))
            except Exception as e:  # keep going; report per-file failures
                errors.append({"file": path.name, "error": str(e)})
    return {
        "files_indexed": len(results),
        "results": results,
        "errors": errors,
        "total_chunks": get_store().count(),
    }


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

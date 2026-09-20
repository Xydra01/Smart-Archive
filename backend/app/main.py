"""FastAPI application: the HTTP surface for the local AI search engine.

Endpoints
----------
GET  /api/health            liveness + model readiness
GET  /api/stats             index stats (chunk counts, sources)
POST /api/upload            upload one or more files into data/raw
POST /api/index             (re)index everything in data/raw
POST /api/index/file        index a single named file already in data/raw
POST /api/search            hybrid search -> ranked chunks (no LLM)
POST /api/ask               RAG answer (streaming) with citations
DELETE /api/source          remove one source from the index
POST /api/reset             wipe the whole index
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .config import settings
from .indexing import indexer
from .indexing.jobs import manager as job_manager
from .indexing.vector_store import get_store
from .ingestion.loaders import SUPPORTED_EXTENSIONS
from .llm import ollama_client, rag

app = FastAPI(title="Smart Archive", version="0.1.0")

# Local-only dev: the Next.js frontend runs on :3000.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------
class SearchRequest(BaseModel):
    query: str
    top_k: int | None = None


class AskRequest(BaseModel):
    question: str
    top_k: int | None = None


class SourceRequest(BaseModel):
    source_path: str


# --------------------------------------------------------------------------
# Health / stats
# --------------------------------------------------------------------------
@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "models": ollama_client.readiness()}


@app.get("/api/stats")
def stats() -> dict:
    from .indexing.manifest import get_manifest

    store = get_store()
    manifest = get_manifest()
    entries = manifest.entries()
    return {
        "total_chunks": store.count(),
        "sources": store.sources(),
        "supported_extensions": list(SUPPORTED_EXTENSIONS),
        "indexed_files": len(entries),
        "manifest": {
            rel: {"chunks": e.chunk_count, "size": e.size}
            for rel, e in sorted(entries.items())
        },
    }


# --------------------------------------------------------------------------
# Upload & index
# --------------------------------------------------------------------------
@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...)) -> dict:
    saved = []
    skipped = []
    for f in files:
        ext = Path(f.filename or "").suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            skipped.append({"file": f.filename, "reason": f"unsupported type {ext}"})
            continue
        # Guard against path traversal in the provided filename.
        safe_name = Path(f.filename or "upload").name
        dest = settings.raw_dir / safe_name
        with dest.open("wb") as out:
            shutil.copyfileobj(f.file, out)
        saved.append(safe_name)
    return {"saved": saved, "skipped": skipped}


@app.post("/api/index")
def index_all(force: bool = False) -> dict:
    """Start (bulk) indexing all files in data/raw as a background job.

    Returns immediately with a job id; poll /api/index/status for progress.
    Incremental by default — unchanged files (per the manifest) are skipped, so
    re-running after a restart is cheap. Pass ?force=true to re-embed everything.
    """
    # Avoid launching a second job while one is already running.
    latest = job_manager.latest()
    if latest and latest.status == "running":
        return {"job_id": latest.id, "status": latest.status, "already_running": True}

    job = job_manager.create("index")
    job_manager.run_in_thread(job, lambda j: indexer.run_index_job(j, force=force))
    return {"job_id": job.id, "status": job.status, "force": force}


class ImportFolderRequest(BaseModel):
    folder: str
    # Hard-link instead of copying (saves disk on the same filesystem).
    hardlink: bool = False


@app.post("/api/import-folder")
def import_folder(req: ImportFolderRequest) -> dict:
    """Import supported files from an external folder into data/raw.

    Copies (or hard-links) files preserving structure, then you can call
    /api/index to embed them. Does not index automatically so you can review
    what was imported first.
    """
    try:
        return indexer.import_folder(Path(req.folder), copy=not req.hardlink)
    except (NotADirectoryError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/index/status")
def index_status(job_id: str | None = None) -> dict:
    job = job_manager.get(job_id) if job_id else job_manager.latest()
    if job is None:
        return {"status": "idle", "message": "No indexing job has run yet."}
    return job.to_dict()


class IndexFileRequest(BaseModel):
    filename: str


@app.post("/api/index/file")
def index_one(req: IndexFileRequest) -> dict:
    path = settings.raw_dir / Path(req.filename).name
    if not path.exists():
        raise HTTPException(
            status_code=404, detail=f"File not found in raw dir: {req.filename}"
        )
    try:
        return indexer.index_file(path)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# --------------------------------------------------------------------------
# Search & ask
# --------------------------------------------------------------------------
@app.post("/api/search")
def search(req: SearchRequest) -> dict:
    from .search.hybrid import hybrid_search

    if not req.query.strip():
        raise HTTPException(status_code=400, detail="Empty query")
    hits = hybrid_search(req.query, top_k=req.top_k)
    return {"query": req.query, "results": hits}


@app.post("/api/ask")
def ask(req: AskRequest) -> StreamingResponse:
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="Empty question")

    def event_stream():
        for kind, payload in rag.answer_stream(req.question, top_k=req.top_k):
            yield json.dumps({"type": kind, "data": payload}) + "\n"

    return StreamingResponse(event_stream(), media_type="application/x-ndjson")


# --------------------------------------------------------------------------
# Source management
# --------------------------------------------------------------------------
@app.delete("/api/source")
def remove_source(req: SourceRequest) -> dict:
    return indexer.remove_source(req.source_path)


@app.post("/api/reset")
def reset() -> dict:
    return indexer.reset_index()

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
GET  /api/selectable-sources  indexed source_paths + chunk counts (for scoping)
GET  /api/references        list per-source bibliographic metadata (effective)
GET  /api/references/{path} fetch one source's effective metadata
PUT  /api/references/{path}  update one source's bibliographic fields
POST /api/references/format  format citations for 1..100 sources in MLA|APA
POST /api/references/{path}/import  import metadata (bibtex|ris|csljson|verbatim)
GET  /api/groups            list saved source groups
POST /api/groups            create a group
GET  /api/groups/{id}       fetch one group (with member presence flags)
PATCH /api/groups/{id}      rename a group
DELETE /api/groups/{id}     delete a group
POST /api/groups/{id}/sources    add sources to a group
DELETE /api/groups/{id}/sources  remove sources from a group
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
from .groups.store import Group, get_group_store
from .indexing import indexer
from .indexing.jobs import manager as job_manager
from .indexing.vector_store import get_store
from .ingestion.loaders import SUPPORTED_EXTENSIONS
from .llm import ollama_client, rag
from .references.display_name import display_name as resolve_display_name
from .references.effective import effective_record
from .references.formatter import (
    CitationStyle,
    format_citation,
    required_fields,
)
from .references.import_parsers import (
    ParseError,
    ParsedVerbatim,
    parse_structured,
    parse_verbatim,
)
from .references.store import (
    SOURCE_TYPES,
    get_source_metadata_store,
)
from .search.scope import QueryScope

from typing import Optional

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
    # Optional scope: an ad-hoc source selection and/or a saved group.
    # Both default to None so existing callers stay whole-archive.
    sources: list[str] | None = None
    group_id: str | None = None


class AskRequest(BaseModel):
    question: str
    top_k: int | None = None
    sources: list[str] | None = None
    group_id: str | None = None


class SourceRequest(BaseModel):
    source_path: str


class GroupNameRequest(BaseModel):
    name: str


class GroupSourcesRequest(BaseModel):
    sources: list[str]


# -- References / citation-formatting models -------------------------------
class SourceMetadataView(BaseModel):
    """The effective (stored + defaults) metadata for one indexed source.

    ``display_name``, ``missing_required`` and ``is_complete`` are derived at
    read time for the UI (Req 4.1, 9.3). ``missing_required`` is the
    style-agnostic base set for the current ``source_type`` (the MLA website
    access-date rule is a formatter/style concern and is not applied here).
    """

    source_path: str
    source_type: str
    authors: list[str]
    title: Optional[str] = None
    container: Optional[str] = None
    publisher: Optional[str] = None
    publication_date: Optional[str] = None
    url: Optional[str] = None
    access_date: Optional[str] = None
    verbatim_overrides: dict[str, str] = {}
    # derived, for the UI:
    display_name: str
    missing_required: list[str]
    is_complete: bool


class UpdateMetadataRequest(BaseModel):
    """A partial update: only the provided (non-``None``) fields are persisted.

    ``verbatim_overrides`` is intentionally absent here — overrides are set via
    the import endpoint, not this update.
    """

    source_type: Optional[str] = None
    authors: Optional[list[str]] = None
    title: Optional[str] = None
    container: Optional[str] = None
    publisher: Optional[str] = None
    publication_date: Optional[str] = None
    url: Optional[str] = None
    access_date: Optional[str] = None


class FormatRequest(BaseModel):
    source_paths: list[str]  # validated 1..100 in the handler
    style: str  # validated MLA|APA in the handler


class FormattedCitationView(BaseModel):
    source_path: str
    text: str
    incomplete: bool
    missing_required: list[str]
    leading_element: Optional[str] = None


class FormatResponse(BaseModel):
    style: str
    citations: list[FormattedCitationView]


class ImportRequest(BaseModel):
    format: str  # validated bibtex|ris|csljson|verbatim in the handler
    payload: str  # the raw reference text
    entry: Optional[str] = None  # index or key selecting one entry (structured)
    style: Optional[str] = None  # MLA|APA; required (only) when format==verbatim


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
# Scope resolution
# --------------------------------------------------------------------------
def resolve_scope(sources: list[str] | None, group_id: str | None) -> QueryScope:
    """Resolve a request's optional selection into a QueryScope.

    Precedence (deterministic):
      1. A non-empty ad-hoc ``sources`` list wins over ``group_id`` entirely.
         Its size is validated against ``settings.max_selection``.
      2. Otherwise a ``group_id`` is resolved to its current members; an
         unknown id is a 404.
      3. Otherwise (sources omitted/empty and no group_id) the query runs
         against the whole archive.

    Note: an empty ad-hoc ``sources`` list ([]) maps to whole-archive — the
    frontend sends no selection to mean "everything". The zero-results path
    (empty Effective_Sources) comes only from a resolved group with no members
    or an all-stale selection, and is handled downstream in hybrid_search.
    """
    if sources is not None and len(sources) > 0:  # ad-hoc wins
        if len(sources) > settings.max_selection:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Selection of {len(sources)} sources exceeds maximum of "
                    f"{settings.max_selection}"
                ),
            )
        return QueryScope.of(sources)
    if group_id is not None:
        group = get_group_store().get(group_id)
        if group is None:
            raise HTTPException(status_code=404, detail=f"Group not found: {group_id}")
        return QueryScope.of(group.members)
    return QueryScope.whole_archive()


# --------------------------------------------------------------------------
# Search & ask
# --------------------------------------------------------------------------
def require_no_active_index_job() -> None:
    """Hard-block queries while an index job is running.

    On an 8GB machine the vision model (ingest) and the chat model (query)
    cannot both be resident, so we refuse queries during indexing rather than
    thrash memory. The structured ``code`` lets the frontend show a specific
    "paused for indexing" state instead of a generic error.
    """
    if job_manager.is_indexing():
        raise HTTPException(
            status_code=409,
            detail={
                "code": "indexing_in_progress",
                "message": "Indexing in progress — querying is paused until it finishes.",
            },
        )


@app.post("/api/search")
def search(req: SearchRequest) -> dict:
    from .search.hybrid import hybrid_search

    require_no_active_index_job()
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="Empty query")
    scope = resolve_scope(req.sources, req.group_id)
    hits = hybrid_search(req.query, top_k=req.top_k, scope=scope)
    return {"query": req.query, "results": hits}


@app.post("/api/ask")
def ask(req: AskRequest) -> StreamingResponse:
    require_no_active_index_job()
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="Empty question")
    # Resolve scope before the stream begins so a 400/404 surfaces as a normal
    # HTTP error rather than mid-stream.
    scope = resolve_scope(req.sources, req.group_id)

    def event_stream():
        for kind, payload in rag.answer_stream(
            req.question, top_k=req.top_k, scope=scope
        ):
            yield json.dumps({"type": kind, "data": payload}) + "\n"

    return StreamingResponse(event_stream(), media_type="application/x-ndjson")


# --------------------------------------------------------------------------
# Selectable sources
# --------------------------------------------------------------------------
@app.get("/api/selectable-sources")
def selectable_sources() -> dict:
    """List the source_paths currently in the index, with chunk counts.

    Populates the scope picker on the frontend.
    """
    store = get_store()
    meta_store = get_source_metadata_store()

    def _display_name_for(sp: str) -> str:
        stored = meta_store.get(sp)
        stored_title = stored.title if stored is not None else None
        return resolve_display_name(sp, stored_title, _source_file_for(sp))

    return {
        "sources": [
            {
                "source_path": sp,
                "chunks": n,
                "display_name": _display_name_for(sp),
            }
            for sp, n in sorted(store.sources().items())
        ]
    }


# --------------------------------------------------------------------------
# References (per-source bibliographic metadata + citation formatting)
# --------------------------------------------------------------------------
_STYLE_MAP = {"MLA": CitationStyle.MLA, "APA": CitationStyle.APA}
_MAX_FIELD_LEN = 2000


def _indexed_sources() -> dict[str, int]:
    """The authoritative set of indexed source_paths (source_path -> chunks)."""
    return get_store().sources()


def _require_indexed(source_path: str) -> None:
    """Raise 404 when ``source_path`` is not currently in the index."""
    if source_path not in _indexed_sources():
        raise HTTPException(
            status_code=404, detail=f"Source not in index: {source_path}"
        )


def _source_file_for(source_path: str) -> str:
    """The file name for a source: the final segment (basename) of source_path.

    The index keys sources by ``source_path`` and carries no separate stored
    display name, so the basename is the ``source_file`` fed to the effective
    record and the display-name resolver.
    """
    return Path(source_path).name or source_path


def _base_missing_required(record) -> list[str]:
    """Style-agnostic base missing-required set for the record's source_type.

    Uses the SAME presence rule as the formatter (a scalar field present iff it
    has >=1 non-whitespace character; ``authors`` present iff >=1 non-blank
    entry). Deliberately excludes the MLA website access-date rule — that is a
    formatter/style concern, not part of the view's base set.
    """
    required = required_fields(record.source_type)
    missing: list[str] = []
    for f in ("title", "authors", "container", "publisher", "url"):
        if f not in required:
            continue
        if f == "authors":
            if not any(
                isinstance(a, str) and a.strip() for a in (record.authors or [])
            ):
                missing.append("authors")
        else:
            value = getattr(record, f, None)
            if not (isinstance(value, str) and value.strip()):
                missing.append(f)
    return missing


def _build_view(source_path: str) -> SourceMetadataView:
    """Assemble the effective SourceMetadataView for one indexed source.

    Merges stored metadata with computed defaults (effective title fallback),
    resolves the display_name (stored title else file name), and computes the
    style-agnostic base missing_required / is_complete.
    """
    stored = get_source_metadata_store().get(source_path)
    source_file = _source_file_for(source_path)
    eff = effective_record(source_path, stored, source_file)

    missing = _base_missing_required(eff)
    name = resolve_display_name(
        source_path, stored.title if stored is not None else None, source_file
    )
    return SourceMetadataView(
        source_path=eff.source_path,
        source_type=eff.source_type,
        authors=list(eff.authors),
        title=eff.title,
        container=eff.container,
        publisher=eff.publisher,
        publication_date=eff.publication_date,
        url=eff.url,
        access_date=eff.access_date,
        verbatim_overrides=dict(eff.verbatim_overrides),
        display_name=name,
        missing_required=missing,
        is_complete=not missing,
    )


@app.get("/api/references")
def list_references() -> dict:
    """One effective SourceMetadataView per indexed source (Req 4.1, 4.2)."""
    return {"references": [_build_view(sp) for sp in sorted(_indexed_sources())]}


@app.get("/api/references/{source_path:path}")
def get_reference(source_path: str) -> SourceMetadataView:
    """The effective view for one indexed source; 404 if not indexed (Req 4.3, 4.4)."""
    _require_indexed(source_path)
    return _build_view(source_path)


@app.put("/api/references/{source_path:path}")
def update_reference(
    source_path: str, req: UpdateMetadataRequest
) -> SourceMetadataView:
    """Persist the provided fields for an indexed source (Req 4.5, 4.6, 4.7, 5.5)."""
    _require_indexed(source_path)

    # Reject an out-of-set source_type before touching the store (Req 4.7).
    if req.source_type is not None and req.source_type not in SOURCE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(f"source_type must be one of {', '.join(SOURCE_TYPES)}."),
        )

    # Enforce the 2000-char bound on each provided string field (Req 5.5).
    for name in (
        "title",
        "container",
        "publisher",
        "publication_date",
        "url",
        "access_date",
    ):
        value = getattr(req, name)
        if isinstance(value, str) and len(value) > _MAX_FIELD_LEN:
            raise HTTPException(
                status_code=400,
                detail=f"Field '{name}' exceeds {_MAX_FIELD_LEN} characters.",
            )
    if req.authors is not None:
        for author in req.authors:
            if isinstance(author, str) and len(author) > _MAX_FIELD_LEN:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Field 'authors' has an entry exceeding "
                        f"{_MAX_FIELD_LEN} characters."
                    ),
                )

    # Only the provided (non-None) fields are persisted.
    fields: dict = {}
    for name in (
        "source_type",
        "authors",
        "title",
        "container",
        "publisher",
        "publication_date",
        "url",
        "access_date",
    ):
        value = getattr(req, name)
        if value is not None:
            fields[name] = value

    get_source_metadata_store().upsert(source_path, fields)
    return _build_view(source_path)


@app.post("/api/references/format")
def format_references(req: FormatRequest) -> FormatResponse:
    """Format citations for 1..100 indexed sources in MLA|APA (Req 4.8, 4.9, 4.10, 1.5)."""
    n = len(req.source_paths)
    if n < 1 or n > 100:
        raise HTTPException(
            status_code=400,
            detail="source_paths must contain between 1 and 100 entries.",
        )

    style = _STYLE_MAP.get(req.style)
    if style is None:
        raise HTTPException(
            status_code=400,
            detail="style must be one of the supported styles: MLA, APA.",
        )

    # Every requested source must be indexed (Req 4.10).
    for sp in req.source_paths:
        _require_indexed(sp)

    citations: list[FormattedCitationView] = []
    store = get_source_metadata_store()
    for sp in req.source_paths:  # request order preserved
        stored = store.get(sp)
        eff = effective_record(sp, stored, _source_file_for(sp))
        formatted = format_citation(eff, style)
        citations.append(
            FormattedCitationView(
                source_path=sp,
                text=formatted.text,
                incomplete=formatted.incomplete,
                missing_required=list(formatted.missing_required),
                leading_element=formatted.leading_element,
            )
        )

    return FormatResponse(style=req.style, citations=citations)


@app.post("/api/references/{source_path:path}/import")
def import_reference(source_path: str, req: ImportRequest) -> SourceMetadataView:
    """Import metadata for an indexed source (Req 8.1-8.4, 8.7-8.9, 8.11, 8.12)."""
    _require_indexed(source_path)  # 404, store unchanged (Req 8.2)

    fmt = (req.format or "").strip().lower()
    if fmt not in ("bibtex", "ris", "csljson", "verbatim"):
        raise HTTPException(
            status_code=400,
            detail="format must be one of bibtex, ris, csljson, verbatim.",
        )

    store = get_source_metadata_store()

    # -- Verbatim: store the pasted text as a per-style override only ------
    if fmt == "verbatim":
        if req.style not in ("MLA", "APA"):
            raise HTTPException(
                status_code=400,
                detail="style must be one of MLA, APA for a verbatim import.",
            )
        parsed = parse_verbatim(req.payload, req.style)
        if isinstance(parsed, ParseError):
            raise HTTPException(
                status_code=400,
                detail=f"verbatim: {parsed.message}",
            )
        # parsed is a ParsedVerbatim; merge per-style, change nothing else.
        store.upsert(
            source_path,
            {"verbatim_overrides": {parsed.style: parsed.text}},
        )
        return _build_view(source_path)

    # -- Structured (bibtex / ris / csljson) -------------------------------
    result = parse_structured(fmt, req.payload)
    if isinstance(result, ParseError):
        raise HTTPException(
            status_code=400,
            detail=f"{fmt}: {result.message}",
        )

    entries = result  # list[ParsedEntry], file order
    if not entries:
        raise HTTPException(
            status_code=400,
            detail=f"{fmt}: no entries found.",
        )

    selected = None
    if req.entry is not None:
        for idx, e in enumerate(entries, start=1):
            if req.entry == e.key or req.entry == str(idx):
                selected = e
                break
        if selected is None:
            raise HTTPException(
                status_code=400,
                detail=f"{fmt}: no such entry: {req.entry}",
            )
    else:
        if len(entries) == 1:
            selected = entries[0]
        else:
            raise HTTPException(
                status_code=400,
                detail=(f"{fmt}: multiple entries; specify which (by key or index)."),
            )

    # Map the selected entry onto SourceMetadata fields, including only
    # non-None values so absent-stays-absent (Req 8.6). A present type
    # indicator sets source_type; an absent one leaves it unchanged (Req 8.7).
    fields: dict = {}
    if selected.source_type is not None:
        fields["source_type"] = selected.source_type
    if selected.authors:
        fields["authors"] = list(selected.authors)
    if selected.title is not None:
        fields["title"] = selected.title
    if selected.container is not None:
        fields["container"] = selected.container
    if selected.publisher is not None:
        fields["publisher"] = selected.publisher
    if selected.publication_date is not None:
        fields["publication_date"] = selected.publication_date
    if selected.url is not None:
        fields["url"] = selected.url

    store.upsert(source_path, fields)
    return _build_view(source_path)


# --------------------------------------------------------------------------
# Groups
# --------------------------------------------------------------------------
def _group_view(group: Group) -> dict:
    """Serialise a Group for the API.

    ``members`` is the group's saved selection (sorted); stale members are
    retained. ``present`` maps each member to whether its source_path is
    currently in the Vector_Store, computed at request time.
    """
    present_sources = get_store().sources()
    members = sorted(group.members)
    return {
        "group_id": group.group_id,
        "name": group.name,
        "members": members,
        "present": {m: m in present_sources for m in members},
    }


def _require_name(name: str) -> str:
    """Validate and normalise a group name; reject empty/whitespace-only."""
    trimmed = name.strip()
    if not trimmed:
        raise HTTPException(status_code=400, detail="Group name must not be empty")
    return trimmed


@app.get("/api/groups")
def list_groups() -> dict:
    return {"groups": [_group_view(g) for g in get_group_store().list()]}


@app.post("/api/groups")
def create_group(req: GroupNameRequest) -> dict:
    name = _require_name(req.name)
    group = get_group_store().create(name)
    return _group_view(group)


@app.get("/api/groups/{group_id}")
def get_group(group_id: str) -> dict:
    group = get_group_store().get(group_id)
    if group is None:
        raise HTTPException(status_code=404, detail=f"Group not found: {group_id}")
    return _group_view(group)


@app.patch("/api/groups/{group_id}")
def rename_group(group_id: str, req: GroupNameRequest) -> dict:
    name = _require_name(req.name)
    try:
        group = get_group_store().rename(group_id, name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Group not found: {group_id}")
    return _group_view(group)


@app.delete("/api/groups/{group_id}")
def delete_group(group_id: str) -> dict:
    try:
        get_group_store().delete(group_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Group not found: {group_id}")
    return {"deleted": group_id}


@app.post("/api/groups/{group_id}/sources")
def add_group_sources(group_id: str, req: GroupSourcesRequest) -> dict:
    try:
        group = get_group_store().add_sources(group_id, req.sources)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Group not found: {group_id}")
    return _group_view(group)


@app.delete("/api/groups/{group_id}/sources")
def remove_group_sources(group_id: str, req: GroupSourcesRequest) -> dict:
    try:
        group = get_group_store().remove_sources(group_id, req.sources)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Group not found: {group_id}")
    return _group_view(group)


# --------------------------------------------------------------------------
# Source management
# --------------------------------------------------------------------------
@app.delete("/api/source")
def remove_source(req: SourceRequest) -> dict:
    return indexer.remove_source(req.source_path)


@app.post("/api/reset")
def reset() -> dict:
    return indexer.reset_index()

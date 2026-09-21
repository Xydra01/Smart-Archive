# Design Document

## Overview

This feature makes the information locked inside document visuals — charts, graphs, diagrams, illustrations, figures, and image-based tables — searchable, by extracting it to text at ingest time with a local vision-language model (VLM, default `qwen2.5vl:3b` via Ollama). It is deliberately front-loaded: the expensive work happens once during indexing, is persisted like any other chunk, captured by the incremental manifest so unchanged files are never re-processed, and leaves query time untouched (the chat model reasons over indexed text alone).

Four coordinated changes deliver this, all additive and consistent with the existing local, single-user, background-job + manifest architecture:

1. **A vision extraction pipeline** (`app/ingestion/vision.py`) that, when enabled, renders document pages, finds visuals, and calls the VLM with targeted per-type prompts to produce searchable text. It fails soft per-visual (errors/timeouts skip one visual, never the file) and is bounded by an image cap.
2. **Native PDF table extraction** (pdfplumber) folded into the PDF loader as a model-free fast path, with the VLM as the fallback for image-only tables.
3. **An ingest/query mutual-exclusion lock**: while an index job runs, `/api/search` and `/api/ask` return `409 Conflict` so the 8 GB machine never holds the vision model and the chat model at once. The frontend disables querying and shows a "paused for indexing" state, re-enabling automatically.
4. **A `content_type` label** on every chunk (`text` | `table` | `chart` | `figure` | `ocr`), surfaced in search results and citations so visual-derived content is trustworthy, locatable, and (later) filterable.

The single anchoring idea: **loaders emit typed sections, the chunker already propagates section metadata, and the vision pipeline is just another producer of sections** — so visual content rides the exact same chunk → embed → BM25 → retrieve → cite path as everything else, with `content_type` as the only new dimension.

## Architecture

### Where each piece lives

```mermaid
flowchart TD
    subgraph Ingest [Index Job (background thread)]
        IDX[indexer.index_file]
        LOAD[loaders.load_document]
        NTE[pdfplumber Native_Table_Extraction]
        VIS[vision.extract_visuals]
        VLM[(Ollama VLM: qwen2.5vl:3b)]
        CH[chunker.chunk_sections + content_type]
        EMB[(embed -> Chroma)]
        BM[(BM25 index)]
        MAN[(manifest.record)]
    end

    subgraph Query [Query phase - blocked during ingest]
        SEARCH[/api/search/]
        ASK[/api/ask/]
        HS[hybrid_search]
        RAG[rag answer_stream]
    end

    subgraph Lock
        GUARD[require_no_active_index_job -> 409]
    end

    IDX --> LOAD
    LOAD -->|PDF pages| NTE
    LOAD -->|"if Vision_Enabled"| VIS
    VIS --> VLM
    NTE --> CH
    VIS --> CH
    LOAD --> CH
    CH --> EMB
    CH --> BM
    IDX --> MAN

    SEARCH --> GUARD --> HS
    ASK --> GUARD --> RAG
    GUARD -.->|"job running"| BLOCK[409 indexing_in_progress]
```

### Ingest and query never overlap

The lock is the linchpin that makes vision on 8 GB safe. `run_index_job` sets a job to `running` (already happens in `JobManager.run_in_thread`). A new guard, consulted by both query endpoints, refuses requests while any job is `running`. Therefore:

- **During ingest:** resident models are `nomic-embed-text` (~0.3 GB) + the VLM (~3 GB). Fits comfortably in 8 GB.
- **During query:** the VLM is idle; the chat model loads. Ollama swaps models as calls arrive.
- **Never simultaneously**, because a query is refused while a job runs.

This also means we do not need any in-process model-memory juggling — the OS/Ollama handle it, and the lock guarantees the phases are disjoint.

### Vision extraction as a section producer

The chunker already turns `LoadedSection(text, location, meta)` into chunks and flattens scalar `meta` values into Chroma metadata. So the entire feature reduces to: **produce more sections, each tagged with a `content_type` in its `meta`.** No change to the embed/index/retrieve core is required beyond carrying the new metadata field and the result shape.

## Components and Interfaces

### 1. Configuration (`app/config.py`)

New settings (all `ARCHIVE_`-overridable), grouped under a "Vision / ingest" comment:

```python
# --- Vision ingest (opt-in; slow, so default off) ---
vision_enabled: bool = False                 # Req 1.1
vision_model: str = "qwen2.5vl:3b"           # Req 6.1
vision_timeout_s: float = 120.0              # per-visual timeout (Req 5.2)
vision_max_images_per_page: int = 6          # Image_Cap per page (Req 7.1)
vision_max_images_per_file: int = 400        # Image_Cap per file (Req 7)
vision_min_image_pixels: int = 64 * 64       # ignore tiny/decorative images
vision_render_dpi: int = 150                 # page render DPI for scanned-page figures
```

### 2. Content types (`app/ingestion/content_types.py` — small shared module)

A single source of truth for the allowed values so loaders, the vision module, and tests agree:

```python
CONTENT_TEXT = "text"
CONTENT_TABLE = "table"
CONTENT_CHART = "chart"
CONTENT_FIGURE = "figure"
CONTENT_OCR = "ocr"
CONTENT_TYPES = frozenset({CONTENT_TEXT, CONTENT_TABLE, CONTENT_CHART, CONTENT_FIGURE, CONTENT_OCR})
```

### 3. LoadedSection gains a content type (`app/ingestion/loaders.py`)

`LoadedSection.meta` already flows to chunk metadata. We standardize a `content_type` key in `meta`, defaulting to `text`:

```python
@dataclass
class LoadedSection:
    text: str
    location: str
    meta: dict = field(default_factory=dict)   # meta["content_type"] in CONTENT_TYPES

    def __post_init__(self):
        self.meta.setdefault("content_type", CONTENT_TEXT)
```

Every existing loader keeps returning `text` sections (Req 12). The DOCX table branch sets `meta["content_type"] = "table"` (Req 4.3). No other behavior changes when vision is off.

### 4. Chunker carries content_type (`app/ingestion/chunker.py`)

`chunk_sections` already copies `section.meta` into `Chunk.extra`, and `to_metadata()` flattens scalars. We add a first-class `content_type` on the chunk for clarity and guaranteed presence:

- In `chunk_sections`, read `extra.get("content_type", CONTENT_TEXT)` and include `content_type` in `to_metadata()` output (Req 4.1, 4.7).
- The chunk id already incorporates location+index+text, so visual chunks get stable ids and are idempotent on re-ingest.

### 5. Vision extractor (`app/ingestion/vision.py` — new)

The core new module. Pure functions plus a small class; no global state. Its job: given a Source, yield `LoadedSection`s for its visuals.

```python
def vision_available() -> bool:
    """True if Vision_Enabled and the configured VLM is installed in Ollama."""

def extract_pdf_visuals(path: Path, job=None) -> list[LoadedSection]:
    """Render pages, find images/figures, and VLM-extract each (bounded by caps)."""

def extract_docx_images(path: Path, job=None) -> list[LoadedSection]: ...
def extract_epub_images(path: Path, job=None) -> list[LoadedSection]: ...
def extract_html_images(path: Path, job=None) -> list[LoadedSection]: ...
```

**Image discovery.** Use PyMuPDF (`pymupdf`) to enumerate embedded images per page and to render full pages (for scanned/image-only pages where figures aren't separable embedded objects). Filter out images below `vision_min_image_pixels` (icons, rules, bullets). Apply the per-page and per-file `Image_Cap`; when exceeded, process up to the cap and note the remainder skipped (Req 7.2).

**Classification + prompt routing.** The VLM itself decides the visual's nature via a single structured prompt that asks it to (a) name the visual type (chart/table/diagram/figure/photo), and (b) extract the appropriate information. We map its declared type to a `content_type`:
- chart/graph → `chart` (prompt asks for title, axes, series, notable values, trend — Req 2.4)
- table → `table` (prompt asks for the table transcribed as text/markdown)
- diagram/illustration/figure/photo → `figure` (prompt asks for description + any embedded text/labels — Req 2.5)
- A full scanned page rendered and read for text → `ocr`

Each produced `LoadedSection` gets `location` like `"p. 12 (figure 2)"` and `meta={"content_type": ...}` (Req 2.3).

**VLM call.** Via the existing Ollama client shape confirmed available: `client.generate(model=vision_model, prompt=<type-specific>, images=[png_bytes], options={...})`. A thin helper in `ollama_client.py` wraps this:

```python
def vision_extract(image_bytes: bytes, prompt: str, timeout_s: float) -> str: ...
```

**Failure isolation + timeout (Req 5).** Each visual is processed in a try/except with a wall-clock timeout (run the VLM call in a worker thread and join with `vision_timeout_s`; on timeout, abandon that visual). Any exception or timeout increments a per-file `visuals_skipped` counter recorded on the job result (Req 5.3) and continues. A single bad image never aborts the file (Req 5.1, 5.4).

### 6. Native PDF table extraction (`app/ingestion/loaders.py`, using pdfplumber)

`load_pdf` is extended so that, per page:

1. Open the page with pdfplumber; call `page.extract_tables()`.
2. For each detected table with a text layer, emit a `LoadedSection` with the table rendered as pipe/markdown rows, `location="p. N (table K)"`, `meta={"content_type": "table"}` (Req 3.1, 3.2).
3. Emit the page's remaining text (pypdf or pdfplumber `extract_text`) as a `text` section (Req 3.5). Existing behavior of skipping empty pages is preserved.

Native table extraction runs regardless of `Vision_Enabled` (Req 3.4, 4.3). It requires no model. Image-only tables (no text layer) produce no native table and, when vision is on, are picked up as visuals by the VLM path (Req 3.3).

To keep pdfplumber and pypdf from double-emitting the same body text, the PDF loader will prefer pdfplumber for both text and tables on a page when pdfplumber is available, falling back to pypdf text if pdfplumber errors on a page.

### 7. Indexer integration (`app/indexing/indexer.py`)

`index_file` composes sections from three producers, then chunks them together:

```python
sections = load_document(path)                       # text + native tables (+ docx tables)
if settings.vision_enabled and vision_available():
    sections += extract_visuals_for(path, job)       # chart/figure/table/ocr sections
chunks = chunk_sections(sections, ...)               # unchanged; carries content_type
```

- `extract_visuals_for` dispatches by extension to the right `vision.extract_*` function; unsupported-for-vision types contribute nothing.
- VLM availability is checked once per job start; if unavailable, a message is set on the job and vision is skipped for all files, but text/native-table indexing proceeds (Req 6.2–6.4).
- The manifest already records `Source_Path` + size + mtime + chunk_count; because visual chunks are part of the same `add_chunks` call, an unchanged file is skipped wholesale on re-ingest without re-invoking the VLM (Req 11.1). A changed file or a force re-index re-runs everything including vision (Req 11.2, 11.3). Source removal already deletes all chunks by `source_path`, including visual ones (Req 11.4).

**Progress (Req 8).** The `Job` dataclass gains two optional fields: `current_stage: str` (e.g. `"extracting visuals"` / `"embedding"`) and `visuals_done`/`visuals_total` counters for the current file. `extract_visuals_for` updates these as it processes visuals, so `/api/index/status` reflects vision progress.

### 8. Ingest/query lock (`app/main.py`, `app/indexing/jobs.py`)

Add a helper on the job manager and a guard used by both query endpoints:

```python
# jobs.py
class JobManager:
    def is_indexing(self) -> bool:
        j = self.latest()
        return j is not None and j.status == "running"

# main.py
def require_no_active_index_job() -> None:
    if job_manager.is_indexing():
        raise HTTPException(
            status_code=409,
            detail={"code": "indexing_in_progress",
                    "message": "Indexing in progress — querying is paused until it finishes."},
        )
```

`/api/search` and `/api/ask` call `require_no_active_index_job()` before doing any work (Req 9.1–9.3). For `/api/ask`, the check happens before the streaming generator is created so the 409 is a normal HTTP error, not a mid-stream failure. Because the guard keys off job status, completion (success or error) immediately re-opens querying with no extra action (Req 9.4). The structured `code: "indexing_in_progress"` lets the frontend distinguish this from generic errors (Req 9.5).

### 9. Result/citation content_type (`app/search/hybrid.py`, `app/llm/rag.py`, `app/main.py`)

- `hybrid_search` results already pass through chunk `metadata`; ensure `content_type` is included in each result's metadata (it will be, since it's in Chroma metadata). No structural change needed beyond confirming it is surfaced.
- `rag._format_context` adds `content_type` to each citation dict alongside `source_file`, `location`, `file_type` (Req 4.6).
- `/api/search` results already return full `metadata`; `content_type` rides along.

### 10. Frontend (`frontend/app/api.ts`, `frontend/app/page.tsx`)

- `api.ts`: `search`/`ask` detect a `409` with `detail.code === "indexing_in_progress"` and throw a typed error (or return a sentinel) the page can recognize (Req 9.5, 10.4). Add `content_type` to the `Citation` and search-result metadata types (Req 4.6).
- `page.tsx`: the page already polls index status. While a job is `running`, disable the Ask/Search button and show a "Querying paused — indexing in progress" notice (Req 10.1, 10.2). When status transitions away from `running`, re-enable automatically (Req 10.3). If a query is nonetheless refused with `indexing_in_progress`, show the same paused state (Req 10.4). Optionally show each result/citation's `content_type` as a small tag (e.g. "chart", "figure") — reuses the existing `.tag` styling.

### 11. Dependencies (`backend/requirements.txt`)

- `pymupdf` — page rendering + embedded-image enumeration for the vision path.
- `pdfplumber` — native table extraction.
- `Pillow` — image normalization/encoding for the VLM call (pdfplumber pulls it in; declared explicitly).
The VLM itself is pulled via Ollama (`ollama pull qwen2.5vl:3b`), not a Python dependency.

## Data Models

### Chunk metadata (extended)

`to_metadata()` output gains one field:

```
content_type: str   # one of text | table | chart | figure | ocr
```

All existing fields (source_file, source_path, file_type, location, chunk_index, total_chunks, token_count, and scalar extras like page) are unchanged (Req 4.7).

### LoadedSection.meta convention

```
meta = {
  "content_type": "chart",          # required (defaulted to "text" in __post_init__)
  "page": 12,                        # existing per-format scalars still allowed
  ... other scalar metadata ...
}
```

### Job (extended)

```
current_stage: str | None      # "loading" | "extracting visuals" | "embedding" | None
visuals_total: int             # visuals detected in the current file
visuals_done: int              # visuals processed so far in the current file
```

Plus per-file result entries may include `"visuals_indexed": int` and `"visuals_skipped": int` (Req 5.3, 7.2). `to_dict()` serializes these for `/api/index/status`.

### Query-lock error (API response)

```
HTTP 409
{ "detail": { "code": "indexing_in_progress",
              "message": "Indexing in progress — querying is paused until it finishes." } }
```

### Vision VLM contract

- Input: PNG/JPEG image bytes + a type-specific prompt.
- Output: plain text. For a chart: a compact textual rendering of title, axes, series, notable values, trend. For a table: the table as pipe/markdown rows. For a figure/diagram: a description plus any embedded labels/text. For a full scanned page: the page's readable text.

## Correctness Properties

*A property is a characteristic or behavior that should hold across all valid executions — a machine-checkable statement of intended behavior.*

### Property 1: Vision off is behavior-preserving

*For any* Source and *any* content, indexing with `vision_enabled=False` produces the same set of text/table chunks (same ids, text, and metadata except the added `content_type`) as the pre-feature system, and never invokes the VLM.

**Validates: Requirements 1.2, 12.1, 12.3**

### Property 2: Every chunk has a valid content_type

*For any* indexed Source, every produced Chunk carries a `content_type` in {`text`, `table`, `chart`, `figure`, `ocr`}, and every non-text chunk still carries source_path, location, and chunk_index.

**Validates: Requirements 4.1, 4.7**

### Property 3: Content_type matches the producer

*For any* section producer, the resulting chunks' `content_type` equals the producer's kind: body text → `text`, native/DOCX/VLM table → `table`, VLM chart → `chart`, VLM diagram/figure → `figure`, full-page OCR → `ocr`.

**Validates: Requirements 4.2, 4.3, 4.4, 4.5**

### Property 4: Per-visual failure isolation

*For any* sequence of visuals where an arbitrary subset raise errors or exceed the timeout, `extract_visuals` returns sections for exactly the successful visuals, records the count of skipped visuals, and never propagates the error.

**Validates: Requirements 5.1, 5.2, 5.3, 5.4**

### Property 5: Image cap is respected

*For any* page with N visuals and per-page cap C, at most C visuals are processed for that page; if N > C the overflow is recorded as skipped-by-cap.

**Validates: Requirements 7.1, 7.2**

### Property 6: VLM unavailability degrades, never aborts

*For any* Source, when `vision_enabled=True` but the configured VLM is unavailable, indexing completes with all text and native-table chunks present, no VLM call is made, and the job records an "unavailable" message without an error status.

**Validates: Requirements 6.2, 6.3, 6.4**

### Property 7: Native tables preserve structure and type

*For any* digital PDF page containing a text-layer table, the produced table chunk contains the table's cell values grouped by row (row separators present) and has `content_type` `table`, independent of `vision_enabled`.

**Validates: Requirements 3.1, 3.2, 3.4**

### Property 8: Query is blocked exactly while indexing

*For any* job state, a call to `/api/search` or `/api/ask` is refused with HTTP 409 and code `indexing_in_progress` if and only if the latest job status is `running`; when it is not `running`, the endpoints serve normally.

**Validates: Requirements 9.1, 9.2, 9.3, 9.4**

### Property 9: Blocked query does no work

*While* a job is running, a refused query performs no retrieval and, for `/api/ask`, no generation (the guard raises before any retrieval/generation call).

**Validates: Requirements 9.1, 9.2**

### Property 10: Manifest skip avoids re-extraction

*For any* Source already indexed and unchanged (same size + mtime), the next Index_Job skips it — no loader, no native-table, and no VLM work — regardless of `vision_enabled`.

**Validates: Requirements 11.1, 11.2**

### Property 11: Content_type surfaces in results and citations

*For any* retrieved chunk, the search result metadata and the Ask citation for it include its `content_type` and `location`.

**Validates: Requirements 4.6**

## Error Handling

| Condition | Where | Handling |
| --- | --- | --- |
| Query while indexing | `require_no_active_index_job` in main.py | HTTP 409 `{code: indexing_in_progress}`; no retrieval/generation (Req 9) |
| VLM not installed | `vision_available()` at job start | Record message on job; skip vision for all files; text/tables still indexed; job not errored (Req 6.2–6.4) |
| Single visual errors | `vision.extract_*` per-visual try/except | Skip that visual, increment `visuals_skipped`, continue (Req 5.1, 5.3) |
| Single visual times out | worker-thread join with `vision_timeout_s` | Abandon that visual, increment `visuals_skipped`, continue (Req 5.2) |
| Too many visuals on a page | Image_Cap check | Process up to cap, record overflow skipped (Req 7.2) |
| pdfplumber errors on a page | PDF loader per-page try/except | Fall back to pypdf text for that page (Req 3.5) |
| Tiny/decorative image | `vision_min_image_pixels` filter | Ignored (not a failure) |
| Corrupt/undecodable image bytes | `vision.extract_*` per-visual try/except | Treated as a per-visual error: skipped, counted, continue (Req 5.1) |
| Ollama call transport error | `ollama_client.vision_extract` | Propagates to per-visual handler → skipped/counted (Req 5.1) |

Design choices:
- **Blocked query is a first-class outcome, not a failure**: it uses 409 with a structured `code` so the UI can present "paused for indexing" specifically (Req 9.5, 10.4).
- **Vision is best-effort**: any visual that can't be read is silently dropped from the index (with a count), never blocking the file's text from being searchable.

## Testing Strategy

Backend tests use pytest + Hypothesis (already set up). VLM and Ollama are always faked in tests — no model runs, no network — by monkeypatching `ollama_client.vision_extract` and `vision_available`. PDF/image handling is tested with tiny synthetic fixtures.

- **Property-based (Hypothesis, ≥100 examples), tagged `Feature: vision-ingest, Property N`:**
  - P1 vision-off equivalence: build sections with vision off vs the pre-feature path; assert identical chunks modulo `content_type`.
  - P2/P3 content_type invariants: for generated section producers, every chunk has a valid `content_type` matching its producer.
  - P4 failure isolation: a fake VLM that raises/times out on an arbitrary subset of visuals yields sections for exactly the successes and the right skipped count.
  - P5 image cap: for generated (N visuals, cap C), at most C processed.
  - P8 query-lock iff running: for generated job states, `/api/search` and `/api/ask` return 409 iff status == running (via TestClient with a faked job manager).
- **Example/integration tests:**
  - P6 VLM-unavailable: `vision_available` false → text/tables indexed, no VLM call, job message set, status not error.
  - P7 native tables: a tiny synthetic PDF (or a pdfplumber-mocked page) with a table → a `table` chunk with row structure, vision off.
  - P9 blocked query does no work: monkeypatch `hybrid_search`/`rag` to record calls; assert zero calls when blocked.
  - P10 manifest skip: index once (fake VLM), then re-run; assert the file is skipped and the fake VLM is not called on the second run.
  - P11 citations/results carry `content_type`: fake hits with content types → `/api/search` and the Ask citations include them.
- **Frontend (Vitest + Testing Library):** while index status is `running`, Ask/Search controls are disabled and the paused notice shows; when status leaves `running`, controls re-enable; a `409 indexing_in_progress` from a submitted query renders the paused state; result/citation `content_type` tags render.
- **Isolation:** all tests use tmp paths and fakes; the real `data/`, Ollama, Chroma, and the VLM are never touched. A real VLM end-to-end pass is a manual verification step (documented, not in CI), consistent with the model-dependent smoke tests in prior features.

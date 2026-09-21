# Implementation Plan: Vision Ingest

## Overview

This plan adds ingest-time visual extraction (VLM), native PDF table extraction (pdfplumber), an ingest/query hard-block lock, and `content_type` chunk metadata — all additive to the existing loader → chunk → embed/BM25 → retrieve → cite pipeline. Work is sequenced so the model-free, always-on pieces (content types, native tables, the lock, metadata plumbing) land and are tested first, then the opt-in vision pipeline, then the frontend, then verification.

Backend is Python/FastAPI with a venv at `backend/.venv`; tests use pytest + Hypothesis (already installed). VLM/Ollama are always faked in tests. Tasks marked `*` are optional (tests/verification) and can be skipped for a faster MVP; core sub-tasks are never optional.

## Tasks

- [ ] 1. Configuration and content-type constants
  - [ ] 1.1 Add vision/lock settings to `backend/app/config.py`
    - `vision_enabled: bool = False`, `vision_model: str = "qwen2.5vl:3b"`, `vision_timeout_s: float = 120.0`, `vision_max_images_per_page: int = 6`, `vision_max_images_per_file: int = 400`, `vision_min_image_pixels: int = 4096`, `vision_render_dpi: int = 150`
    - _Requirements: 1.1, 1.4, 6.1, 5.2, 7.1, 7.3_
  - [ ] 1.2 Create `backend/app/ingestion/content_types.py` with the allowed set
    - `CONTENT_TEXT/TABLE/CHART/FIGURE/OCR` constants and `CONTENT_TYPES` frozenset
    - _Requirements: 4.1_
  - [ ]* 1.3 Unit test: `CONTENT_TYPES` equals the documented five values
    - _Requirements: 4.1_

- [ ] 2. Thread content_type through sections and chunks
  - [ ] 2.1 Add `content_type` default to `LoadedSection` in `backend/app/ingestion/loaders.py`
    - `__post_init__` sets `meta.setdefault("content_type", CONTENT_TEXT)`; DOCX table branch sets `content_type = "table"`
    - _Requirements: 4.1, 4.2, 4.3, 12.1_
  - [ ] 2.2 Surface `content_type` in `backend/app/ingestion/chunker.py`
    - Read `extra.get("content_type", CONTENT_TEXT)`; include `content_type` in `Chunk.to_metadata()`
    - _Requirements: 4.1, 4.7_
  - [ ]* 2.3 Property test: every chunk has a valid content_type; non-text chunks keep provenance
    - **Property 2** — **Validates: Requirements 4.1, 4.7**
  - [ ]* 2.4 Property test: content_type matches the producer kind
    - **Property 3** — **Validates: Requirements 4.2, 4.3, 4.4, 4.5**

- [ ] 3. Native PDF table extraction (pdfplumber)
  - [ ] 3.1 Add `pdfplumber` (and `Pillow`) to `backend/requirements.txt` and install into `.venv`
    - _Requirements: 3.1_
  - [ ] 3.2 Extend `load_pdf` in `loaders.py` with per-page table + text extraction
    - Per page: pdfplumber `extract_tables()` → `table` sections (`location="p. N (table K)"`, pipe/markdown rows); remaining page text → `text` section; per-page try/except falls back to pypdf text on error; preserve empty-page skipping
    - _Requirements: 3.1, 3.2, 3.4, 3.5, 12.1_
  - [ ]* 3.3 Test native tables preserve structure and type (vision off)
    - Tiny synthetic PDF or pdfplumber-mocked page → `table` chunk with row separators, `content_type == "table"`
    - **Property 7** — **Validates: Requirements 3.1, 3.2, 3.4**

- [ ] 4. Ingest/query hard-block lock
  - [ ] 4.1 Add `is_indexing()` to `JobManager` in `backend/app/indexing/jobs.py`
    - Returns True iff `latest()` status == `running`
    - _Requirements: 9.3_
  - [ ] 4.2 Add `require_no_active_index_job()` guard in `backend/app/main.py` and call it in `/api/search` and `/api/ask`
    - Raises `HTTPException(409, detail={"code": "indexing_in_progress", "message": ...})`; for `/api/ask` the check runs before the streaming generator is created
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5_
  - [ ]* 4.3 Property test: query blocked iff a job is running
    - TestClient + faked job manager over generated statuses; 409 + code iff running
    - **Property 8** — **Validates: Requirements 9.1, 9.2, 9.3, 9.4**
  - [ ]* 4.4 Test: a blocked query performs no retrieval/generation
    - Monkeypatch hybrid_search/rag to record calls; assert zero when blocked
    - **Property 9** — **Validates: Requirements 9.1, 9.2**

- [ ] 5. Checkpoint — model-free pieces green
  - Ensure all tests so far pass; ask the user if questions arise.

- [ ] 6. Ollama vision helper
  - [ ] 6.1 Add `vision_extract(image_bytes, prompt, timeout_s)` and `vision_available()` to `backend/app/llm/ollama_client.py`
    - `vision_extract` calls `client.generate(model=settings.vision_model, prompt=..., images=[image_bytes], options=...)`, run in a worker thread joined with the timeout; `vision_available()` returns `settings.vision_enabled and <model installed>` (reuse existing model-listing logic)
    - _Requirements: 6.1, 6.2, 5.2_
  - [ ]* 6.2 Test: `vision_available()` reflects flag + installed models (mocked list)
    - _Requirements: 6.1, 6.2_

- [ ] 7. Vision extractor module
  - [ ] 7.1 Add `pymupdf` to `backend/requirements.txt` and install into `.venv`
    - _Requirements: 2.1_
  - [ ] 7.2 Create `backend/app/ingestion/vision.py` — discovery, prompting, extraction
    - PDF: enumerate embedded images per page + render pages (PyMuPDF); filter by `vision_min_image_pixels`; apply per-page/per-file `Image_Cap`; per-visual try/except + timeout; map declared visual type → content_type (chart/table/figure/ocr); build `LoadedSection`s with `location="p. N (figure K)"` and `meta["content_type"]`; update optional `job` progress/counters; return sections + skipped count
    - docx/epub/html image extraction helpers (embedded images)
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 3.3, 5.1, 5.2, 5.3, 5.4, 7.1, 7.2, 8.1_
  - [ ]* 7.3 Property test: per-visual failure isolation (errors + timeouts)
    - Fake `vision_extract` raising/timing out on a subset → sections for successes only, correct skipped count, never raises
    - **Property 4** — **Validates: Requirements 5.1, 5.2, 5.3, 5.4**
  - [ ]* 7.4 Property test: image cap respected
    - Generated (N visuals, cap C) → at most C processed; overflow recorded
    - **Property 5** — **Validates: Requirements 7.1, 7.2**

- [ ] 8. Wire vision into the indexer
  - [ ] 8.1 Compose visual sections in `index_file` / job start in `backend/app/indexing/indexer.py`
    - When `settings.vision_enabled and vision_available()`, append `vision.extract_*` sections before chunking; dispatch by extension; check VLM availability once per job (message on job if unavailable, continue with text/tables); add `Job.current_stage`, `visuals_total`, `visuals_done` and per-file `visuals_indexed`/`visuals_skipped`
    - _Requirements: 2.2, 6.2, 6.3, 6.4, 8.1, 8.2, 11.1, 11.2, 11.3, 11.4_
  - [ ] 8.2 Serialize new Job fields in `jobs.py` `to_dict()`
    - `current_stage`, `visuals_total`, `visuals_done`
    - _Requirements: 8.1, 8.2_
  - [ ]* 8.3 Test: VLM-unavailable degrades, never aborts
    - `vision_available` false → text/tables present, no VLM call, job message set, status not error
    - **Property 6** — **Validates: Requirements 6.2, 6.3, 6.4**
  - [ ]* 8.4 Test: manifest skip avoids re-extraction
    - Index once (fake VLM), re-run → file skipped, fake VLM not called second time
    - **Property 10** — **Validates: Requirements 11.1, 11.2**

- [ ] 9. Surface content_type in results and citations
  - [ ] 9.1 Include `content_type` in Ask citations in `backend/app/llm/rag.py`
    - Add to each citation dict in `_format_context` (search results already carry full metadata)
    - _Requirements: 4.6_
  - [ ]* 9.2 Test: results and citations include content_type + location
    - **Property 11** — **Validates: Requirements 4.6**

- [ ] 10. Checkpoint — backend vision pipeline green
  - Ensure all backend tests pass; ask the user if questions arise.

- [ ] 11. Frontend: ingest lock + content_type
  - [ ] 11.1 Handle the 409 lock and content_type types in `frontend/app/api.ts`
    - `search`/`ask` detect `409` with `detail.code === "indexing_in_progress"` → typed error/sentinel; add `content_type` to `Citation` and search-result metadata types
    - _Requirements: 9.5, 10.4, 4.6_
  - [ ] 11.2 Disable querying during indexing + show paused notice in `frontend/app/page.tsx`
    - While polled index status is `running`, disable Ask/Search and show "Querying paused — indexing in progress"; re-enable when status leaves `running`; on a refused query show the paused state; optionally render `content_type` tags on results/citations
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 4.6_
  - [ ]* 11.3 Frontend component tests (Vitest)
    - Controls disabled while running; re-enabled after; 409 `indexing_in_progress` shows paused state; content_type tags render
    - _Requirements: 10.1, 10.2, 10.3, 10.4_

- [ ] 12. Final verification
  - [ ]* 12.1 Run the full backend suite from `backend/.venv` (`pytest`, property tests ≥100 examples)
  - [ ]* 12.2 Run frontend tests (`npm test`) and build (`npm run build`)
  - [ ]* 12.3 Manual VLM smoke: enable vision, `ollama pull qwen2.5vl:3b`, index a small PDF with a chart/figure, confirm a `chart`/`figure` chunk is searchable and that querying is blocked during the job

## Notes

- Tasks marked `*` are optional (tests/verification); core implementation sub-tasks are never optional.
- VLM/Ollama are faked in all automated tests; a real model run is the manual step 12.3.
- New dependencies: `pdfplumber`, `Pillow`, `pymupdf`. The VLM is pulled via Ollama, not pip.
- Checkpoints (5, 10) provide incremental validation before the vision pipeline and the frontend.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2", "3.1", "4.1", "7.1"] },
    { "id": 1, "tasks": ["1.3", "2.1", "4.2", "6.1"] },
    { "id": 2, "tasks": ["2.2", "3.2", "4.3", "4.4", "6.2"] },
    { "id": 3, "tasks": ["2.3", "2.4", "3.3", "7.2"] },
    { "id": 4, "tasks": ["7.3", "7.4", "8.1"] },
    { "id": 5, "tasks": ["8.2", "8.3", "8.4", "9.1"] },
    { "id": 6, "tasks": ["9.2", "11.1"] },
    { "id": 7, "tasks": ["11.2"] },
    { "id": 8, "tasks": ["11.3"] }
  ]
}
```

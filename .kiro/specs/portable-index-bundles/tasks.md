# Implementation Plan: Portable Index Bundles

## Overview

Add portable export/import of indexed chunks (text + embeddings + metadata) as gzip JSONL bundles, with content-based chunk ids so identical chunks deduplicate across machines and merge is an idempotent union. Sequenced so the id-scheme change and its invariants land and are tested first, then the bundle format, then export, then import (validation → gate → merge → consistency), then the CLIs, then verification.

Backend is Python/FastAPI with a venv at `backend/.venv`; tests use pytest + Hypothesis (installed). Ollama/Chroma are faked in tests. Tasks marked `*` are optional (tests/verification); core sub-tasks are never optional.

## Tasks

- [ ] 1. Content-based Chunk_Id
  - [ ] 1.1 Rewrite `_chunk_id` and the id-assignment loop in `backend/app/ingestion/chunker.py`
    - New `_chunk_id(source_file, location, content_type, text, occurrence)` hashing content only (file name, not relative path; no global index). In `chunk_sections`, compute `occurrence` from a dict keyed by `(location, content_type, text)`; keep `chunk_index`/`total_chunks` metadata for display but stop feeding them into the id. Use `Path(source_path).name` for the id's file-name component.
    - _Requirements: 9.1, 9.2, 9.3, 9.5, 10.1, 10.3_
  - [ ]* 1.2 Property test: id is path-independent
    - **Property 1** — same fields under different directory paths → same id. **Validates: 9.1, 10.1**
  - [ ]* 1.3 Property test: text-chunk ids are vision-invariant
    - **Property 2** — chunk the same sections with and without appended vision sections; text-chunk id set identical. **Validates: 9.2, 9.3**
  - [ ]* 1.4 Property test: distinct chunks keep distinct ids; duplicates disambiguated
    - **Property 3** — differing text/location/content_type → different ids; exact dup within a source → distinct via occurrence. **Validates: 9.5, 10.3**

- [ ] 2. Bundle format module
  - [ ] 2.1 Create `backend/app/bundles/__init__.py` and `backend/app/bundles/format.py`
    - `BUNDLE_FORMAT_VERSION`, `BundleHeader` dataclass, a streaming writer (temp file + running sha256 + count, then prepend finalized header, gzip by default, atomic replace), and a streaming reader/generator that yields the validated header then validated records, size-capping each line before JSON parse. Config: `bundle_max_record_bytes`, `bundle_default_gzip`.
    - _Requirements: 1.3, 1.4, 1.5, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 8.1, 8.2, 8.7_
  - [ ] 2.2 Add `bundle_max_record_bytes` and `bundle_default_gzip` to `backend/app/config.py`
    - _Requirements: 1.4, 8.7_
  - [ ]* 2.3 Test: gzip + plain round-trip through writer/reader
    - Write records, read them back; header fields (count, dim, checksum) accurate. **Property 12** — **Validates: 3.1-3.4**
  - [ ]* 2.4 Test: malformed header/records contained
    - Bad first line → reader refuses; oversized/invalid record line → reader flags invalid without loading it whole. **Property 10 (format layer)** — **Validates: 8.1, 8.2, 8.7**

- [ ] 3. Vector store export/import helpers
  - [ ] 3.1 Add helpers to `backend/app/indexing/vector_store.py`
    - `get_all_ids() -> set[str]`; `iter_export(source_paths) -> Iterator[(id, text, metadata, embedding)]` (Chroma `get(include=["embeddings","documents","metadatas"])`, filtered by source_path); `add_precomputed(ids, texts, metadatas, embeddings)` (batched upsert, no Ollama). Reuse `delete_by_source` for replace.
    - _Requirements: 1.2, 4.1, 7.1_
  - [ ]* 3.2 Test: iter_export filters by source_path; add_precomputed round-trips
    - Fake/real temp Chroma with mocked embedder; assert filtering and that added chunks come back with their vectors.

- [ ] 4. Export
  - [ ] 4.1 Create `backend/app/bundles/export.py`
    - `export_bundle(out_path, sources=None, group_id=None, gzip_output=True)`: resolve selection (group→members via group store, else sources filter, else all); stream chunks from `iter_export`; derive `embed_dim` from first vector, `embed_model` from settings, `vision_included` from content types; zero matches → report, no silent empty bundle; return report dict.
    - _Requirements: 1.1, 1.2, 1.6, 2.1, 2.2, 2.3, 2.4, 2.5, 3.2, 3.3_
  - [ ]* 4.2 Property test: selective export includes exactly the selection
    - **Property 9** — **Validates: 2.1, 2.2, 2.3, 2.5**
  - [ ]* 4.3 Property test: header self-description accurate
    - **Property 12** — chunk_count == records, embed_dim == vector len, vision_included correct. **Validates: 3.1-3.4**

- [ ] 5. Import
  - [ ] 5.1 Create `backend/app/bundles/import_.py`
    - `import_bundle(in_path, replace_sources=False)`: lock check; read+validate header; embedding-model+dim gate (refuse, report both, no writes); optional replace-source (delete existing for bundle's sources, scoped); stream records (validate fields + embedding length, skip+count invalid); merge-by-id against `get_all_ids()` + ids added this run; batched `add_precomputed`; checksum compare→warn; rebuild BM25 if added; update manifest for imported sources; return report.
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 5.1, 5.2, 5.3, 6.1, 6.2, 6.3, 7.1, 7.2, 7.3, 8.3, 8.4, 8.5, 8.6, 11.1, 12.1, 12.2, 12.3, 12.4_
  - [ ] 5.2 Add an import-aware manifest entry path in `backend/app/indexing/manifest.py`
    - A way to mark a source present when it originates from an import (no local raw file): record with a sentinel signature so a later local reindex still behaves. Keep the existing `record(path, rel, chunk_count)` for the normal path.
    - _Requirements: 12.3_
  - [ ]* 5.3 Property test: round-trip preserves chunks
    - **Property 4** — export→import into empty store reproduces id/text/embedding/metadata. **Validates: 1.1, 1.2, 4.1, 12.1**
  - [ ]* 5.4 Property test: import idempotent + union merge
    - **Properties 5, 6** — second import adds zero; result id-set == L ∪ B. **Validates: 4.2, 4.3, 5.3**
  - [ ]* 5.5 Property test: text-only + vision enrichment yields union
    - **Property 7** — **Validates: 5.1, 5.2, 5.3**
  - [ ]* 5.6 Property test: embedding mismatch refused, no writes
    - **Property 8** — **Validates: 6.1, 6.2, 6.3**
  - [ ]* 5.7 Property test: malformed input contained
    - **Property 10** — bad header refuses wholesale; bad/wrong-dim record skipped+counted; valid neighbors import. **Validates: 8.1-8.5**
  - [ ]* 5.8 Test: replace-source is scoped
    - **Property 11** — only bundle's sources removed; others untouched. **Validates: 7.1, 7.3**
  - [ ]* 5.9 Test: consistency after import
    - **Property 14** — imported chunk retrievable by keyword search after rebuild; manifest lists source. **Validates: 12.2, 12.3, 12.4**

- [ ] 6. Checkpoint — bundle engine green
  - Ensure all export/import/format/id tests pass; ask the user if questions arise.

- [ ] 7. CLI entry points + lock
  - [ ] 7.1 Create `backend/export_index.py` and `backend/import_index.py`
    - argparse CLIs calling export_bundle/import_bundle; human-readable reports; non-zero exit on refusal; consult `job_manager.is_indexing()` and refuse while indexing.
    - _Requirements: 1.6, 4.4, 6.3, 11.1, 11.2, 11.3_
  - [ ]* 7.2 Test: lock blocks export/import while indexing
    - **Property 13** — monkeypatch is_indexing → both refuse. **Validates: 11.1, 11.2, 11.3**

- [ ] 8. Docs
  - [ ] 8.1 Document the feature in `README.md`
    - Export/import usage, the gzip JSONL format, the embedding-model compatibility rule, merge-by-id semantics, the text-only→vision enrichment workflow, and the one-time reindex needed to adopt content-based ids.
    - _Requirements: 10.4_

- [ ] 9. Final verification
  - [ ]* 9.1 Run the full backend suite from `backend/.venv` (pytest, property tests ≥100 examples)
  - [ ]* 9.2 Manual cross-machine smoke: export a small archive, import into a fresh data dir (fake or second install), confirm search returns imported chunks and a re-import adds nothing

## Notes

- `*` tasks are optional (tests/verification); core sub-tasks are never optional.
- The content-based id change (task 1) is a breaking id-scheme change: existing archives need a force reindex to dedup against new bundles. Documented in task 8.1.
- Ollama/Chroma are faked in automated tests; a real transfer is the manual step 9.2.
- Checkpoint (task 6) validates the engine before the CLI/docs layer.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "2.2"] },
    { "id": 1, "tasks": ["1.2", "1.3", "1.4", "2.1", "3.1"] },
    { "id": 2, "tasks": ["2.3", "2.4", "3.2", "4.1", "5.2"] },
    { "id": 3, "tasks": ["4.2", "4.3", "5.1"] },
    { "id": 4, "tasks": ["5.3", "5.4", "5.5", "5.6", "5.7", "5.8", "5.9"] },
    { "id": 5, "tasks": ["7.1"] },
    { "id": 6, "tasks": ["7.2", "8.1"] }
  ]
}
```

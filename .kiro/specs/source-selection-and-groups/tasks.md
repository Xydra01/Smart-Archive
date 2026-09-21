# Implementation Plan: Source Selection and Groups

## Overview

This plan threads scope through the existing single retrieval path (`hybrid_search` → RRF over Chroma + BM25) and adds a `Group_Store` that mirrors the existing `Manifest`. Work is sequenced bottom-up: configuration and the pure `QueryScope` value object first, then persistence (`GroupStore`), then the two retriever filters, then the scoped `hybrid_search` and `rag` passthrough, then API resolution and endpoints, then indexer pruning hooks, and finally the frontend. Each backend step ships with the property/unit tests that validate it so scope enforcement and set semantics are proven close to the code under design.

Backend is Python/FastAPI with a virtualenv at `backend/.venv`. Property tests use Hypothesis (added to `requirements.txt` in task 1) with a minimum of 100 iterations, tagged `Feature: source-selection-and-groups, Property N: ...`. Retrieval property tests use fake/stubbed stores; `GroupStore` tests use `tmp_path`; integration tests use a real temporary Chroma collection with a mocked embedder.

Tasks marked with `*` are optional (tests and verification-only) and are not implemented by default.

## Tasks

- [x] 1. Add configuration and test tooling
  - [x] 1.1 Add scope/group settings to `backend/app/config.py`
    - Add `groups_file: Path = ARCHIVE_ROOT / "data" / "groups.json"` (beside `index_manifest.json`, separate from the Manifest)
    - Add `max_selection: int = 10_000`
    - _Requirements: 13.1, 1.8_
  - [x]* 1.2 Add Hypothesis and pytest to `backend/requirements.txt`
    - Add `pytest` and `hypothesis` (unpinned or version-pinned consistent with existing style); install into `backend/.venv`
    - Create `backend/tests/` package with an `__init__.py` so tests are importable
  - [x]* 1.3 Write smoke test for the groups persistence path
    - Assert `settings.groups_file` resolves under `data/` and is distinct from the Manifest path
    - _Requirements: 13.1_

- [x] 2. Implement the QueryScope value object
  - [x] 2.1 Create `backend/app/search/scope.py` with the `QueryScope` dataclass
    - `@dataclass(frozen=True)` holding `selection: frozenset[str] | None`
    - `whole_archive()` → `selection=None`; `of(sources)` → `frozenset(sources)` (dedup by construction); `is_unscoped` property returns `selection is None`
    - Document the `None` (whole archive) vs empty-`frozenset` (zero results) distinction
    - _Requirements: 1.3, 1.4, 1.7, 11.1, 11.2_
  - [x]* 2.2 Write property test for QueryScope set/dedup semantics
    - **Property 7: Selection is treated as a set (duplicates do not matter)** — `of(list)` equals `of(list-with-dups-removed)`
    - **Property 3 (scope side): empty scope is distinct from unscoped** — `whole_archive().is_unscoped` is true; `of([])` is not unscoped and carries an empty selection
    - **Validates: Requirements 1.7, 11.1, 11.2**

- [x] 3. Implement the Group data model and GroupStore
  - [x] 3.1 Create `backend/app/groups/__init__.py` and the `Group` dataclass in `backend/app/groups/store.py`
    - Fields: `group_id: str`, `name: str`, `members: set[str]`, `created_at: float`, `updated_at: float`
    - _Requirements: 4.1, 5.1_
  - [x] 3.2 Implement `GroupStore` mirroring `Manifest` (lazy singleton `get_group_store()`, `threading.Lock`, atomic `_save`, corrupt-tolerant `_load`)
    - `_load()` reads `settings.groups_file`; missing/corrupt → empty groups, never raises
    - `_save()` writes to a `.tmp` file then `replace()`; on-disk shape `{"version": 1, "groups": {group_id: {group_id, name, members (sorted list), created_at, updated_at}}}`
    - Queries: `list()`, `get(group_id)`
    - Mutations (each persists under the lock): `create(name, members=())` (uuid4 hex id, empty set when no members), `rename(group_id, name)` (keeps id + members), `delete(group_id)`, `add_sources(group_id, sources)` (set union, idempotent), `remove_sources(group_id, sources)` (set difference, idempotent)
    - Lifecycle hooks: `prune_source(source_path)` (drop from every group, persist once, keep id/name), `clear_all_members()` (empty every group, keep id/name)
    - _Requirements: 4.1, 4.2, 4.3, 5.1, 5.2, 5.3, 6.1, 6.2, 7.1, 7.2, 7.3, 8.1, 8.2, 8.3, 13.2, 13.3, 13.4, 14.1, 14.2, 14.3, 14.4_
  - [x]* 3.3 Write property test for add-sources (union + idempotence)
    - **Property 9: Adding sources to a group is set union (and idempotent)**
    - **Validates: Requirements 7.1, 7.2**
  - [x]* 3.4 Write property test for remove-sources (difference + idempotence)
    - **Property 10: Removing sources from a group is set difference (and idempotent)**
    - **Validates: Requirements 8.1, 8.2**
  - [x]* 3.5 Write property test for persistence round-trip
    - **Property 12: Group persistence round-trips** — apply a generated sequence of create/rename/delete/add/remove/prune/clear, then a fresh `GroupStore` from the same `tmp_path` reproduces id/name/members exactly
    - **Validates: Requirements 4.2, 5.2, 6.1, 7.3, 8.3, 13.2, 14.3**
  - [x]* 3.6 Write property test for corrupt/missing load
    - **Property 13: Corrupt or missing persistence loads as empty** — for arbitrary/truncated/non-JSON bytes (and a missing file), construction yields empty groups and does not raise
    - **Validates: Requirements 13.4**
  - [x]* 3.7 Write property test for source-removal pruning
    - **Property 14: Source-removal pruning drops the path everywhere and preserves group identity**
    - **Validates: Requirements 14.1, 14.4**
  - [x]* 3.8 Write property test for index-reset member clearing
    - **Property 15: Index reset clears all members but keeps groups**
    - **Validates: Requirements 14.2**
  - [x]* 3.9 Write model-based property test for GroupStore
    - **Property 17: Group store matches a reference set model** — sequence of ops applied to store and to a dict `group_id → (name, member set)`; `list()`/`get()` agree on id/name/members
    - **Validates: Requirements 4.1, 5.1, 5.3, 6.2, 9.1, 9.2**
  - [x]* 3.10 Write unit test for atomic write mechanism
    - Assert a temp file is written then replaced and the target remains valid JSON after a save
    - _Requirements: 13.3_

- [x] 4. Checkpoint — Ensure all pure-logic tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Add source-scope filtering to both retrievers
  - [x] 5.1 Add `where_sources` filter to `VectorStore.query` in `backend/app/indexing/vector_store.py`
    - New optional `where_sources: set[str] | None = None`; when present pass Chroma `where={"source_path": {"$in": sorted(where_sources)}}`; `None` preserves current behavior exactly
    - _Requirements: 2.1_
  - [x] 5.2 Add `allowed_sources` filter to `KeywordIndex.query` in `backend/app/indexing/keyword_index.py`
    - New optional `allowed_sources: set[str] | None = None`; apply the filter to the ranked index list **before** truncating to `top_k` so in-scope hits are not lost; skip any chunk whose `source_path` is not in the set
    - _Requirements: 2.2, 2.4_
  - [x]* 5.3 Write integration test for the real Chroma `$in` filter
    - Build a small fixed corpus in a temporary real Chroma collection with a mocked embedder; assert `VectorStore.query(..., where_sources=...)` returns only in-scope `source_path` chunks
    - **Property 2 (vector side): Both retrievers are filtered before fusion**
    - **Validates: Requirements 2.1**
  - [x]* 5.4 Write test for the BM25 pre-truncation filter
    - Construct a keyword index where out-of-scope docs would otherwise dominate `top_k`; assert filtering happens before truncation and only in-scope `source_path` chunks are returned
    - **Property 2 (keyword side): Both retrievers are filtered before fusion**
    - **Validates: Requirements 2.2, 2.4**

- [x] 6. Add scope to hybrid_search
  - [x] 6.1 Add optional `scope: QueryScope | None` to `hybrid_search` in `backend/app/search/hybrid.py`
    - Default to `QueryScope.whole_archive()` so existing callers are unaffected
    - Unscoped path: query both retrievers unfiltered (unchanged behavior)
    - Scoped path: compute `Effective_Sources = scope.selection & frozenset(store.sources().keys())`; if empty, return `[]` (query neither retriever); otherwise pass the allowed set to both `store.query(..., where_sources=...)` and `keyword.query(..., allowed_sources=...)` before the existing RRF block
    - _Requirements: 1.1, 1.5, 1.6, 2.3, 10.2, 10.5, 11.1, 11.2, 12.1_
  - [x]* 6.2 Write property test for soundness (no out-of-scope leak)
    - Use fake/stubbed Vector_Store and Keyword_Index returning chunks tagged with generated `source_path` values
    - **Property 1: Selection filtering is sound (no out-of-scope source leaks)**
    - **Validates: Requirements 1.1, 1.2, 1.6, 2.4, 10.2**
  - [x]* 6.3 Write property test for both retrievers filtered before fusion
    - Assert the candidate lists handed to RRF contain only Effective_Sources chunks
    - **Property 2: Both retrievers are filtered before fusion**
    - **Validates: Requirements 1.5, 2.1, 2.2, 2.3**
  - [x]* 6.4 Write property test for empty-scope zero results
    - **Property 3: Empty scope yields zero results without whole-archive fallback** — empty Effective_Sources returns `[]` and touches neither fake retriever
    - **Validates: Requirements 10.5, 11.1, 11.2**
  - [x]* 6.5 Write property test for stale members being inert
    - **Property 4: Stale (non-present) selection members are inert** — augmenting a selection with absent `source_path` values yields identical results
    - **Validates: Requirements 12.1**
  - [x]* 6.6 Write property test for duplicate-insensitive selection
    - **Property 7: Selection is treated as a set (duplicates do not matter)** — end-to-end through `hybrid_search`
    - **Validates: Requirements 1.7**

- [x] 7. Thread scope through the RAG service
  - [x] 7.1 Add `scope` passthrough to `answer` and `answer_stream` in `backend/app/llm/rag.py`
    - Forward `scope` into `hybrid_search`; reuse the existing empty-hits branch to emit `citations: []` plus a scope-aware message ("No sources are in scope for this query." when scoped-empty, otherwise the existing whole-archive message)
    - _Requirements: 1.9, 11.3_
  - [x]* 7.2 Write property test for scoped Ask citations
    - Fake stores + mocked LLM; assert every citation's `source_path` is in Effective_Sources, and empty Effective_Sources yields empty citations + the scope message
    - **Property 18: Ask citations stay within scope; empty scope yields empty citations**
    - **Validates: Requirements 1.9, 11.3**

- [x] 8. Checkpoint — Ensure retrieval and RAG scoping tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 9. Implement scope resolution and wire query endpoints
  - [x] 9.1 Add `resolve_scope(sources, group_id) -> QueryScope` helper in `backend/app/main.py`
    - Ad-hoc non-empty `sources` wins (validate size ≤ `settings.max_selection`, else `HTTPException(400)`); else `group_id` resolves via `get_group_store().get(...)` (`HTTPException(404)` identifying the missing id if unknown) to `QueryScope.of(group.members)`; else (`sources` omitted/empty and no `group_id`) → `QueryScope.whole_archive()`
    - _Requirements: 1.4, 1.8, 10.1, 10.3, 10.4, 10.5, 11.1_
  - [x] 9.2 Add optional `sources` and `group_id` to `SearchRequest`/`AskRequest` and wire both handlers
    - Both fields `list[str] | None` / `str | None`, default `None` (backward compatible); each handler calls `resolve_scope(...)` then passes `scope` into `hybrid_search` / `rag.answer_stream`
    - _Requirements: 1.1, 1.2, 10.1, 10.4_
  - [x]* 9.3 Write property test for group-vs-ad-hoc equivalence
    - **Property 5: Group scoping equals ad-hoc selection of the same members** — scoping by `group_id` equals ad-hoc selection of that group's members
    - **Validates: Requirements 10.1**
  - [x]* 9.4 Write property test for ad-hoc precedence over group_id
    - **Property 6: Ad-hoc selection takes precedence over group_id** — resolved scope equals the ad-hoc selection, independent of the group
    - **Validates: Requirements 10.4**
  - [x]* 9.5 Write unit tests for query-path error/boundary paths
    - 10,000 accepted / 10,001 rejected with 400 (Req 1.8); unknown `group_id` on Search/Ask → 404 (Req 10.3); empty ad-hoc `sources` and omitted selection both map to whole archive (Req 1.4)
    - _Requirements: 1.4, 1.8, 10.3_

- [x] 10. Implement the selectable-sources and group endpoints
  - [x] 10.1 Add `GET /api/selectable-sources` in `backend/app/main.py`
    - Return `{"sources": [{"source_path": str, "chunks": int}, ...]}` from `store.sources()`
    - _Requirements: 3.1, 3.2_
  - [x] 10.2 Add the `/api/groups` CRUD surface with a `GroupView` builder
    - `GET /api/groups` (list), `POST /api/groups {name}` (create), `GET /api/groups/{id}` (with `present` flags), `PATCH /api/groups/{id} {name}` (rename), `DELETE /api/groups/{id}`, `POST /api/groups/{id}/sources {sources}` (add), `DELETE /api/groups/{id}/sources {sources}` (remove)
    - Name validation: empty/whitespace-only name on create/rename → `HTTPException(400)`; unknown `{id}` on any route → `HTTPException(404)`
    - `GroupView` includes `members` plus `present` computed from `store.sources()` at request time (stale members retained in `members`)
    - _Requirements: 4.1, 4.4, 5.1, 5.4, 5.5, 6.1, 6.3, 7.1, 7.4, 8.1, 8.4, 9.1, 9.2, 9.3, 12.2, 12.3_
  - [x]* 10.3 Write property test for selectable sources
    - **Property 8: Selectable sources equal the Vector_Store contents with counts**
    - **Validates: Requirements 3.1, 3.2**
  - [x]* 10.4 Write property test for presence flags
    - **Property 16: Presence flags reflect the Vector_Store** — flag true exactly when `source_path` is present; all members reported
    - **Validates: Requirements 12.2, 12.3**
  - [x]* 10.5 Write property test for group-name validation
    - **Property 11: Whitespace-only group names are rejected** — empty/whitespace names on create/rename rejected with 400; no group created/renamed
    - **Validates: Requirements 4.4, 5.5**
  - [x]* 10.6 Write unit tests for group-route 404s
    - Unknown `{id}` returns 404 on GET one / PATCH / DELETE / add-sources / remove-sources
    - _Requirements: 5.4, 6.3, 7.4, 8.4, 9.3_

- [x] 11. Wire group-membership pruning into the indexer
  - [x] 11.1 Hook pruning into `remove_source` and `reset_index` in `backend/app/indexing/indexer.py`
    - `remove_source(source_path)`: call `get_group_store().prune_source(source_path)` after manifest removal
    - `reset_index()`: call `get_group_store().clear_all_members()` before rebuilding the keyword index
    - _Requirements: 14.1, 14.2, 14.3, 14.4_
  - [x]* 11.2 Write integration test for indexer pruning hooks
    - After `remove_source`, the path is gone from every group; after `reset_index`, all members are empty; group ids/names retained in both
    - **Property 14 / Property 15** exercised through the indexer entry points
    - **Validates: Requirements 14.1, 14.2, 14.4**

- [x] 12. Extend the frontend API client
  - [x] 12.1 Add types and client functions to `frontend/app/api.ts`
    - Types: `Group { group_id, name, members, present? }`, `SelectableSource { source_path, chunks }`
    - Functions: `getSelectableSources`, `listGroups`, `createGroup`, `renameGroup`, `deleteGroup`, `addSources`, `removeSources`
    - Add optional scope arg `{ sources?: string[]; group_id?: string }` to `search()` and `ask()`, merged into the POST body; omit both keys when nothing is picked (whole archive)
    - _Requirements: 15.2, 15.3, 15.4_

- [x] 13. Add frontend scope and group-management controls
  - [x] 13.1 Add the scope control and groups manager to `frontend/app/page.tsx`
    - Scope control near the search bar: source multi-select populated from `getSelectableSources()`, group picker populated from `listGroups()`; send `sources` when sources picked, `group_id` when a group is chosen with no ad-hoc selection, nothing when unscoped; ad-hoc selection visibly takes precedence
    - Groups manager: create/rename/delete a group, add/remove sources, invoking the matching client functions
    - _Requirements: 15.1, 15.2, 15.3, 15.4, 15.5_
  - [x]* 13.2 Write frontend component tests for scope controls
    - Renders selectable sources (Req 15.1); selecting sources sends `sources[]` (Req 15.2); choosing a group sends `group_id` (Req 15.3); selecting nothing omits both (Req 15.4); create/rename/delete/add/remove invoke the right client calls (Req 15.5)
    - _Requirements: 15.1, 15.2, 15.3, 15.4, 15.5_

- [x] 14. Final verification
  - [x]* 14.1 Run the full backend test suite from `backend/.venv`
    - Run `pytest` (property tests at ≥100 Hypothesis iterations, tagged `Feature: source-selection-and-groups, Property N: ...`); fix any failures
  - [x]* 14.2 Run the Chroma `$in` + BM25 filter integration path
    - Exercise `hybrid_search` scoped over a small fixed corpus in a temporary real Chroma collection with a mocked embedder; confirm both retrievers enforce scope together
  - [x]* 14.3 Build the frontend
    - Run the Next.js build to confirm the new client functions and controls compile with no type errors
  - [ ]* 14.4 Manual smoke path
    - Create a group, add a source, run a scoped Search and Ask, remove the source, and confirm the group is pruned but retained

## Notes

- Tasks marked with `*` are optional (tests and verification-only) and can be skipped for a faster MVP; core implementation sub-tasks are never optional.
- Each task references specific requirement clauses and, where applicable, the design correctness property it validates.
- Checkpoints (tasks 4 and 8) provide incremental validation before moving to the API and UI layers.
- Retrieval property tests use fake/stubbed stores; `GroupStore` tests use `tmp_path`; the real Chroma `where` + BM25 filter path is covered by integration tests with a mocked embedder.
- Long-running commands (Next.js dev server, watch-mode test runners) should be run manually by the user; verification tasks use single-run commands.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2", "2.1", "3.1"] },
    { "id": 1, "tasks": ["1.3", "2.2", "3.2", "5.1", "5.2"] },
    { "id": 2, "tasks": ["3.3", "3.4", "3.5", "3.6", "3.7", "3.8", "3.9", "3.10", "5.3", "5.4", "6.1"] },
    { "id": 3, "tasks": ["6.2", "6.3", "6.4", "6.5", "6.6", "7.1", "11.1"] },
    { "id": 4, "tasks": ["7.2", "9.1", "11.2"] },
    { "id": 5, "tasks": ["9.2", "9.3", "9.4", "9.5", "10.1", "10.2"] },
    { "id": 6, "tasks": ["10.3", "10.4", "10.5", "10.6", "12.1"] },
    { "id": 7, "tasks": ["13.1"] },
    { "id": 8, "tasks": ["13.2", "14.1", "14.2", "14.3", "14.4"] }
  ]
}
```

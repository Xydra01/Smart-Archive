# Implementation Plan: Citation Formatting

## Overview

This plan builds the citation feature bottom-up, mirroring the existing
`source-selection-and-groups` code exactly so it stays familiar. The central
discipline is a clean split between *stored data* (persisted verbatim),
*effective data* (stored merged with computed defaults), and *formatting* (a
pure function of effective data + style). That split is what makes almost the
whole feature property-testable with no models and no network — the easy wins
on the CPU-only laptop.

Sequencing follows the dependency arrows in the design:

1. **Pure, offline, property-testable backend units first** — config, the
   `Metadata_Store`, the effective-record layer, the `display_name` resolver,
   the `Citation_Formatter` (MLA + APA, incl. the verbatim-override
   short-circuit), the import parsers (BibTeX/RIS/CSL-JSON/Verbatim), and the
   ordering comparator. Each ships with its Hypothesis property tests.
2. **Metadata_API endpoints** (list/get/update/format/import) plus the indexer
   prune/reset wiring, with FastAPI `TestClient` tests.
3. **display_name integration** into `rag._format_context` and
   `/api/selectable-sources`, then its consumption in the frontend labels.
4. **Frontend** — `api.ts` helpers/types, `References_View` (edit form,
   validation, live preview, import UI), then `Ask_View` `Style_Selector` +
   `Bibliography_Block`.
5. **Stretch export** (Requirement 10) — marked optional throughout.

Backend is Python/FastAPI with a virtualenv at `backend/.venv`. New backend
code lives in `backend/app/references/` (mirroring `backend/app/groups/`):
`store.py`, `effective.py`, `formatter.py`, `import_parsers.py`,
`display_name.py`. Property tests use Hypothesis (already a dependency; see
`backend/.hypothesis/`) at **minimum 100 iterations**, each tagged
`Feature: citation-formatting, Property N: ...`, following the
`backend/tests/` style used by the source-selection-and-groups tests.
`SourceMetadataStore` tests bind to a per-test temp file via a
`make_metadata_store(path)` helper (never the real
`data/source_metadata.json`).

Tasks marked with `*` are optional (tests and verification-only) and are not
implemented by default.

> **Tooling caveat for the implementer:** the edit tool has stripped the
> parentheses from multi-type `except (A, B):` handlers in this repo before,
> producing `SyntaxError`. In all new code, prefer `except Exception:` or
> separate single-type `except` clauses — never a parenthesized multi-type
> handler.

## Tasks

- [x] 1. Add configuration and the references module skeleton
  - [x] 1.1 Add the metadata-store setting to `backend/app/config.py`
    - Add `source_metadata_file: Path = ARCHIVE_ROOT / "data" / "source_metadata.json"` beside `groups_file`
    - _Requirements: 2.9_
  - [x] 1.2 Create the `backend/app/references/` package
    - Add `backend/app/references/__init__.py` (mirrors `app/groups/__init__.py`)
    - _Requirements: 2.9_
  - [ ]* 1.3 Add a smoke test for the metadata persistence path
    - Assert `settings.source_metadata_file` resolves under `data/` and is distinct from `groups_file` and the index manifest path
    - Ensure `backend/tests/` is a package importable by pytest
    - _Requirements: 2.9_

- [x] 2. Implement the Metadata_Store
  - [x] 2.1 Implement `SourceMetadata` and `SourceMetadataStore` in `backend/app/references/store.py`
    - `SOURCE_TYPES = ("book", "article", "website", "report", "other")`
    - `@dataclass SourceMetadata` with `source_path`, `source_type` (default `"other"`), `authors: list[str]`, `title/container/publisher/publication_date/url/access_date: str | None`, and `verbatim_overrides: dict[str, str]` (style → text; absent styles omitted)
    - `SourceMetadataStore` mirrors `GroupStore`: lazy singleton `get_source_metadata_store()`, `threading.Lock` on every mutation
    - On-disk shape `{"version": 1, "records": {source_path: {...}}}`; absent fields omitted from JSON (not written as `null`) so read merges them back to absent
    - `_load()` reads `settings.source_metadata_file`; missing/unreadable/non-JSON/non-object/missing-`records`/`records`-not-a-mapping/structurally-invalid-record all resolve to empty and never raise
    - `_save()` writes `source_metadata.json.tmp` then atomic `.replace()`
    - Queries: `get(source_path)`, `all()` (stored records only)
    - Mutations: `upsert(source_path, fields)` — merges supplied fields into any existing record; sets `access_date` to today (server local date) only when *creating* a record with no supplied `access_date`; merges a supplied `verbatim_overrides[style]` into the existing map rather than clobbering the whole map
    - Lifecycle hooks: `prune_source(source_path)`, `clear_all()`
    - _Requirements: 2.1, 2.2, 2.8, 2.9, 2.10, 2.11, 3.5, 8.12_
  - [x]* 2.2 Write property test for store round-trip and absent-stays-absent
    - **Property 7: Store round-trips, and absent fields stay absent**
    - **Validates: Requirements 2.1, 2.2, 2.8**
  - [x]* 2.3 Write property test for corrupt/missing load tolerance
    - **Property 8: Missing or corrupt persistence loads as empty and never raises** — over `st.binary()` plus crafted bad-shape bytes (mirror `corrupt_bytes` in `test_group_store.py`) and a missing file
    - **Validates: Requirements 2.10**
  - [x]* 2.4 Write property test for prune/clear lifecycle hooks
    - **Property 9: Source removal and index reset drop metadata** — `prune_source` removes one record and leaves the rest; `clear_all` removes every record
    - **Validates: Requirements 4.11**
  - [x]* 2.5 Write a unit test for the atomic-write mechanism
    - A temp file is written then replaced; the target parses as valid JSON with no leftover `.json.tmp` after a save (mirror `test_save_produces_valid_json_...`)
    - _Requirements: 2.11_

- [x] 3. Implement the effective-record layer and the display_name resolver
  - [x] 3.1 Implement `effective_record(...)` in `backend/app/references/effective.py`
    - Pure function `(source_path, stored: SourceMetadata | None, source_file: str | None) -> SourceMetadata`
    - `source_type` defaults to `"other"` when absent; `title`, when absent, falls back to `source_file` with its extension stripped, else the final segment of `source_path` with its extension stripped
    - Leaves a `None` `access_date` as `None` (the store owns the create-time default), so the formatter can apply the website/MLA access-date rule and incompleteness marking
    - _Requirements: 3.1, 2.8_
  - [x] 3.2 Implement `display_name(...)` in `backend/app/references/display_name.py`
    - Pure, total `(source_path, stored_title: str | None, source_file: str | None) -> str`
    - Return `stored_title` when it has at least one non-whitespace character; else the file name (`source_file` when present, else the final segment of `source_path`); always non-empty
    - _Requirements: 9.1, 9.2_
  - [x]* 3.3 Write property test for the title fallback
    - **Property 4: Title fallback is correct and non-empty**
    - **Validates: Requirements 3.1**
  - [x]* 3.4 Write property test for display-name resolution
    - **Property 24: Display-name resolution is total and title-preferring**
    - **Validates: Requirements 9.1, 9.2**

- [x] 4. Implement the Citation_Formatter (MLA + APA)
  - [x] 4.1 Implement the formatter core and `required_fields` in `backend/app/references/formatter.py`
    - `CitationStyle(str, Enum)` with `MLA`/`APA`; `@dataclass FormattedCitation` (`source_path`, `style`, `text`, `missing_required`, `incomplete`, `leading_element`)
    - `required_fields(source_type)` encodes Req 2.3–2.7: book → {title, authors}; article → {title, authors, container}; website → {title, url}; report → {title}; other → {title}. A field is present iff it has ≥1 non-whitespace character; `authors` iff ≥1 such entry
    - `format_citation(record, style)`: pure/deterministic, no clock/I/O/randomness; total (never raises); output non-empty and contains the effective title
    - MLA-specific completeness: for a `website` with no `publication_date`, MLA output includes `access_date`, and if `access_date` is also absent it is added to `missing_required` for that (record, style) pair
    - `incomplete == bool(missing_required)`; `leading_element` = author surname else effective title, `None` if neither
    - _Requirements: 1.4, 3.6, 3.7, 2.3, 2.4, 2.5, 2.6, 2.7, 3.4_
  - [x] 4.2 Implement the verbatim-override short-circuit in `format_citation`
    - Consult `record.verbatim_overrides` first: when an entry exists for the requested style, return its text byte-for-byte with `incomplete = False`, `missing_required = []`, and `leading_element` derived from the override's leading token up to the first period/comma (trimmed, case-folded)
    - When no override exists for the requested style, fall through to field-based assembly; the *other* style is always field-based
    - _Requirements: 8.10_
  - [x] 4.3 Implement MLA and APA element assembly and fallbacks
    - Author position: `authors` joined per style; when empty, the effective title takes the author position and no placeholder author text (e.g. no "Anonymous"/"Unknown") is emitted
    - Date position: APA with no `publication_date` renders literal `(n.d.)`; MLA omits an absent date except the website access-date rule
    - MLA 9th ordering/punctuation (`Author. "Title." Container, Publisher, Date, URL. Accessed <date>.`, `Surname, First`, title case); APA 7th ordering/punctuation (`Author (Year). Title. Container. Publisher. URL`, `Surname, F.`, sentence case)
    - _Requirements: 1.2, 1.3, 3.2, 3.3_
  - [x]* 4.4 Write property test for formatter totality and title-bearing output
    - **Property 1: Formatter is total and produces a non-empty, title-bearing citation**
    - **Validates: Requirements 3.7, 1.4**
  - [x]* 4.5 Write property test for formatter determinism
    - **Property 2: Formatter is deterministic** — same input twice yields byte-identical `text`, `missing_required`, `leading_element`
    - **Validates: Requirements 1.2, 1.3, 6.10**
  - [x]* 4.6 Write property test for the incomplete marker
    - **Property 3: Incomplete marker holds exactly when a required field is missing** — parameterized over the five `source_type`s × {MLA, APA}, incl. the MLA website access-date rule
    - **Validates: Requirements 2.3, 2.4, 2.5, 2.6, 2.7, 3.4, 3.6**
  - [x]* 4.7 Write property test for the no-author title position
    - **Property 5: With no authors, the title occupies the author position and no placeholder is emitted**
    - **Validates: Requirements 3.3**
  - [x]* 4.8 Write property test for APA `(n.d.)`
    - **Property 6: APA renders (n.d.) for a missing publication date**
    - **Validates: Requirements 3.2**
  - [x]* 4.9 Write property test for verbatim override precedence and isolation
    - **Property 23: Verbatim override precedence and isolation** — override text returned exactly with complete markers for its style; the other style ignores it; a verbatim import leaves every other field and the resolved `display_name` unchanged
    - **Validates: Requirements 8.9, 8.10**
  - [x]* 4.10 Write example unit tests for exact MLA/APA strings
    - Hand-written records → exact expected MLA and APA strings per `source_type` (Req 1.2, 1.3) and default-MLA behavior (Req 1.4), in `test_formatter_examples.py`
    - _Requirements: 1.2, 1.3, 1.4_

- [x] 5. Implement the import parsers (BibTeX / RIS / CSL-JSON / Verbatim)
  - [x] 5.1 Implement the parser intermediates and dispatch in `backend/app/references/import_parsers.py`
    - `@dataclass ParsedEntry` (`key`, `source_type: str | None`, `authors`, `title`, `container`, `publisher`, `publication_date`, `url`); `@dataclass ParsedVerbatim` (`style`, `text`); `@dataclass ParseError` (`format`, `message`)
    - All parsers are total: return a normalized result or a `ParseError`, never raise; hand-rolled on the stdlib (CSL-JSON via `json`), no new heavy dependencies
    - _Requirements: 8.3_
  - [x] 5.2 Implement `parse_bibtex`, `parse_ris`, `parse_csljson` (structured, multi-entry)
    - Return a *list* of `ParsedEntry` in file order, each with a `key` (BibTeX cite key, RIS 1-based index, CSL `id`/index); a single-entry file yields a one-element list
    - Field mapping per the design tables (BibTeX `author` split on `" and "`, `journal`/`booktitle` → `container`, `url`/`doi` → `url`; RIS `AU`/`A1`, `TI`/`T1`, `JO`/`JF`/`T2`, `PB`, `UR`; CSL `author` `family, given`, `container-title`, `URL`, `issued.date-parts`)
    - Type inference from BibTeX `@type` / RIS `TY` / CSL `type` per the mapping tables (unknown → `other`); no indicator → `source_type = None` (leave existing unchanged)
    - Omitted mappable fields → `None` (persisted as absent)
    - Date normalization: full precision → `YYYY-MM-DD`; lower precision stored as given (`"2015"`, `"2015-06"`) with no fabricated month/day
    - _Requirements: 8.4, 8.5, 8.6, 8.7, 8.11_
  - [x] 5.3 Implement `parse_verbatim(text, style)`
    - Validate + wrap the pasted text as `ParsedVerbatim(style, text)`; do not parse into fields
    - _Requirements: 8.9_
  - [x]* 5.4 Write property test for structured field mapping and type inference
    - **Property 20: Structured parse maps entry fields and infers source_type** — generate entries from a known field set, render to each format, parse back; omitted fields map to absent; type inference matches the tables (unknown → other; no indicator → unchanged)
    - **Validates: Requirements 8.4, 8.6, 8.7**
  - [x]* 5.5 Write property test for parser totality
    - **Property 21: Parsers are total and never raise** — every parser over `st.text()` and decoded `st.binary()` returns a result or `ParseError`, never raising
    - **Validates: Requirements 8.3**
  - [x]* 5.6 Write property test for date-precision preservation
    - **Property 22: Date normalization preserves precision**
    - **Validates: Requirements 8.5**
  - [x]* 5.7 Write property test for replaceable/idempotent one-at-a-time import
    - **Property 25: Import is replaceable and idempotent, one entry at a time** — re-import of the same structured entry is idempotent; a second override for a style replaces the first and leaves the other style untouched; multi-entry files return distinct keys and attach exactly the selected entry
    - **Validates: Requirements 8.11, 8.12**
  - [x]* 5.8 Add checked-in parser fixtures
    - A small `.bib` (`@book{stewart2015, ...}`), a `.ris` (`TY - JOUR ... ER -`), a CSL-JSON array (`type: webpage`), and a verbatim APA string (`Stewart, J. (2015). Calculus (8th ed.). Cengage Learning.`)
    - _Requirements: 8.1_

- [x] 6. Implement the ordering comparator
  - [x] 6.1 Implement the bibliography comparator (shared by Ask block and export)
    - Primary key: leading element case-folded (`str.casefold()`); export variant additionally diacritic-folds (NFKD then drop combining marks)
    - Tie-break: full citation `text` compared the same way; entries with no leading element sort last
    - Total order: reflexive, antisymmetric on distinct texts, transitive, total; sort is a stable permutation
    - _Requirements: 6.8, 6.9, 10.3_
  - [x]* 6.2 Write property test for the ordering total order
    - **Property 17: Bibliography ordering is a total order with the documented key** — incl. no-leading-element-last and export diacritic folding
    - **Validates: Requirements 6.8, 6.9, 10.3**

- [x] 7. Checkpoint — Ensure all pure-logic tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 8. Implement the Metadata_API endpoints and Pydantic models
  - [x] 8.1 Add the Pydantic models and a `_require_indexed` helper in `backend/app/main.py`
    - `SourceMetadataView`, `UpdateMetadataRequest`, `FormatRequest`, `FormattedCitationView`, `FormatResponse`, `ImportRequest` per the design sketches (incl. derived `display_name`, `missing_required`, `is_complete` on the view)
    - `_require_indexed(source_path)` raises `HTTPException(404)` when the path is not in `get_store().sources()` (parallel to the groups membership checks)
    - A `_build_view(source_path, stored)` helper assembling the effective record + `display_name` for responses
    - _Requirements: 4.1, 4.3, 9.3_
  - [x] 8.2 Implement `GET /api/references` and `GET /api/references/{source_path:path}`
    - List returns one `SourceMetadataView` per indexed source from `get_store().sources()`, each merged with defaults; empty index → `{ "references": [] }`
    - Get-one returns the effective view; 404 if not indexed
    - _Requirements: 4.1, 4.2, 4.3, 4.4_
  - [x] 8.3 Implement `PUT /api/references/{source_path:path}` (update)
    - 404 if not indexed; 400 if `source_type` not in `SOURCE_TYPES` (name the allowed types); 400 if any string field > 2000 chars (name the field); on success persist via `store.upsert(...)` and return the effective view
    - _Requirements: 4.5, 4.6, 4.7, 5.5_
  - [x] 8.4 Implement `POST /api/references/format` (batch format)
    - Validate `1 <= len(source_paths) <= 100` (400 otherwise), `style in {MLA, APA}` (400 naming the styles), every `source_path` indexed (404 if any is not); return one `FormattedCitationView` per requested source in request order
    - _Requirements: 4.8, 4.9, 4.10, 1.5_
  - [x] 8.5 Implement `POST /api/references/{source_path:path}/import`
    - 404 if not indexed (store unchanged); 400 if `format` not in `bibtex|ris|csljson|verbatim`, or (verbatim) `style` not `MLA|APA`
    - Dispatch to the matching parser; a `ParseError` → 400 naming the declared format, store unchanged
    - Structured: select the single entry named by `entry` (index or key); multi-entry with a missing/unmatched selector → 400; persist mapped fields via `store.upsert(...)`; a present type indicator sets `source_type`, an absent one leaves it unchanged
    - Verbatim: store `payload` as `verbatim_overrides[style]` only, changing no other field and not `display_name`; re-import replaces
    - Return the effective, updated `SourceMetadataView` (incl. `verbatim_overrides` and resolved `display_name`)
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.7, 8.8, 8.9, 8.11, 8.12_
  - [x]* 8.6 Write property test for list-all coverage
    - **Property 10: List-all covers exactly the indexed sources, each effective** — `TestClient` with `store.sources()` stubbed to a controlled set
    - **Validates: Requirements 4.1, 4.2**
  - [x]* 8.7 Write property test for update-then-read round-trip
    - **Property 11: Update-then-read round-trips on an indexed source**
    - **Validates: Requirements 4.5**
  - [x]* 8.8 Write property test for the unknown-source 404
    - **Property 12: Unknown source is a 404 and leaves the store unchanged** — get-one, update, import, and any-unknown format batch all 404 with the on-disk file byte-for-byte unchanged
    - **Validates: Requirements 4.4, 4.6, 4.10, 8.2**
  - [x]* 8.9 Write property test for the style gate
    - **Property 13: The style gate accepts exactly MLA and APA** — across format/update/export requests; every other value 400s naming MLA and APA, state unchanged
    - **Validates: Requirements 1.1, 1.5, 4.9, 10.2**
  - [x]* 8.10 Write property test for the disallowed source_type
    - **Property 14: A disallowed source_type is a 400 and leaves the store unchanged**
    - **Validates: Requirements 4.7**
  - [x]* 8.11 Write property test for batch-format coverage
    - **Property 15: Batch format returns one citation per requested source** (1..100 indexed sources)
    - **Validates: Requirements 4.8**
  - [x]* 8.12 Write import API example tests
    - Each of the four formats accepted/dispatched (Req 8.1); unparseable payload → 400 naming the format, store unchanged (Req 8.3); structured import persists mapped fields + inferred type (Req 8.4, 8.7); verbatim writes only the override and leaves `display_name` (Req 8.9); re-import replaces (Req 8.12)
    - _Requirements: 8.1, 8.3, 8.4, 8.7, 8.9, 8.12_

- [x] 9. Wire index-lifecycle hooks and display_name into existing backend paths
  - [x] 9.1 Hook prune/reset into `backend/app/indexing/indexer.py`
    - In `remove_source(source_path)`: call `get_source_metadata_store().prune_source(source_path)` beside the existing manifest/group hooks
    - In `reset_index()`: call `get_source_metadata_store().clear_all()` beside `get_group_store().clear_all_members()`
    - _Requirements: 4.11_
  - [x] 9.2 Add `display_name` to citations in `backend/app/llm/rag.py`
    - In `_format_context`, resolve `display_name` per citation from the citation's `source_file` and the stored title looked up by `source_path`, and add it to each citation dict
    - _Requirements: 9.3_
  - [x] 9.3 Add `display_name` to `GET /api/selectable-sources` in `backend/app/main.py`
    - Add a `display_name` field beside `source_path`/`chunks`, resolved via the same helper (stored title else file name)
    - _Requirements: 9.3_
  - [x]* 9.4 Write an integration test for the indexer hooks
    - After `remove_source`, the source's metadata is dropped; after `reset_index`, all metadata is cleared (exercises Property 9 through the indexer entry points)
    - **Validates: Requirements 4.11**
  - [x]* 9.5 Write wiring tests for display_name
    - `/api/selectable-sources` and the `rag._format_context` citation payload each include a resolved `display_name` (title else file name)
    - _Requirements: 9.3_

- [x] 10. Checkpoint — Ensure API and wiring tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 11. Extend the frontend API client
  - [~] 11.1 Add types and client functions to `frontend/app/api.ts`
    - Types: `SourceType`, `CitationStyle`, `StyleSelection`, `SourceMetadata` (incl. `verbatim_overrides`, `display_name`, `missing_required`, `is_complete`), `FormattedCitation`, `ImportFormat`
    - Functions (typed `fetch`, throw on non-OK): `listReferences`, `getReference`, `updateReference`, `formatCitations`, `importReference`
    - Read the `display_name` field now present on selectable-sources and citation payloads, falling back to the file name so existing behavior is preserved
    - _Requirements: 9.3_

- [ ] 12. Implement the References_View
  - [~] 12.1 Build the References_View panel/route under `frontend/app/`
    - New collapsible panel (consistent with the Groups manager) or sibling route; on load calls `listReferences()`
    - Lists every indexed source by `source_path` (Req 5.1); no-sources empty state (Req 5.2); each row shows effective metadata with an explicit empty marker per absent field (Req 5.3) and names each field still required for the current `source_type` from `missing_required` (Req 5.9)
    - Edit form: `source_type` `<select>` limited to the five types (Req 5.4); inputs for `authors` (as a list), `title`, `container`, `publisher`, `publication_date`, `url`, `access_date`, each bounded to 2000 chars (Req 5.5)
    - Rows label sources by `display_name` (Req 9.3)
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.9, 9.3_
  - [~] 12.2 Add client-side validation, save, and error/timeout handling
    - Validate `url` and date fields before sending; invalid fields flagged, save not sent, entered values preserved (Req 5.8) using the same ISO-date predicate as Property 19
    - Save via `updateReference(...)`; on success the row reflects saved values (Req 5.6); a failure or a >10s timeout (`AbortController` deadline) shows an error and keeps entered values (Req 5.7)
    - _Requirements: 5.6, 5.7, 5.8_
  - [~] 12.3 Add the live citation preview
    - While editing, show the formatted citation in the currently selected style, refreshed within ~1s of an edit via a debounced `formatCitations([source_path], style)`; default to MLA when the Style_Selector is Off
    - _Requirements: 5.10, 1.4_
  - [~] 12.4 Add the reference import control
    - A control that posts to `importReference(...)` for BibTeX/RIS/CSL-JSON/Verbatim (with `entry` selector and, for verbatim, `style`) and reflects the returned view (incl. `display_name`/`verbatim_overrides`)
    - _Requirements: 8.1_
  - [ ]* 12.5 Write the ISO date validator and its property test
    - Implement the date predicate (accept iff a valid `YYYY-MM-DD` calendar date; reject malformed shapes and impossible dates like `2023-02-30`), used by 12.2
    - **Property 19: The date validator accepts exactly ISO calendar dates**
    - **Validates: Requirements 5.8**
  - [ ]* 12.6 Write References_View component tests
    - One row per indexed source; empty state; empty markers for absent fields; type dropdown offers exactly five types; invalid url/date flagged and not sent; save success/failure/timeout paths; live preview updates on edit; rows labeled by `display_name`; import control posts to the (mocked) import endpoint and reflects the returned view
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.6, 5.7, 5.8, 5.10, 8.1, 9.3_

- [ ] 13. Implement the Ask_View Style_Selector and Bibliography_Block
  - [~] 13.1 Add the Style_Selector to `frontend/app/page.tsx`
    - A `<select>` with exactly `Off`/`MLA`/`APA` (Req 6.1) defaulting to `Off` (Req 6.2), held in `useState<StyleSelection>("off")`
    - Keep the existing inline `[n]` markers and Sources panel rendering independent of the selector (Req 6.11)
    - _Requirements: 6.1, 6.2, 6.11_
  - [~] 13.2 Add the Bibliography_Block and its client-side formatting
    - Render beneath the Sources panel only when the selector is MLA/APA *and* there is ≥1 cited source (Req 6.3, 6.4, 6.5); title "Works Cited" for MLA, "References" for APA (Req 6.6, 6.7)
    - Compute the distinct `source_path`s among the current `citations`, call `formatCitations(distinctPaths, style)`, sort with the ordering comparator (client-side mirror of Property 17), and render; changing the style re-issues `formatCitations` and re-renders within ~1s **without** calling `/api/ask` again (Req 6.10)
    - _Requirements: 6.3, 6.4, 6.5, 6.6, 6.7, 6.8, 6.9, 6.10_
  - [ ]* 13.3 Write property test for distinct-source mapping
    - **Property 16: The bibliography has one entry per distinct cited source** (pure client-side mapping over generated citation lists)
    - **Validates: Requirements 6.4, 6.5**
  - [ ]* 13.4 Write Ask_View component tests
    - Style_Selector has exactly Off/MLA/APA and defaults to Off; Off hides the block; MLA→"Works Cited", APA→"References"; block hidden with zero cited sources; switching style calls the (mocked) `format` endpoint rather than `/api/ask`; inline `[n]` markers and the Sources list unaffected by the selector
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.10, 6.11_

- [~] 14. Checkpoint — Ensure frontend tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 15. Bibliography export (stretch, Requirement 10)
  - [ ]* 15.1 Implement `GET /api/references/export?style=MLA|APA` in `backend/app/main.py`
    - One entry per indexed source, sorted by the export comparator (diacritic-folded leading element, full-text tie-break); incomplete sources included and flagged rather than omitted; 400 on bad style; empty index → empty block + a no-sources indication
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5_
  - [ ]* 15.2 Write property test for export coverage and incomplete-flagging
    - **Property 18: Export covers every indexed source, marking (not omitting) incomplete ones**
    - **Validates: Requirements 10.1, 10.4, 10.5**
  - [ ]* 15.3 Add a frontend export control
    - A control that fetches the export in the active style and presents the ordered bibliography (reusing the same rendering as the Bibliography_Block)
    - _Requirements: 10.1_

- [ ] 16. Final verification
  - [ ]* 16.1 Run the full backend test suite from `backend/.venv`
    - Run `pytest` (property tests at ≥100 Hypothesis iterations, tagged `Feature: citation-formatting, Property N: ...`); fix any failures
  - [ ]* 16.2 Build the frontend
    - Run the Next.js build to confirm the new client functions, References_View, Style_Selector, and Bibliography_Block compile with no type errors

## Notes

- Tasks marked with `*` are optional (tests and verification-only) and can be skipped for a faster MVP; core implementation sub-tasks are never optional.
- Each task references specific requirement clauses and, where applicable, the design correctness property it validates. Properties 1–25 come from the design's Correctness Properties section.
- Checkpoints (tasks 7, 10, 14) provide incremental validation before moving to the API, wiring, and UI layers.
- The pure backend units (store, effective layer, display_name, formatter, parsers, comparator) need no models and no network, so their property tests run on the CPU-only laptop and in CI.
- `SourceMetadataStore` tests bind to a per-test temp file via `make_metadata_store(path)`; API tests use FastAPI `TestClient` with `store.sources()` stubbed to a controlled set; frontend tests mock the API client.
- **Tooling caveat:** avoid parenthesized multi-type `except (A, B):` handlers in new code (the edit tool has stripped the parentheses here before, causing `SyntaxError`); use `except Exception:` or separate `except` clauses.
- Long-running commands (Next.js dev server, watch-mode test runners) should be run manually by the user; verification tasks use single-run commands.
- Requirement 10 (export) is the stretch goal and is entirely optional (task 15).

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2"] },
    { "id": 1, "tasks": ["1.3", "2.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "2.4", "2.5", "3.1", "3.2", "6.1"] },
    { "id": 3, "tasks": ["3.3", "3.4", "4.1", "5.1", "6.2"] },
    { "id": 4, "tasks": ["4.2", "4.3", "5.2", "5.3", "5.8"] },
    { "id": 5, "tasks": ["4.4", "4.5", "4.6", "4.7", "4.8", "4.9", "4.10", "5.4", "5.5", "5.6", "5.7"] },
    { "id": 6, "tasks": ["8.1"] },
    { "id": 7, "tasks": ["8.2", "8.3", "8.4", "8.5"] },
    { "id": 8, "tasks": ["8.6", "8.7", "8.8", "8.9", "8.10", "8.11", "8.12", "9.1", "9.2", "9.3"] },
    { "id": 9, "tasks": ["9.4", "9.5", "11.1"] },
    { "id": 10, "tasks": ["12.1", "13.1", "15.1"] },
    { "id": 11, "tasks": ["12.2", "12.3", "12.4", "13.2", "15.2", "15.3"] },
    { "id": 12, "tasks": ["12.5", "12.6", "13.3", "13.4"] },
    { "id": 13, "tasks": ["16.1", "16.2"] }
  ]
}
```

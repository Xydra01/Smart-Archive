# Design Document

## Overview

This feature turns the sources that back an answer into properly formatted
academic citations in two styles — MLA (9th edition) and APA (7th edition) —
and gives the user a place to fill in the bibliographic details that uploaded
files rarely carry.

It adds several cooperating pieces, all deliberately shaped after the existing
`source-selection-and-groups` feature so the code stays familiar:

- **Metadata_Store** — a per-source bibliographic record store persisted to
  `data/source_metadata.json`, keyed by `source_path`. It mirrors `GroupStore`
  exactly: a dataclass plus a store class, a module-level lazy singleton, a
  `threading.Lock` on every mutation, a corrupt/missing-tolerant `_load` that
  never raises, and an atomic temp-file-then-`replace` `_save`. It also grows
  the two index-lifecycle hooks the indexer already calls on `GroupStore`
  (`prune_source`, `clear_all`).
- **Citation_Formatter** — a pure, deterministic, offline component that
  assembles one formatted citation string for one source in one style. No LLM,
  no network: it must run identically on the CPU-only laptop. It returns both
  the formatted string and the list of missing required fields (the incomplete
  marker).
- **Metadata_API** — FastAPI endpoints that read and write metadata keyed by
  `source_path`, and format citations for a batch of sources. They follow the
  existing conventions in `main.py`: Pydantic request models, `HTTPException`
  for 400/404, indexed sources sourced from `get_store().sources()`.
- **Reference_Import parser** — a pure backend module that parses an external
  reference in one of four **Import_Formats** (BibTeX, RIS, CSL-JSON, or a
  pre-formatted **Verbatim** string) into a normalized intermediate that maps
  onto `Source_Metadata` fields (or, for Verbatim, into a per-style
  **Verbatim_Override**). The parsers are total: they return a normalized result
  or a typed parse error, never an uncontrolled exception, so the API can turn a
  bad payload into a clean 400 (Req 8).
- **Display_Name resolver** — a single pure helper that resolves the label used
  for a source everywhere in the app: the stored title when present, else the
  source's file name (Req 9). It is surfaced as an added field on existing
  response shapes so the change surface stays small.
- **Frontend** — a **References_View** (a new panel/route to see every indexed
  source and edit its fields with a live citation preview) and, in the existing
  **Ask_View**, a **Style_Selector** dropdown (Off / MLA / APA) plus a
  **Bibliography_Block** rendered beneath the answer.

The design's central discipline is a clean separation between *stored data*
(what the user typed, persisted verbatim), *effective data* (stored data merged
with computed defaults such as the title fallback and `access_date`), and
*formatting* (a pure function of effective data + style). This separation is
what makes the whole thing property-testable without models or a network.

### Key design decisions

- **Formatter is pure and total.** Given any `Source_Metadata` (however sparse)
  and a valid style, it always returns a non-empty string. Missing data is
  handled with documented fallbacks, never an exception. This is the property
  that lets the bibliography and the References preview render for every source.
- **Defaults are computed at read time, stored data is verbatim.** The store
  persists exactly the fields the user set (absent fields stay absent, per Req
  2.8). Merging with defaults (title fallback, `access_date` default) happens in
  a thin "effective record" layer used by the API and formatter. The one
  exception is `access_date` on *first creation* (Req 3.5), which is persisted so
  the consulted date is stable rather than drifting to "today" on every read.
- **The index is the source of truth for which sources exist.** The Metadata_API
  never invents sources. "Indexed sources" always come from
  `get_store().sources()` (a `dict[source_path, chunk_count]`), exactly as
  `/api/selectable-sources` and groups do. A `source_path` not in that dict is a
  404.
- **The bibliography formats client-side from already-fetched citations.** When
  the user flips the Style_Selector, the Ask_View re-formats without
  resubmitting the question (Req 6.10) by calling the batch `format` endpoint
  with the `source_path` values it already has from the streamed `citations`
  event. Justification is in the Frontend section.
- **Import parsing is pure and total; only the API mutates the store.** Each
  parser is a pure function from `(payload_text)` to either a normalized result
  or a typed `ParseError`. It never touches disk or the network and never raises
  to the API layer; the API decides success (persist) or failure (400). This
  keeps the offline/CPU-only footprint small and the parsers property-testable
  on random bytes (Req 8.3).
- **Structured import writes fields; verbatim import writes only an override.** A
  BibTeX/RIS/CSL-JSON import populates the normal `Source_Metadata` fields (and,
  when the entry carries a type, the `source_type`), so the existing formatter
  produces the citation. A Verbatim import stores the pasted string as a
  `Verbatim_Override` for exactly one style and changes nothing else; the
  formatter returns that string verbatim for that style and formats the other
  style from fields (Req 8.9, 8.10).
- **Display_Name is one resolver, surfaced as an added field.** Rather than
  threading title-vs-filename logic through every view, a single
  `display_name(...)` helper is computed at read time and added to the citation
  payload and the selectable-sources / references responses. Every label site
  reads that field; when no title is stored it falls back to the file name, so
  the change is backward-compatible (Req 9).

## Architecture

The feature slots into the existing request flow. The Ask flow already streams a
`("citations", [...])` event first (see `rag.answer_stream`); the frontend keeps
those citations and, when a style is active, asks the backend to format the
distinct sources among them.

```mermaid
graph TD
  subgraph Frontend["Frontend (Next.js App Router)"]
    AskView["Ask_View (page.tsx)"]
    StyleSel["Style_Selector (Off/MLA/APA)"]
    BibBlock["Bibliography_Block (Works Cited / References)"]
    RefView["References_View (new panel/route)"]
    ApiTs["api.ts typed fetch helpers"]
    AskView --> StyleSel
    AskView --> BibBlock
    StyleSel --> ApiTs
    BibBlock --> ApiTs
    RefView --> ApiTs
  end

  subgraph Backend["Backend (FastAPI)"]
    MetaAPI["Metadata_API endpoints (main.py)"]
    ImportParser["Import_Parser (BibTeX/RIS/CSL-JSON/Verbatim)"]
    Formatter["Citation_Formatter (MLA / APA)"]
    DisplayName["display_name resolver (title else file name)"]
    Effective["Effective-record layer (merge-with-defaults)"]
    MetaStore["Metadata_Store (source_metadata.json)"]
    Indexer["indexer.remove_source / reset_index"]
    VStore["VectorStore.sources()"]
    Rag["rag.answer_stream (citations event)"]

    MetaAPI --> Effective
    Effective --> MetaStore
    MetaAPI --> Formatter
    MetaAPI --> ImportParser
    ImportParser -->|normalized fields / override / ParseError| MetaAPI
    Formatter --> Effective
    MetaAPI --> VStore
    MetaAPI --> DisplayName
    Rag --> DisplayName
    Indexer -->|prune_source / clear_all| MetaStore
  end

  ApiTs -->|/api/references, /api/references/format, /api/references/{path}/import| MetaAPI
  AskView -->|/api/ask stream| Rag
  Rag -->|citations: source_path + display_name| AskView
  BibBlock -->|format distinct source_paths| MetaAPI
```

`Import_Parser` feeds normalized data (or a `ParseError`) into the Metadata_API,
which persists to the Metadata_Store. The `display_name` resolver is consulted by
the Metadata_API (list / selectable-sources responses) and by `rag` (citation
payload) so every label site shows the resolved name.

How the pieces connect to what already exists:

- **`VectorStore.sources()`** is the authoritative list of indexed
  `source_path` values (with chunk counts). The Metadata_API reads it to answer
  "list all", to 404 unknown sources, and to power the References_View list —
  the same source of truth `/api/selectable-sources` uses.
- **`rag` citations** carry `source_path` and `source_file` per cited chunk (see
  `rag._format_context`, which builds the citation dicts). The Bibliography_Block
  formats the *distinct* sources among them (keyed by `source_path`); the
  `source_file` travels along as the title fallback input. `_format_context` also
  gains a resolved `display_name` per citation (title else file name) so the
  Sources list and inline labels show the real title once a reference is imported
  (Req 9.3).
- **`display_name` resolution** reuses the stored `title` (from manual edits or a
  structured import) and the `source_file` already present in the index/citation
  metadata. It is added to the `/api/selectable-sources` response (the scope
  picker) and to the references list/get responses so groups and search results
  can label sources consistently, all from one resolver (Req 9).
- **Indexer lifecycle hooks**: `remove_source(source_path)` already calls
  `get_manifest().remove(...)` and `get_group_store().prune_source(...)`; we add
  `get_source_metadata_store().prune_source(source_path)` beside them so citation
  metadata is dropped with the source (Req 4.11). `reset_index()` already calls
  `get_group_store().clear_all_members()`; we add the metadata store's
  `clear_all()` there.

## Components and Interfaces

### Metadata_Store (`backend/app/references/store.py`)

New module `app/references/` (mirrors `app/groups/`). The store is a direct
structural copy of `GroupStore`.

```python
# app/references/store.py  (interface sketch)

SOURCE_TYPES = ("book", "article", "website", "report", "other")


@dataclass
class SourceMetadata:
    source_path: str
    source_type: str  # one of SOURCE_TYPES; default "other"
    authors: list[str]  # ordered; may be empty
    title: str | None  # None = absent (fall back at format time)
    container: str | None
    publisher: str | None
    publication_date: str | None  # ISO YYYY-MM-DD or None
    url: str | None
    access_date: str | None  # ISO YYYY-MM-DD or None
    verbatim_overrides: dict[
        str, str
    ]  # Citation_Style -> pre-formatted text; absent styles omitted


class SourceMetadataStore:
    def __init__(self) -> None: ...
    # -- persistence (never raises) --
    def _load(self) -> None: ...  # missing/corrupt -> empty
    def _save(self) -> None: ...  # temp file then .replace()
    # -- queries --
    def get(self, source_path: str) -> SourceMetadata | None: ...
    def all(self) -> dict[str, SourceMetadata]: ...  # stored records only
    # -- mutations --
    def upsert(self, source_path: str, fields: dict) -> SourceMetadata: ...
    # -- index-lifecycle hooks (called from indexer) --
    def prune_source(self, source_path: str) -> None: ...
    def clear_all(self) -> None: ...


def get_source_metadata_store() -> SourceMetadataStore: ...
```

Notes:

- `upsert` sets `access_date` to today (server local date) only when the record
  is being *created* and no `access_date` is supplied (Req 3.5). On subsequent
  updates it leaves `access_date` alone unless the caller sends one.
- `verbatim_overrides` is a mapping of `Citation_Style` → pre-formatted string,
  persisted verbatim; styles with no override are omitted from the mapping
  entirely (absent-stays-absent). A structured import writes the normal fields
  (`title`, `authors`, …) and touches no override; a verbatim import writes only
  `verbatim_overrides[style]`. Import is replaceable/idempotent (Req 8.12):
  re-importing structured data overwrites the mapped fields, and re-importing a
  verbatim override for a style replaces just that style's entry, leaving the
  other style's override untouched. `upsert` merges the supplied override into
  the existing mapping rather than clobbering the whole mapping.
- `_load` tolerates: missing file, unreadable file, non-JSON bytes, a JSON
  top-level that is not an object, a missing `records` key, `records` not a
  mapping, and individual records that are structurally invalid — all resolve to
  "no metadata", never an exception (Req 2.10).
- `_save` writes `source_metadata.json.tmp` then `.replace()`s the target
  (Req 2.11).

### Effective-record layer (`backend/app/references/effective.py`)

A pure helper that merges a stored record (or its absence) with computed
defaults, given the source's presence in the index. This is the boundary
between "what's stored" and "what the formatter sees".

```python
def effective_record(
    source_path: str,
    stored: SourceMetadata | None,
    source_file: str | None,  # from the index / citation, for title fallback
) -> SourceMetadata:
    """Merge stored fields with computed defaults.

    - source_type defaults to "other" when absent.
    - title, when absent, falls back to source_file (extension stripped),
      else the final segment of source_path (extension stripped) (Req 3.1).
    - access_date default is applied by the store at creation, not here;
      effective_record leaves a None access_date as None so the formatter can
      apply the website/MLA access-date rule and incompleteness marking.
    """
```

Keeping the title fallback in a pure function (not in the store) means the
formatter and the API agree on the "effective" title, and it stays testable
without touching disk.

### Citation_Formatter (`backend/app/references/formatter.py`)

A pluggable formatter with one implementation per style. Pure and deterministic:
no clock reads, no I/O, no randomness. `access_date`'s "today" default is
resolved upstream (store on creation), so the formatter itself is a pure
function of `(SourceMetadata, style)`.

```python
class CitationStyle(str, Enum):
    MLA = "MLA"
    APA = "APA"


@dataclass
class FormattedCitation:
    source_path: str
    style: CitationStyle
    text: str  # never empty (Req 3.7)
    missing_required: list[str]  # empty => complete; non-empty => incomplete
    incomplete: bool  # == bool(missing_required)
    leading_element: str | None  # author surname else title; None if neither


def format_citation(
    record: SourceMetadata, style: CitationStyle
) -> FormattedCitation: ...


def required_fields(source_type: str) -> set[str]:
    """Fields required to be *complete* for this source_type (Req 2.3-2.7)."""
```

**Verbatim override precedence (Req 8.10).** `format_citation` consults
`record.verbatim_overrides` *first*. When an override exists for the requested
style, the formatter short-circuits and returns that stored text exactly:

- `text` = the override string, byte-for-byte.
- `incomplete` = `False` and `missing_required` = `[]` — a user-supplied
  formatted citation is treated as complete for that style.
- `leading_element` is *derived from the override text* so ordering still works:
  take the override's leading token up to (but not including) the first period or
  comma, trimmed and case-folded (the same key the comparator uses). For example,
  `"Stewart, James. Calculus. …"` yields a leading element of `"Stewart"`.

When no override exists for the requested style, the formatter falls through to
the existing field-based assembly. Because overrides are per-style, the *other*
style is always field-based (Req 8.10), and the formatter remains pure and
deterministic (the override text is stored data, not computed).

`required_fields` encodes Req 2.3–2.7:

| source_type | required for completeness                 |
| ----------- | ----------------------------------------- |
| book        | `title`, at least one `authors` entry     |
| article     | `title`, one `authors` entry, `container` |
| website     | `title`, `url`                            |
| report      | `title`                                   |
| other       | `title`                                   |

A field counts as present only if it has at least one non-whitespace character
(for `authors`, at least one entry with a non-whitespace character) — the
"complete citation" definition in the glossary.

**MLA-specific completeness rule (Req 3.4):** for a `website` with no
`publication_date`, MLA output includes the Access_Date; if `access_date` is
also absent, `access_date` is added to `missing_required` for that
(record, style) pair. This means completeness can depend on style, so
`missing_required` is computed inside `format_citation`, not solely by
`required_fields`.

The formatter is organized as element assembly (reached only when no
`verbatim_overrides[style]` entry exists — the override short-circuit above runs
first):

1. Compute the ordered *elements* for the (source_type, style) pair from the
   effective record, applying fallbacks:
   - **Author position**: `authors` joined per style; when empty, the title
     takes the author position and no placeholder author text is emitted
     (Req 3.3).
   - **Date position**: APA with no `publication_date` renders `(n.d.)`
     (Req 3.2); MLA omits an absent date except the website access-date rule.
   - **Title**: the effective title (never absent after fallback, Req 3.1/3.7).
2. Join elements with the style's punctuation and ordering (MLA 9th vs APA 7th).
3. Determine `missing_required` and `leading_element`.

Style differences captured (illustrative, not exhaustive):

- **MLA 9th**: `Author. "Title." Container, Publisher, Date, URL. Accessed
  <access_date>.` Author is `Surname, First`; title in title case; access date
  appended for undated websites.
- **APA 7th**: `Author (Year). Title. Container. Publisher. URL` with `(n.d.)`
  when undated. Author is `Surname, F.`; title in sentence case.

Both implementations share the element-assembly scaffolding and differ only in
element selection, ordering, and punctuation, so the "for all" properties (total,
deterministic, non-empty, incomplete-iff-missing) hold uniformly across styles.

### Reference_Import parser (`backend/app/references/import_parsers.py`)

A new pure module that parses each Import_Format into a common intermediate.
Every parser is a total function: it returns a normalized result or a typed
`ParseError`, and never raises to the API layer (Req 8.3).

```python
# app/references/import_parsers.py  (interface sketch)


@dataclass
class ParsedEntry:
    """One structured entry mapped onto Source_Metadata fields.

    Fields are None when the entry does not supply them (persisted as absent).
    """

    key: str | None  # BibTeX cite key / RIS position / CSL id, for display
    source_type: str | None  # inferred; None => leave existing source_type unchanged
    authors: list[str]  # ordered; may be empty
    title: str | None
    container: str | None
    publisher: str | None
    publication_date: str | None  # normalized YYYY-MM-DD when precise, else as given
    url: str | None


@dataclass
class ParsedVerbatim:
    style: CitationStyle  # MLA | APA
    text: str  # stored verbatim as the override


@dataclass
class ParseError:
    format: str  # the declared format, echoed in the 400 message
    message: str  # human-readable reason


# structured parsers: text -> list of entries (multi-entry) or ParseError
def parse_bibtex(text: str) -> list[ParsedEntry] | ParseError: ...
def parse_ris(text: str) -> list[ParsedEntry] | ParseError: ...
def parse_csljson(text: str) -> list[ParsedEntry] | ParseError: ...


# verbatim: not parsed into fields; just validated + wrapped
def parse_verbatim(text: str, style: CitationStyle) -> ParsedVerbatim | ParseError: ...
```

**Parser choice.** Hand-rolled tolerant parsers implemented on the standard
library — no new heavy dependencies. BibTeX and RIS are simple line/brace
grammars; a small tolerant reader (skip unknown fields, ignore comments, accept
minor whitespace/case variation) is enough to map onto our field set and keeps
the offline/CPU-only footprint minimal. CSL-JSON parses with stdlib `json`.
(A dedicated library such as `bibtexparser` or `rispy` could be added later if
real-world files prove too irregular; noted, but not required — the hand-rolled
tolerant parser is the chosen default.)

**Multi-entry files.** A `.bib`, `.ris`, or CSL-JSON array can hold many entries.
The structured parsers return the *list* of `ParsedEntry` in file order, each
with a `key` (BibTeX cite key, RIS 1-based index, CSL `id`/index). The import
request names which single entry to attach (by index or key); the API selects
exactly one and attaches it (one-at-a-time, Req 8.11). A single-entry file yields
a one-element list.

**Type-inference mapping tables** (entry type indicator → `source_type`; when no
indicator is present the API leaves the existing `source_type` unchanged, Req
8.7):

BibTeX entry type → `source_type`:

| BibTeX `@type`                     | source_type |
| ---------------------------------- | ----------- |
| `article`                          | `article`   |
| `book`, `inbook`, `booklet`        | `book`      |
| `techreport`, `report`             | `report`    |
| `misc`, `online`, `electronic`     | `website`   |
| anything else                      | `other`     |

RIS `TY` → `source_type`:

| RIS `TY`      | source_type |
| ------------- | ----------- |
| `JOUR`        | `article`   |
| `BOOK`        | `book`      |
| `RPRT`        | `report`    |
| `ELEC`, `WEB` | `website`   |
| anything else | `other`     |

CSL-JSON `type` → `source_type`:

| CSL `type`        | source_type |
| ----------------- | ----------- |
| `article-journal` | `article`   |
| `book`            | `book`      |
| `report`          | `report`    |
| `webpage`         | `website`   |
| anything else     | `other`     |

**Field mapping per format** (entry field → `Source_Metadata` field):

- **BibTeX**: `author` → `authors` (split on `" and "`, trimmed); `title` →
  `title`; `journal` or `booktitle` → `container`; `publisher` → `publisher`;
  `url` or (failing that) `doi` → `url`; date from `date`, else `year`/`month`/
  `day` → `publication_date` (see date normalization below).
- **RIS**: `AU`/`A1` (repeatable) → `authors`; `TI`/`T1` → `title`; `JO`/`JF`/
  `T2` → `container`; `PB` → `publisher`; `UR` → `url`; `DA`, else `PY`, else
  `Y1` → `publication_date`.
- **CSL-JSON**: `author` array (`family`, `given`) → `authors` (`"family, given"`
  form); `title` → `title`; `container-title` → `container`; `publisher` →
  `publisher`; `URL` → `url`; `issued.date-parts` → `publication_date`.

**Date normalization (Req 8.5).** When a source supplies enough precision to form
a full calendar date (year + month + day), the parser normalizes it to
`YYYY-MM-DD`. When it supplies less (year only, or year+month), the parser stores
what the entry provides (e.g. `"2015"` or `"2015-06"`) rather than fabricating a
month or day. Because the store persists strings verbatim, a year-only date
round-trips as the year. (The References_View date validator only *requires* a
full `YYYY-MM-DD` for manual edits; imported partial dates are stored as-is and
render best-effort, consistent with the "produce a citation from available
fields" rule.)

**Omitted fields (Req 8.6).** A field the entry does not supply becomes `None`
on the `ParsedEntry` and is persisted as absent by the store (absent-stays-absent,
per the existing store contract), never rejected.

### Display_Name resolver (`backend/app/references/display_name.py`)

A single pure, total helper used app-wide to label a source (Req 9):

```python
def display_name(
    source_path: str, stored_title: str | None, source_file: str | None
) -> str:
    """Resolve the label for a source.

    - If stored_title has at least one non-whitespace character, return it (Req 9.1).
    - Otherwise return the source's file name: source_file when present, else the
      final path segment of source_path (Req 9.2).
    Total and deterministic; always returns a non-empty string for an indexed source.
    """
```

**Integration points (Req 9.3).** The resolver is computed at read time and
surfaced as an added field on the existing response shapes, so the change stays
minimal (one function, no new endpoints, backward-compatible fallback to file
name):

- **Sources list & citations** — `rag._format_context` adds `display_name` to
  each citation dict (using the citation's `source_file` and the stored title
  looked up by `source_path`). The Ask_View Sources panel and inline labels read
  it.
- **Selectable-sources (scope picker)** — `GET /api/selectable-sources` adds a
  `display_name` field beside `source_path`/`chunks`. The frontend scope picker
  labels each entry by it.
- **Groups** — group member rows are labeled via the same selectable-sources
  data, so they inherit `display_name` without a groups-endpoint change.
- **Search results** — the search response labels each hit by `display_name`
  (title else file name) using the same resolver.
- **References_View** — each row already carries the effective `title`; the view
  labels the row by `display_name` for consistency.

This deliberately widens the change surface a little beyond citations (it touches
selectable-sources and the sources/citations rendering), but it is contained to
one resolver function surfaced as one extra field on shapes that already exist.

### Metadata_API (`backend/app/main.py`, models in `app/references/`)

New endpoints added alongside the groups endpoints, following the same
conventions. Indexed sources come from `get_store().sources()`.

- `GET  /api/references` — list all indexed sources, each merged with defaults.
- `GET  /api/references/{source_path}` — one source (404 if not indexed).
- `PUT  /api/references/{source_path}` — update fields (404 if not indexed, 400
  bad `source_type`).
- `POST /api/references/format` — format 1..100 sources in a style.
- `POST /api/references/{source_path:path}/import` — import a reference (BibTeX /
  RIS / CSL-JSON / Verbatim) onto one indexed source (Req 8).
- `GET  /api/references/export` — whole-archive bibliography (stretch, Req 10).

(`source_path` values can contain `/`, so the single-source routes use a path
parameter that accepts slashes, or the `source_path` is passed in the request
body / query for the format and update calls to avoid encoding pitfalls; see
the endpoint detail section.)

### References_View and Ask_View (frontend)

Detailed in the Frontend section. The References_View is a new collapsible panel
(consistent with the existing Groups manager panel) or a sibling route under
`frontend/app/`; the Style_Selector is a `<select>` in the Ask_View, and the
Bibliography_Block renders beneath the existing Sources panel.

## Data Models

### Source_Metadata record

| field              | type              | notes                                                    |
| ------------------ | ----------------- | -------------------------------------------------------- |
| `source_path`      | string            | key; must be an indexed source                           |
| `source_type`      | enum              | `book` \| `article` \| `website` \| `report` \| `other`  |
| `authors`          | list[string]      | ordered; empty list allowed                              |
| `title`            | string \| absent  | absent triggers the title fallback at format time        |
| `container`        | string \| absent  | journal / website / containing book title                |
| `publisher`        | string \| absent  |                                                          |
| `publication_date` | string \| absent  | ISO 8601 `YYYY-MM-DD`                                     |
| `url`              | string \| absent  |                                                          |
| `access_date`      | string \| absent  | ISO 8601 `YYYY-MM-DD`; defaulted to today on creation    |
| `verbatim_overrides` | map \| absent   | `Citation_Style` → pre-formatted string; absent styles omitted |

Each editable string field is bounded to at most 2000 characters (Req 5.5); the
API enforces the bound and rejects over-long fields with 400.

**`verbatim_overrides`** holds pre-formatted citation strings pasted via a
Verbatim import, keyed by style (e.g. `{"APA": "...", "MLA": "..."}`). It is
absent when no override has been imported, and only the styles that actually have
an override appear as keys (absent-stays-absent). A structured import never
writes this field; a verbatim import writes only the entry for its declared
style, replacing any prior entry for that style and leaving the other untouched
(Req 8.9, 8.12).

**Display_Name is derived, not stored.** The label for a source is computed at
read time by the `display_name` resolver (stored `title` if non-empty, else the
file name — Req 9.1/9.2). It is not persisted; it appears only in API responses
and citation payloads as a convenience field.

### On-disk JSON shape (`data/source_metadata.json`)

Mirrors `groups.json`: a `version` plus a `records` object keyed by
`source_path`. Absent fields are omitted from the JSON entirely rather than
written as `null` (Req 2.8) — reading merges them back to "absent".

```json
{
  "version": 1,
  "records": {
    "calculus_eighth_edition.pdf": {
      "source_type": "book",
      "authors": ["Stewart, James"],
      "title": "Calculus",
      "publisher": "Cengage Learning",
      "publication_date": "2015-01-01",
      "access_date": "2024-05-01"
    },
    "notes/intro.md": {
      "source_type": "other"
    },
    "papers/imported_verbatim.pdf": {
      "source_type": "other",
      "verbatim_overrides": {
        "APA": "Stewart, J. (2015). Calculus (8th ed.). Cengage Learning."
      }
    }
  }
}
```

The `verbatim_overrides` object is omitted entirely for records with no override;
styles with no override are omitted from the object. `display_name` is never
written to disk — it is resolved on read.

### Merge-with-defaults (read path)

When the API returns a record for an indexed source, it merges the stored record
(possibly absent) with defaults:

- `source_type` → `"other"` if not stored.
- `title` → stored title; else `source_file` with its extension removed; else
  the final segment of `source_path` with its extension removed (Req 3.1). The
  `source_file` is available from the index metadata / citation payload.
- `access_date` → stored value; for a source that has *no stored record yet*,
  "list all" and "get one" present the default of today's date, and the first
  `PUT`/create persists it (Req 3.5) so it becomes stable.
- `display_name` → resolved via the `display_name` helper (stored `title` if
  non-empty, else the file name) and added to the response so every label site
  reads one field (Req 9).
- `verbatim_overrides` → returned as stored (the map of style → text), or an
  empty map when none.
- All other absent fields → presented as an explicit "empty" to the UI (Req 5.3),
  represented as `null`/empty in the API response.

The distinction: the **store** persists verbatim (absent stays absent); the
**API response** is the *effective* record (defaults merged) so the UI and
formatter never have to recompute fallbacks.

## Metadata API endpoints (detail)

All endpoints live in `main.py` beside the groups endpoints and use the same
conventions: Pydantic request/response models, `HTTPException` for 400/404, and
`get_store().sources()` as the authoritative set of indexed `source_path`s. A
small `_require_indexed(source_path)` helper (parallel to the groups' membership
checks) raises 404 when a path is absent.

Because a `source_path` can contain `/`, the update and single-get routes take
the path via a request body / query rather than relying on URL path encoding for
the batch operations; a slash-tolerant path parameter is used for the RESTful
single-source routes.

### Pydantic models (sketch)

```python
SOURCE_TYPES = ("book", "article", "website", "report", "other")


class SourceMetadataView(BaseModel):
    source_path: str
    source_type: str
    authors: list[str]
    title: str | None
    container: str | None
    publisher: str | None
    publication_date: str | None
    url: str | None
    access_date: str | None
    verbatim_overrides: dict[str, str]  # style -> text; empty map when none
    # derived, for the UI:
    display_name: str  # resolved title-else-filename label (Req 9)
    missing_required: list[str]  # per current source_type (style-agnostic base)
    is_complete: bool


class UpdateMetadataRequest(BaseModel):
    source_type: str | None = None
    authors: list[str] | None = None
    title: str | None = None
    container: str | None = None
    publisher: str | None = None
    publication_date: str | None = None
    url: str | None = None
    access_date: str | None = None
    # Field length bound (<=2000) enforced in the handler for clear 400s.


class FormatRequest(BaseModel):
    source_paths: list[str]  # validated 1..100 in the handler
    style: str  # validated MLA|APA in the handler


class FormattedCitationView(BaseModel):
    source_path: str
    text: str
    incomplete: bool
    missing_required: list[str]
    leading_element: str | None


class FormatResponse(BaseModel):
    style: str
    citations: list[FormattedCitationView]


class ImportRequest(BaseModel):
    format: str  # validated bibtex|ris|csljson|verbatim in the handler
    payload: str  # the raw reference text
    entry: str | None = None  # index or key selecting one entry in a multi-entry file
    style: str | None = (
        None  # MLA|APA; required (and only used) when format == verbatim
    )
```

### Endpoints

- `GET /api/references` → `{ "references": [SourceMetadataView, ...] }`. One
  entry per indexed source (from `get_store().sources()`), each merged with
  defaults; empty index → `{ "references": [] }` (Req 4.1, 4.2). The `source_file`
  used for the title fallback is derived from the `source_path`'s final segment
  when the index does not carry a separate display name.

- `GET /api/references/{source_path:path}` → `SourceMetadataView`; 404 if
  `source_path` not indexed (Req 4.3, 4.4).

- `PUT /api/references/{source_path:path}` with `UpdateMetadataRequest` →
  updated `SourceMetadataView`. 404 if not indexed (Req 4.6); 400 if
  `source_type` not in the allowed set (Req 4.7); 400 if any field > 2000 chars
  (Req 5.5). On success persists via `store.upsert(...)` and returns the effective
  record (Req 4.5).

- `POST /api/references/format` with `FormatRequest` → `FormatResponse`. Validates
  `1 <= len(source_paths) <= 100` (400 otherwise), `style in {MLA, APA}` (400
  naming the styles, Req 4.9), and that every `source_path` is indexed (404 if any
  is not, Req 4.10). Returns one `FormattedCitationView` per requested source,
  in the requested order (Req 4.8).

- `POST /api/references/{source_path:path}/import` with `ImportRequest` → the
  updated `SourceMetadataView` (now including `verbatim_overrides` and the
  resolved `display_name`). Behavior:
  - 404 if `source_path` is not indexed, store unchanged (Req 8.2).
  - 400 if `format` is not one of `bibtex|ris|csljson|verbatim`, or (for
    verbatim) `style` is not `MLA|APA`.
  - The handler dispatches to the matching parser. If the parser returns a
    `ParseError`, respond 400 naming the declared format, store unchanged
    (Req 8.3).
  - **Structured** (`bibtex|ris|csljson`): the parser returns a list of
    `ParsedEntry`. The handler selects the single entry named by `entry`
    (index or key); if the file has multiple entries and `entry` is missing or
    doesn't match, respond 400. The selected entry's mapped fields are persisted
    via `store.upsert(...)`; a present type indicator sets `source_type`, an
    absent one leaves it unchanged (Req 8.4, 8.6, 8.7, 8.11). A stored title
    makes `display_name` resolve to it thereafter (Req 8.8).
  - **Verbatim**: the handler stores `payload` as `verbatim_overrides[style]`
    only, changing no other field and not the `display_name` (Req 8.9). Re-import
    for the same style replaces that override; structured re-import overwrites the
    mapped fields — import is replaceable/idempotent (Req 8.12).
  - On success returns the effective, updated `SourceMetadataView`.

- `GET /api/references/export?style=MLA|APA` → an ordered, whole-archive
  bibliography: one entry per indexed source, sorted by the export comparator
  (case- and diacritic-insensitive leading element, full-text tie-break),
  incomplete sources included and flagged (Req 10.1, 10.3, 10.4). 400 on bad style
  (Req 10.2); empty index → empty block + a no-sources indication (Req 10.5). This
  is the stretch endpoint and reuses the same formatter and comparator as the Ask
  bibliography.

### Index-lifecycle wiring (in `indexer.py`)

```python
# remove_source(source_path): add beside the existing manifest/group hooks
get_source_metadata_store().prune_source(source_path)  # Req 4.11

# reset_index(): add beside get_group_store().clear_all_members()
get_source_metadata_store().clear_all()
```

## Frontend design

### api.ts additions

New typed helpers mirroring the existing ones (typed `fetch`, throw on non-OK):

```ts
export type SourceType = "book" | "article" | "website" | "report" | "other";
export type CitationStyle = "MLA" | "APA";
export type StyleSelection = "off" | "MLA" | "APA";

export interface SourceMetadata {
  source_path: string;
  source_type: SourceType;
  authors: string[];
  title: string | null;
  container: string | null;
  publisher: string | null;
  publication_date: string | null;
  url: string | null;
  access_date: string | null;
  verbatim_overrides: Record<string, string>;
  display_name: string;
  missing_required: string[];
  is_complete: boolean;
}
export interface FormattedCitation {
  source_path: string;
  text: string;
  incomplete: boolean;
  missing_required: string[];
  leading_element: string | null;
}

export function listReferences(): Promise<{ references: SourceMetadata[] }>;
export function getReference(sourcePath: string): Promise<SourceMetadata>;
export function updateReference(
  sourcePath: string, fields: Partial<SourceMetadata>
): Promise<SourceMetadata>;
export function formatCitations(
  sourcePaths: string[], style: CitationStyle
): Promise<{ style: CitationStyle; citations: FormattedCitation[] }>;

export type ImportFormat = "bibtex" | "ris" | "csljson" | "verbatim";
export function importReference(
  sourcePath: string,
  body: { format: ImportFormat; payload: string; entry?: string; style?: CitationStyle }
): Promise<SourceMetadata>;  // returns the updated view incl. display_name/verbatim_overrides
```

The scope picker and Sources labels read the `display_name` field now present on
selectable-sources and citation payloads; when no title is stored it falls back
to the file name, so existing behavior is preserved (Req 9).

### References_View

A new collapsible panel (consistent with the existing Groups manager) or a
sibling route under `frontend/app/`. On load it calls `listReferences()` (backed
by the same indexed-source truth as `/api/selectable-sources`).

- Lists every indexed source by `source_path` (Req 5.1); a no-sources empty state
  when the list is empty (Req 5.2).
- Each row shows the current effective metadata with an explicit "empty" marker
  per absent field (Req 5.3), and names each field that is still required for the
  current `source_type` (from `missing_required`, Req 5.9).
- An edit form per source: a `source_type` `<select>` limited to the five allowed
  types (Req 5.4); text inputs for `authors` (as a list), `title`, `container`,
  `publisher`, `publication_date`, `url`, `access_date`, each bounded to 2000
  chars (Req 5.5).
- Client-side validation: `url` and date fields are checked before sending;
  invalid fields are flagged, the save is not sent, and entered values are
  preserved (Req 5.8). The same date predicate as Property 19 is used.
- Save calls `updateReference(...)`; on success the row reflects saved values
  (Req 5.6). A failure or a >10s timeout (an `AbortController` deadline) shows an
  error and keeps the user's entered values (Req 5.7).
- Live preview: while editing, the row shows the formatted citation in the
  currently selected style, refreshed within ~1s of an edit (Req 5.10). The
  preview calls `formatCitations([source_path], style)` (debounced); when the
  Style_Selector is Off the preview defaults to MLA (Req 1.4).

### Ask_View: Style_Selector and Bibliography_Block

- **Style_Selector**: a `<select>` with exactly `Off`, `MLA`, `APA` (Req 6.1),
  defaulting to `Off` (Req 6.2), held in a `useState<StyleSelection>("off")`.
- The existing streamed `("citations", [...])` event is already captured into
  `citations` state. The Ask_View continues to render the inline `[n]` markers
  and the Sources panel exactly as today, independent of the selector (Req 6.11).
- **Bibliography_Block** renders beneath the Sources panel only when the selector
  is MLA or APA *and* there is at least one cited source (Req 6.3, 6.4, 6.5). Its
  title is "Works Cited" for MLA and "References" for APA (Req 6.6, 6.7).
- Entries are sorted by the ordering comparator (leading element, case-insensitive,
  full-text tie-break; no-leading-element last) — Req 6.8, 6.9.

**How the block gets its citations (design decision).** When the selector is set
to MLA/APA (or changed while an answer is displayed), the Ask_View computes the
distinct `source_path`s among the current `citations`, calls
`formatCitations(distinctPaths, style)`, sorts the returned citations with the
comparator, and renders them. Changing the style re-issues `formatCitations` and
re-renders within ~1s **without** calling `/api/ask` again (Req 6.10).

*Justification:* formatting must be deterministic and spec-faithful, and both MLA
and APA rules (author inversion, `(n.d.)`, MLA access-date, incomplete marking)
already live in the backend formatter that the References preview and export use.
Re-implementing them in TypeScript would duplicate the rules and risk divergence
from the property-tested backend. Because the formatter is pure and needs no
model or network, the `format` call is cheap and offline-friendly, so calling it
client-side on style change (rather than baking formatted strings into the `ask`
stream) keeps a single source of truth and satisfies the "re-render without
resubmitting" requirement cleanly. The distinct-source mapping (Property 16) is
done client-side from the already-fetched citations.

## Alphabetical ordering and sorting rules

A single comparator underlies both the Ask Bibliography_Block and the export,
differing only in whether the primary key folds diacritics.

- **Leading element**: the author surname when the citation has one, else the
  title. It is `None` when the citation has neither (only possible for a
  degenerate record; the title fallback in Property 4 normally guarantees a
  title, so `None` is an edge the comparator still handles).
- **Primary key**: the leading element, case-folded (`str.casefold()`) for the
  Ask block; additionally diacritic-folded (Unicode NFKD then drop combining
  marks) for the export (Req 10.3), so "Éluard" and "Eluard" compare equal.
- **Tie-break**: the full citation `text`, compared the same way, so equal
  leading elements order deterministically (Req 6.8, 10.3).
- **No leading element sorts last** (Req 6.9): entries with `leading_element ==
  None` are placed after all entries that have one, then ordered among themselves
  by full text.

The resulting relation is a total order (Property 17): every pair is comparable,
distinct citation texts never tie, and the sort is a stable permutation of the
input.

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all
valid executions of a system — essentially, a formal statement about what the
system should do. Properties serve as the bridge between human-readable
specifications and machine-verifiable correctness guarantees.*

The properties below are the consolidated result of analyzing every acceptance
criterion (see prework). Redundant per-type and per-verb criteria were folded
into parameterized properties: the five per-`source_type` required-field
criteria (2.3–2.7) and the formatter contract (3.4, 3.6) collapse into one
"incomplete iff a required field is missing" property parameterized over type
and style; the identical 404 behavior for get and update (4.4, 4.6, 4.10)
collapses into one property; the identical style gate (1.5, 4.9, 10.2) collapses
into one; and the Ask and export ordering (6.8, 6.9, 10.3) collapse into one
ordering property with a comparator parameter. All formatter/store/ordering
properties are pure and run with no models and no network — important because
the app must work on the CPU-only laptop.

Properties 20–25 cover the new import (Req 8) and display-name (Req 9) behavior.
They are equally pure and offline: the parsers, the formatter's verbatim
short-circuit, and the `display_name` resolver are all pure functions. Several
new criteria fold into the existing set rather than adding properties: the
import route's unknown-source 404 (8.2) extends Property 12; absent-field
handling on structured import (8.6) is subsumed by the field-mapping property
(20) and the store round-trip (Property 7); and the "stored title becomes the
Display_Name" criterion (8.8) is the stored-title branch of Property 24.

### Property 1: Formatter is total and produces a non-empty, title-bearing citation

*For any* `Source_Metadata` record (however sparse) and *any* style in
{MLA, APA}, `format_citation` returns a non-empty string that contains the
record's effective title (per the title fallback), and never raises.

**Validates: Requirements 3.7, 1.4**

### Property 2: Formatter is deterministic

*For any* record and style, formatting the same input twice yields byte-identical
output (`text`, `missing_required`, `leading_element`). This purity is what lets
the Ask_View re-format on a style change without resubmitting the question.

**Validates: Requirements 1.2, 1.3, 6.10**

### Property 3: Incomplete marker holds exactly when a required field is missing

*For any* record and *any* style, `missing_required` equals exactly the set of
fields required for that record's `source_type` and style that are absent (a
field is present iff it has at least one non-whitespace character; `authors` is
present iff it has at least one such entry), and `incomplete == (missing_required
!= empty)`. The required set is: book → {title, authors}; article → {title,
authors, container}; website → {title, url}, plus `access_date` when the style is
MLA and no `publication_date` is stored; report → {title}; other → {title}.

**Validates: Requirements 2.3, 2.4, 2.5, 2.6, 2.7, 3.4, 3.6**

### Property 4: Title fallback is correct and non-empty

*For any* record with no stored `title`, the effective title equals the
`source_file` with its file extension removed when `source_file` is present,
otherwise the final path segment of `source_path` with its extension removed;
and the effective title is always non-empty.

**Validates: Requirements 3.1**

### Property 5: With no authors, the title occupies the author position and no placeholder is emitted

*For any* record whose `authors` is empty, the formatted citation's leading
element is the effective title and the output contains no author-placeholder
token (e.g. no "Anonymous" / "N.A." / "Unknown").

**Validates: Requirements 3.3**

### Property 6: APA renders (n.d.) for a missing publication date

*For any* record with no stored `publication_date`, APA output contains the
literal text `(n.d.)` in the date position.

**Validates: Requirements 3.2**

### Property 7: Store round-trips, and absent fields stay absent

*For any* set of `Source_Metadata` records with arbitrary subsets of fields
present (including `source_type`), saving then loading into a fresh store
reproduces every record exactly; a field left absent on write is absent (not
coerced to a present empty/`null`) on read.

**Validates: Requirements 2.1, 2.2, 2.8**

### Property 8: Missing or corrupt persistence loads as empty and never raises

*For any* byte content of the metadata file (arbitrary bytes, non-JSON, a
non-object top level, a missing/`non-mapping` `records` key, or structurally
invalid records) — and for a missing file — `_load` does not raise and the store
behaves as empty.

**Validates: Requirements 2.10**

### Property 9: Source removal and index reset drop metadata

*For any* store contents and *any* `source_path`, `prune_source(source_path)`
removes that source's record and leaves every other record unchanged; and
`clear_all()` removes every record. (Integration-checked: `indexer.remove_source`
calls `prune_source`, `indexer.reset_index` calls `clear_all`.)

**Validates: Requirements 4.11**

### Property 10: List-all covers exactly the indexed sources, each effective

*For any* index contents and *any* stored metadata, the list-all response has
exactly one entry per indexed `source_path` (no more, no fewer), and each entry
is the effective record (stored fields merged with computed defaults).

**Validates: Requirements 4.1, 4.2**

### Property 11: Update-then-read round-trips on an indexed source

*For any* indexed `source_path` and *any* valid submitted fields, after a
successful update the reloaded effective record reflects exactly the submitted
fields (merged with defaults).

**Validates: Requirements 4.5**

### Property 12: Unknown source is a 404 and leaves the store unchanged

*For any* `source_path` that is not in the index, get-one, update, and import all
return 404 and the on-disk metadata is byte-for-byte unchanged; likewise a format
batch containing any unknown `source_path` returns 404 without mutating the store.

**Validates: Requirements 4.4, 4.6, 4.10, 8.2**

### Property 13: The style gate accepts exactly MLA and APA

*For any* string, it is accepted as a Citation_Style iff it is exactly `MLA` or
`APA`; every other value (across format, update, and export requests) is rejected
with a 400 naming MLA and APA as the supported styles, and the previously active
style / stored state is unchanged.

**Validates: Requirements 1.1, 1.5, 4.9, 10.2**

### Property 14: A disallowed source_type is a 400 and leaves the store unchanged

*For any* `source_type` string outside {book, article, website, report, other},
an update request is rejected with 400 naming the allowed types, and the store is
unchanged.

**Validates: Requirements 4.7**

### Property 15: Batch format returns one citation per requested source

*For any* list of between 1 and 100 indexed `source_path` values and *any* valid
style, the format response contains exactly one formatted citation per requested
source, covering every requested source.

**Validates: Requirements 4.8**

### Property 16: The bibliography has one entry per distinct cited source

*For any* citation list emitted by the Ask flow, the Bibliography_Block contains
exactly one entry per distinct `source_path` among the cited items (duplicates
across cited chunks collapse to a single bibliography entry).

**Validates: Requirements 6.4, 6.5**

### Property 17: Bibliography ordering is a total order with the documented key

*For any* set of formatted citations, sorting is a permutation of the input that
is non-decreasing under the key (case-insensitive leading element — author
surname else title — with the full citation text as tie-break), the induced
relation is a total order (reflexive, antisymmetric on distinct texts,
transitive, total), and any citation with no leading element is ordered after
every citation that has one. For the whole-archive export the leading-element
comparison additionally folds diacritics (so e.g. "Éluard" and "Eluard" compare
equal in the primary key).

**Validates: Requirements 6.8, 6.9, 10.3**

### Property 18: Export covers every indexed source, marking (not omitting) incomplete ones

*For any* index contents and *any* valid style, the exported bibliography
contains exactly one entry per indexed source; sources whose citation is
incomplete are included with their best-effort citation and flagged incomplete
rather than omitted.

**Validates: Requirements 10.1, 10.4, 10.5**

### Property 19: The date validator accepts exactly ISO calendar dates

*For any* string, the client/`server` date validator accepts it iff it is a valid
`YYYY-MM-DD` calendar date (rejecting malformed shapes and impossible dates such
as `2023-02-30`).

**Validates: Requirements 5.8**

### Property 20: Structured parse maps entry fields and infers source_type

*For any* structured entry (BibTeX, RIS, or CSL-JSON) built from a known set of
bibliographic fields, parsing yields exactly those fields mapped onto
`Source_Metadata` (`authors`, `title`, `container`, `publisher`,
`publication_date`, `url`), and a mappable field the entry omits maps to absent
(not a coerced empty). *For any* entry-type indicator, the inferred `source_type`
equals the mapping table's result (unknown indicators → `other`); when the entry
carries no type indicator, the import leaves the source's existing `source_type`
unchanged.

**Validates: Requirements 8.4, 8.6, 8.7**

### Property 21: Parsers are total and never raise

*For any* input string (arbitrary text or garbage bytes decoded to text), each
parser (`parse_bibtex`, `parse_ris`, `parse_csljson`, `parse_verbatim`) returns
either a normalized result or a `ParseError` and never raises an uncontrolled
exception; unparseable input yields a `ParseError` that the API turns into a 400
naming the declared format, with the store left unchanged.

**Validates: Requirements 8.3**

### Property 22: Date normalization preserves precision

*For any* structured date, a full-precision date (year, month, and day) is
normalized to `YYYY-MM-DD`, and a lower-precision date (year only, or year and
month) is stored exactly as the entry provides it — no month or day is
fabricated.

**Validates: Requirements 8.5**

### Property 23: Verbatim override precedence and isolation

*For any* record whose `verbatim_overrides` has an entry for a requested style,
`format_citation(record, style).text` equals that override string exactly, with
`incomplete == False` and `missing_required == []`, while formatting the *other*
style ignores the override and produces field-based output. A verbatim import
writes only `verbatim_overrides[style]`: for any prior record, text, and style,
after the import every other field and the resolved `display_name` are unchanged.

**Validates: Requirements 8.9, 8.10**

### Property 24: Display-name resolution is total and title-preferring

*For any* `(source_path, stored_title, source_file)`, `display_name` returns the
`stored_title` when it has at least one non-whitespace character, otherwise the
file name (`source_file` when present, else the final path segment of
`source_path`); the result is always non-empty, and the function is total and
deterministic.

**Validates: Requirements 9.1, 9.2**

### Property 25: Import is replaceable and idempotent, one entry at a time

*For any* structured entry, importing it and then re-importing the same entry
leaves the same mapped fields (idempotent). *For any* two override texts for the
same style, importing the second replaces the first for that style and leaves the
other style's override untouched (replaceable). *For any* multi-entry file and a
valid entry selector (index or key), the parser returns every entry with a
distinct key and the import attaches exactly the selected entry's mapped fields.

**Validates: Requirements 8.11, 8.12**

## Error Handling

The design keeps a hard line between *never-raise* internals and *typed HTTP
errors* at the API edge.

**Metadata_Store (never raises).** Following `GroupStore`, `_load` treats any
unreadable/corrupt/wrong-shape file as empty (Property 8). Mutations take the
`threading.Lock`; `_save` writes a temp file then atomically `.replace()`s so a
crash mid-write leaves the previous good file intact (Req 2.11). The store never
validates business rules — that is the API's job — so it cannot reject a caller.

**Metadata_API (typed errors, matching `main.py` conventions):**

- Unknown `source_path` (not in `get_store().sources()`) → `HTTPException(404)`
  with a message naming the source. No store mutation (Property 12).
- `source_type` outside the allowed set → `HTTPException(400)` naming the allowed
  types. No store mutation (Property 14). Enforced before persistence.
- Style neither MLA nor APA → `HTTPException(400)` naming MLA and APA
  (Property 13). Missing style where required → `422`/`400` from the Pydantic
  model (Req 1.6).
- Batch size outside 1..100 → `HTTPException(400)` stating the bound (Req 4.8).
- Any string field over 2000 chars → `HTTPException(400)` naming the field
  (Req 5.5).
- A malformed `url`/date reaching the API → `HTTPException(400)` naming the field
  (the frontend also validates client-side and does not send, Req 5.8).
- Import with an unknown `source_path` → `HTTPException(404)`, store unchanged
  (Req 8.2). Bad `format` value, or a missing/invalid `style` for a verbatim
  import → `HTTPException(400)`. A parser `ParseError` → `HTTPException(400)`
  naming the declared format, store unchanged (Req 8.3). A multi-entry file with
  a missing/unmatched entry selector → `HTTPException(400)`. The parsers
  themselves never raise (Property 21); the API converts their typed
  `ParseError` into the 400.

**Formatter (never raises).** The formatter is total (Property 1): missing data
is expressed through fallbacks and `missing_required`, never exceptions. This
guarantees the References preview and the Bibliography_Block always render.

**Frontend.** Save failures or a >10s timeout show an error and retain the user's
entered values (Req 5.7); invalid `url`/date fields are flagged and the save is
not sent (Req 5.8). The Style_Selector and Bibliography_Block never block the
existing answer/Sources rendering (Req 6.11).

## Testing Strategy

**Dual approach.** Property-based tests (Hypothesis) cover the universal
formatter/store/ordering/API-invariant properties; example-based unit tests
cover concrete style conformance and specific UI behaviors; a small number of
integration/component tests cover wiring.

Property-based testing is the right tool here because the core of the feature —
the Citation_Formatter, the effective-record layer, the Metadata_Store, the
ordering comparator, the **Import_Parser**, and the **display_name resolver** —
are **pure functions and a JSON round-trip store** over a large input space,
exactly the shape `source-selection-and-groups` already tests. Crucially, none of
the formatter/store/ordering/parser/display-name tests need Ollama or a network,
so they run on the CPU-only laptop and in CI. The parsers and the formatter's
verbatim short-circuit take strings in and return dataclasses out — no models, no
I/O — which makes parser totality and override precedence cheap to test over
thousands of generated inputs.

**Backend (pytest + Hypothesis, matching `backend/tests/` style):**

- Reuse the isolation pattern from `conftest.py`/`test_group_store.py`: build a
  `SourceMetadataStore` bound to a per-test temp file (never the real
  `data/source_metadata.json`), via a `make_metadata_store(path)` helper.
- Property tests use `@hypothesis.settings(max_examples=100)` (minimum 100
  iterations) and a `source_metadata` strategy that generates records with
  arbitrary subsets of fields present, all five `source_type`s, optional
  `verbatim_overrides` (none / one style / both), and edge-y strings
  (whitespace-only, diacritics, missing extensions, path-like `source_path`s).
- Each property test is tagged with a comment in the format
  **Feature: citation-formatting, Property {number}: {property_text}**.
- Implement each Correctness Property with a single property-based test:
  - P1–P6 → `test_formatter.py` (totality, determinism, incomplete-iff-missing
    parameterized over type×style, title fallback, no-author position, APA n.d.).
  - P7, P8 → `test_metadata_store.py` (round-trip incl. absent-stays-absent;
    corrupt/missing tolerance over `st.binary()` and crafted bad-shape bytes,
    mirroring `corrupt_bytes` in `test_group_store.py`).
  - P9 → `test_metadata_store.py` (prune/clear) + `test_indexer_pruning`-style
    integration asserting `remove_source`/`reset_index` invoke the hooks.
  - P10–P15 → `test_references_api.py` using FastAPI `TestClient` with the
    vector store's `sources()` stubbed to a controlled set (list coverage,
    update round-trip, 404, style gate, bad source_type, batch 1..100).
  - P16, P17, P18 → `test_bibliography.py` (distinct-source mapping; ordering as a
    total order with the leading-element key and no-leading-element-last, plus
    diacritic folding for export; export covers-all-and-flags-incomplete).
  - P19 → `test_validation.py` (date validator accepts exactly ISO calendar
    dates; stateful/round-trip against `datetime.date.fromisoformat`).
  - P20, P22, P25 → `test_import_parsers.py` (structured field mapping and
    source_type inference over generated entries rendered to each format then
    parsed; date-precision preservation; idempotent re-import and one-at-a-time
    multi-entry selection).
  - P21 → `test_import_parsers.py` (parser totality: every parser over
    `st.text()` — and decoded `st.binary()` — returns a result or `ParseError`,
    never raises).
  - P23 → `test_formatter.py` (verbatim override precedence and per-style
    isolation) + a `test_references_api.py` case that a verbatim import writes
    only `verbatim_overrides[style]`.
  - P24 → `test_display_name.py` (resolver is total, title-preferring, non-empty).
- Example unit tests (`test_formatter_examples.py`): hand-written records → exact
  expected MLA and APA strings per `source_type` (Req 1.2, 1.3), the default-MLA
  behavior (Req 1.4), and the atomic-write mechanism (target parses, no leftover
  `.json.tmp`, Req 2.11) — mirroring `test_save_produces_valid_json_...`.
- Import API tests (`test_references_api.py`): each of the four formats accepted
  and dispatched (Req 8.1); unknown source → 404 (Req 8.2); unparseable payload →
  400 naming the format, store unchanged (Req 8.3); structured import persists
  mapped fields and inferred type (Req 8.4, 8.7); verbatim import writes only the
  override and does not change `display_name` (Req 8.9); re-import replaces
  (Req 8.12); the returned view carries `verbatim_overrides` and `display_name`.
- Display-name wiring tests: `/api/selectable-sources` and the citation payload
  from `rag._format_context` include a resolved `display_name` (Req 9.3);
  frontend component tests assert the scope picker, Sources list, and References
  rows label sources by it.
- A model-based `RuleBasedStateMachine` (optional, mirroring
  `GroupStoreModel`) exercises upsert/prune/clear/import against a `dict`
  reference model to catch persistence/round-trip regressions across operation
  sequences.

**Representative fixtures** (small, checked-in test inputs for the parser and
verbatim tests — pure strings, no models):

```bibtex
@book{stewart2015,
  author = {Stewart, James},
  title = {Calculus},
  publisher = {Cengage Learning},
  year = {2015}
}
```

```ris
TY  - JOUR
AU  - Smith, Jane
TI  - On Widgets
JO  - Journal of Widgets
PY  - 2020/06/01
UR  - https://example.org/widgets
ER  -
```

```json
[
  {
    "id": "doe2021",
    "type": "webpage",
    "title": "A Web Resource",
    "author": [{ "family": "Doe", "given": "John" }],
    "container-title": "Example Site",
    "URL": "https://example.org/resource",
    "issued": { "date-parts": [[2021, 3, 14]] }
  }
]
```

A verbatim APA string used as a Verbatim import fixture:

```
Stewart, J. (2015). Calculus (8th ed.). Cengage Learning.
```

**PBT library & config:** use Hypothesis (already a dependency; see
`backend/.hypothesis/`), do not hand-roll property testing, ≥100 iterations per
property test, and reference the design property in each test's tag.

**Frontend (component/example tests under `frontend/app/__tests__`):**

- References_View: renders one row per indexed source; empty state when none;
  empty markers for absent fields; type dropdown offers exactly the five types;
  invalid url/date flagged and not sent; save success/failure/timeout paths;
  live preview updates on edit (Req 5.1–5.10); rows label sources by
  `display_name` (Req 9.3); an import control posts to the import endpoint
  (mocked) and reflects the returned view.
- Scope picker / groups: sources are labeled by the `display_name` returned from
  selectable-sources, falling back to the file name when no title is stored
  (Req 9.2, 9.3).
- Ask_View: Style_Selector has exactly Off/MLA/APA and defaults to Off; Off hides
  the block; MLA→"Works Cited", APA→"References"; block hidden with zero cited
  sources; switching style calls the `format` endpoint (mocked) rather than
  `/api/ask`; inline `[n]` markers and the Sources list are unaffected by the
  selector (Req 6.1–6.11).

**Out of scope for PBT (intentionally):** the holistic "conforms to the MLA/APA
spec" claim (example tests with expected strings), UI rendering/layout and timing
budgets (component/example tests), the first-create `access_date`-defaults-to-
today behavior (example test with the observed local date), the format-dispatch
acceptance of the four import formats (Req 8.1, example tests per format), and
the display-name *wiring* across views (Req 9.3, integration/component tests) —
none of these are universal "for all inputs" statements. The display-name
*resolver* itself (Property 24) and the parsers (Properties 20–22, 25) are pure
and are property-tested.

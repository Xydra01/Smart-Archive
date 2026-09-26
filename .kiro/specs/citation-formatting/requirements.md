# Requirements Document

## Introduction

Smart Archive already answers questions with a "Sources" list, where each cited
source carries a `source_file`, `source_path`, `location` (e.g. "p. 12"),
`file_type`, and `content_type`. This feature adds the ability to turn those
sources into properly formatted academic citations in two styles — MLA (9th
edition) and APA (7th edition) — and to manage the bibliographic metadata those
citations require.

Uploaded files rarely contain every field a citation needs, so the feature
introduces a per-source bibliographic metadata store (mirroring the existing
`data/groups.json` pattern), an API to read and write that metadata keyed by
`source_path`, a References management UI to edit it, and an Ask-view style
selector (Off / MLA / APA) that renders a formatted "Works Cited" (MLA) or
"References" (APA) block beneath answers. Citation strings are assembled purely
from stored fields with documented fallbacks for missing data — no LLM is
involved in producing the reference strings.

The app is local and single-user: there is no authentication, no multi-tenant
concern, and it runs fully offline.

## Glossary

- **System**: The Smart Archive application as a whole (backend + frontend).
- **Citation_Formatter**: The backend component that assembles a formatted
  citation string for one source in a given style (MLA or APA).
- **Metadata_Store**: The persistent per-source bibliographic metadata store,
  a JSON file at `data/source_metadata.json`, keyed by `source_path`.
- **Metadata_API**: The backend HTTP endpoints that read and write per-source
  bibliographic metadata.
- **References_View**: The frontend page or panel where the user views all
  indexed sources and edits each source's bibliographic fields.
- **Ask_View**: The existing frontend view that submits questions and renders
  an answer with its Sources list.
- **Style_Selector**: The Ask_View control that chooses the active citation
  style (Off, MLA, or APA).
- **Bibliography_Block**: The formatted, sorted list of citations rendered
  beneath an answer when a citation style is active. Titled "Works Cited" for
  MLA and "References" for APA.
- **Source_Metadata**: The set of bibliographic fields stored for one source
  (`source_path`), including its `source_type` and style-relevant fields.
- **Source_Type**: The kind of source a record describes; one of `book`,
  `article`, `website`, `report`, or `other`.
- **Citation_Style**: A supported academic style; one of `MLA` or `APA`.
- **MLA**: Modern Language Association style, 9th edition.
- **APA**: American Psychological Association style, 7th edition.
- **Access_Date**: The date on which an online source was consulted, required
  by MLA for online sources lacking a publication date.
- **Publication_Date**: The date a source was published.
- **source_path**: The stable identifier for an indexed source, already used
  across the index, selectable-sources, and groups.
- **complete citation**: A citation for which every field required by the
  source's Source_Type is present with at least one non-whitespace character.
- **Reference_Import**: The act of attaching an external reference (structured
  or verbatim) to one indexed source.
- **Import_Format**: One of BibTeX, RIS, CSL-JSON, or Verbatim (a pasted
  formatted MLA or APA string).
- **Verbatim_Override**: A stored, pre-formatted citation string for a specific
  Citation_Style, displayed exactly as given for that style.
- **Display_Name**: The label used for a source across the application, resolved
  as the imported/stored title when present, else the source file name.

## Requirements

### Requirement 1: Supported citation styles

**User Story:** As a researcher, I want the archive to support both MLA and APA
citation styles, so that I can produce citations matching my assignment's
required style.

#### Acceptance Criteria

1. THE System SHALL support exactly two citation styles, identified as MLA (9th
   edition) and APA (7th edition), and SHALL NOT accept any other citation style
   value.
2. WHERE the active Citation_Style is MLA, THE Citation_Formatter SHALL produce
   citation strings whose element ordering, punctuation, and formatting conform
   to the MLA 9th edition specification.
3. WHERE the active Citation_Style is APA, THE Citation_Formatter SHALL produce
   citation strings whose element ordering, punctuation, and formatting conform
   to the APA 7th edition specification.
4. WHEN no Citation_Style is explicitly selected, THE Citation_Formatter SHALL
   use MLA (9th edition) as the active Citation_Style.
5. IF a requested citation style value is neither MLA nor APA, THEN THE
   Metadata_API SHALL reject the request, return an error response indicating
   the value is unsupported and naming MLA and APA as the supported styles, and
   leave the previously active Citation_Style unchanged.
6. IF a request omits the citation style value where one is required, THEN THE
   Metadata_API SHALL reject the request and return an error response indicating
   that a citation style value is required.

### Requirement 2: Bibliographic metadata model

**User Story:** As a researcher, I want to record what kind of source each item
is and its bibliographic details, so that citations can be formatted correctly
for that source type.

#### Acceptance Criteria

1. THE Metadata_Store SHALL store, for each `source_path`, a Source_Type whose
   value is one of `book`, `article`, `website`, `report`, or `other`.
2. THE Metadata_Store SHALL store the following common fields for each
   `source_path`: `authors` (an ordered list of strings), `title`, `container`
   (journal, website, or book title containing the work), `publisher`,
   `publication_date` (an ISO 8601 calendar date, `YYYY-MM-DD`), `url`, and
   `access_date` (an ISO 8601 calendar date, `YYYY-MM-DD`).
3. WHERE the Source_Type is `book`, THE Metadata_Store SHALL treat `title` and
   at least one entry in `authors` as the fields required to form a complete
   citation, and SHALL treat `publisher` and `publication_date` as optional.
4. WHERE the Source_Type is `article`, THE Metadata_Store SHALL treat `title`,
   at least one entry in `authors`, and `container` as the fields required to
   form a complete citation, and SHALL treat `publication_date` as optional.
5. WHERE the Source_Type is `website`, THE Metadata_Store SHALL treat `title`
   and `url` as the fields required to form a complete citation, and SHALL treat
   `authors`, `container`, and `publication_date` as optional.
6. WHERE the Source_Type is `report`, THE Metadata_Store SHALL treat `title` as
   the field required to form a complete citation, and SHALL treat `authors`,
   `publisher`, and `publication_date` as optional.
7. WHERE the Source_Type is `other`, THE Metadata_Store SHALL treat `title` as
   the field required to form a complete citation, and SHALL treat all other
   fields as optional.
8. WHEN a Source_Metadata record omits any storable field, THE Metadata_Store
   SHALL persist that field as absent rather than rejecting the record.
9. THE Metadata_Store SHALL persist records as JSON at
   `data/source_metadata.json`, keyed by `source_path`.
10. IF the metadata file is missing, or its contents are not parseable as a JSON
    object keyed by `source_path`, THEN THE Metadata_Store SHALL behave as if no
    metadata is stored and SHALL NOT raise an error to callers.
11. WHEN the Metadata_Store writes the metadata file, THE Metadata_Store SHALL
    write to a temporary file and atomically replace the target file.

### Requirement 3: Missing-field fallbacks and auto-fill

**User Story:** As a researcher, I want the system to fill in citation fields
from what it already knows and to handle missing information gracefully, so that
I get a usable citation even when a source lacks complete metadata.

#### Acceptance Criteria

1. WHEN a source has no stored `title`, THE Citation_Formatter SHALL use as the
   title the source's `source_file` with its file extension removed; and WHEN
   `source_file` is absent, THE Citation_Formatter SHALL use the final path
   segment of `source_path` with its file extension removed.
2. WHEN a source has no stored `publication_date` AND the active Citation_Style
   is APA (7th edition), THE Citation_Formatter SHALL render the date position as
   the literal text `(n.d.)`.
3. WHEN a source has no stored `authors`, THE Citation_Formatter SHALL begin the
   citation with the title in the position an author would occupy, and SHALL NOT
   emit any placeholder author text.
4. WHERE the Source_Type is `website` AND no `publication_date` is stored, THE
   Citation_Formatter SHALL include the Access_Date in MLA output; and IF no
   `access_date` is stored either, THEN THE Citation_Formatter SHALL mark the
   citation as incomplete.
5. WHEN a Source_Metadata record is first created for a source AND no
   `access_date` is provided, THE System SHALL default `access_date` to the
   current calendar date in the server's local time zone.
6. IF a field required for a source's Source_Type and active Citation_Style is
   absent AND has no defined fallback, THEN THE Citation_Formatter SHALL still
   produce a citation string from the available fields AND SHALL return an
   indicator identifying each missing required field.
7. THE Citation_Formatter SHALL produce a non-empty citation string for any
   source in the index, at minimum containing the title determined per criterion
   1.

### Requirement 4: Metadata API

**User Story:** As a researcher, I want to read and update each source's
bibliographic metadata through an API, so that the References_View and Ask_View
can display and edit citation data.

#### Acceptance Criteria

1. WHEN a client requests the metadata for all indexed sources, THE Metadata_API
   SHALL return one record per indexed `source_path`, merging stored fields with
   any auto-filled defaults.
2. WHEN a client requests the metadata for all indexed sources AND the index has
   no sources, THE Metadata_API SHALL return an empty collection.
3. WHEN a client requests the metadata for a single indexed `source_path`, THE
   Metadata_API SHALL return that source's Source_Metadata.
4. IF a client requests the metadata for a `source_path` that is not in the
   index, THEN THE Metadata_API SHALL reject the request with a 404 status and
   leave the Metadata_Store unchanged.
5. WHEN a client submits updated bibliographic fields for an indexed
   `source_path`, THE Metadata_API SHALL persist those fields to the
   Metadata_Store and SHALL return the updated Source_Metadata.
6. IF a client submits updated bibliographic fields for a `source_path` that is
   not in the index, THEN THE Metadata_API SHALL reject the request with a 404
   status and leave the Metadata_Store unchanged.
7. IF a client submits a Source_Type outside the allowed set, THEN THE
   Metadata_API SHALL reject the request with a 400 status and a message naming
   the allowed types, and leave the Metadata_Store unchanged.
8. WHEN a client requests formatted citations for between 1 and 100
   `source_path` values in a specified Citation_Style (MLA or APA), THE
   Metadata_API SHALL return the formatted citation string for each requested
   source in that style.
9. IF a client requests formatted citations in a Citation_Style that is neither
   MLA nor APA, THEN THE Metadata_API SHALL reject the request with a 400 status
   and a message naming the supported styles.
10. IF a client requests formatted citations AND any requested `source_path` is
    not in the index, THEN THE Metadata_API SHALL reject the request with a 404
    status.
11. WHEN a source is removed from the index, THE Metadata_Store SHALL drop that
    source's Source_Metadata.

### Requirement 5: References management UI

**User Story:** As a researcher, I want a dedicated place to see all my sources
and edit their citation details, so that I can complete the bibliographic
information that uploaded files do not contain.

#### Acceptance Criteria

1. THE References_View SHALL display every indexed source, identified by its
   `source_path`.
2. WHEN no sources are indexed, THE References_View SHALL show a no-sources
   indication.
3. WHEN the References_View loads, THE References_View SHALL show each source's
   current Source_Metadata, including auto-filled defaults, and SHALL show an
   explicit empty indicator for each absent field.
4. THE References_View SHALL let the user set a source's Source_Type from the
   allowed set (`book`, `article`, `website`, `report`, `other`).
5. THE References_View SHALL let the user edit the common bibliographic fields
   `authors`, `title`, `container`, `publisher`, `publication_date`, `url`, and
   `access_date`, each bounded to at most 2000 characters.
6. WHEN the user saves edits for a source, THE References_View SHALL send the
   updated fields to the Metadata_API and SHALL reflect the saved values on
   success.
7. IF the save request fails or does not complete within 10 seconds, THEN THE
   References_View SHALL indicate the error and retain the user's entered values.
8. IF an edited `url` or date field is not in a valid format, THEN THE
   References_View SHALL indicate which field is invalid, SHALL NOT send the save
   request, and SHALL preserve the user's entered values.
9. WHERE a source lacks a field required for its current Source_Type, THE
   References_View SHALL name each missing required field.
10. WHILE editing a source, THE References_View SHALL show a preview of that
    source's formatted citation in the currently selected Citation_Style,
    updating the preview within 1 second of an edit.

### Requirement 6: Ask-view style selector and bibliography block

**User Story:** As a researcher, I want to choose a citation style from within
the Ask view and see a formatted bibliography under the answer, so that I can
copy properly formatted citations for the sources that back an answer.

#### Acceptance Criteria

1. THE Ask_View SHALL present a Style_Selector offering the choices Off, MLA,
   and APA within a dropdown menu.
2. THE Style_Selector SHALL default to Off.
3. WHILE the Style_Selector is set to Off, THE Ask_View SHALL NOT render a
   Bibliography_Block.
4. WHEN the Style_Selector is set to MLA or APA AND an answer has one or more
   cited sources, THE Ask_View SHALL render a Bibliography_Block beneath the
   answer containing one formatted citation per cited source.
5. WHEN the Style_Selector is set to MLA or APA AND an answer has zero cited
   sources, THE Ask_View SHALL NOT render a Bibliography_Block.
6. WHERE the active Citation_Style is MLA, THE Ask_View SHALL title the
   Bibliography_Block "Works Cited".
7. WHERE the active Citation_Style is APA, THE Ask_View SHALL title the
   Bibliography_Block "References".
8. THE Ask_View SHALL order entries in the Bibliography_Block alphabetically by
   the leading element of each citation (author surname, else title), using a
   case-insensitive comparison, with the full citation text as the tie-break.
9. WHEN a citation in the Bibliography_Block has no leading element, THE Ask_View
   SHALL order that citation last.
10. WHEN the user changes the Style_Selector while an answer is displayed, THE
    Ask_View SHALL re-render the Bibliography_Block in the newly selected style
    within 1 second and without resubmitting the question.
11. THE Ask_View SHALL continue to render the existing inline citation markers
    (`[n]`) and Sources list independent of the Style_Selector setting.

### Requirement 8: Reference import

**User Story:** As a researcher, I want to import an external reference in a
common format and attach it to one of my sources, so that citations for that
source are driven by accurate bibliographic data or by a citation I already have
formatted.

#### Acceptance Criteria

1. THE Metadata_API SHALL accept a Reference_Import in any one of the four
   Import_Formats — BibTeX, RIS, CSL-JSON, or Verbatim — attached to a single
   indexed `source_path`.
2. IF a client submits a Reference_Import for a `source_path` that is not in the
   index, THEN THE Metadata_API SHALL reject the request with a 404 status and
   leave the Metadata_Store unchanged.
3. IF the imported payload cannot be parsed as its declared Import_Format, THEN
   THE Metadata_API SHALL reject the request with a 400 status, return an error
   naming the declared format, and leave the Metadata_Store unchanged.
4. WHEN a structured Reference_Import (BibTeX, RIS, or CSL-JSON) is parsed, THE
   Metadata_API SHALL populate the source's Source_Metadata fields from the
   parsed entry — `authors`, `title`, `container` (journal or container title),
   `publisher`, `publication_date`, and `url` — and SHALL persist those fields.
5. WHEN a structured Reference_Import provides a `publication_date` with enough
   precision to form an ISO 8601 calendar date, THE Metadata_API SHALL normalize
   that date to `YYYY-MM-DD`; and WHEN the entry provides less precision, THE
   Metadata_API SHALL store the date value the entry provides.
6. WHEN a structured Reference_Import omits a mappable field, THE Metadata_Store
   SHALL persist that field as absent rather than rejecting the record.
7. WHEN a structured Reference_Import indicates a source type (BibTeX entry
   type, RIS `TY` tag, or CSL-JSON `type`), THE Metadata_API SHALL infer and
   store the source's Source_Type from that indicator; and WHEN the format
   provides no such indicator, THE Metadata_API SHALL leave the source's
   existing Source_Type unchanged.
8. WHEN a structured Reference_Import provides a title, THE Metadata_API SHALL
   store that title as the source's `title`, and THE Display_Name SHALL
   thereafter resolve to that stored title.
9. WHEN a Verbatim Reference_Import is submitted for a given Citation_Style, THE
   Metadata_API SHALL store the pasted text as a Verbatim_Override for that
   Citation_Style only, SHALL NOT populate any other Source_Metadata field from
   the pasted text, and SHALL NOT change the source's Display_Name.
10. WHERE a Verbatim_Override exists for the active Citation_Style, THE
    Citation_Formatter SHALL output the override text exactly for that style,
    and SHALL produce field-based formatting for the other Citation_Style.
11. WHEN a structured file contains multiple entries, THE Metadata_API SHALL
    attach exactly one entry to the `source_path`, identified by the entry the
    request specifies.
12. WHEN a Reference_Import is submitted for a `source_path` that already has
    imported metadata or a Verbatim_Override, THE Metadata_API SHALL replace
    that source's affected metadata or override with the newly imported values.

### Requirement 9: Display-name resolution

**User Story:** As a researcher, I want each source labeled by its reference
title once I have imported one, so that I recognize sources by their real titles
everywhere in the application.

#### Acceptance Criteria

1. WHEN a source has a stored `title`, THE System SHALL resolve that source's
   Display_Name to the stored title.
2. WHEN a source has no stored `title`, THE System SHALL resolve that source's
   Display_Name to the source's file name.
3. THE System SHALL use the resolved Display_Name wherever a source is labeled,
   including the Sources list, search results, the scope picker, groups, and the
   References_View.

### Requirement 10: Bibliography export (stretch)

**User Story:** As a researcher, I want to export a formatted bibliography for
my whole archive, so that I can assemble a complete reference list for a paper.

#### Acceptance Criteria

1. WHEN the user requests a whole-archive bibliography in a specified
   Citation_Style, THE System SHALL produce a Bibliography_Block containing
   exactly one formatted citation per indexed source in that style.
2. IF the requested Citation_Style is neither MLA nor APA, THEN THE System SHALL
   reject the request, produce no Bibliography_Block, and return an error
   indication naming the supported styles.
3. THE System SHALL order the exported bibliography alphabetically by the
   leading element of each citation, using a case-insensitive and
   diacritic-insensitive comparison, with the full citation text as the
   tie-break.
4. WHERE the exported bibliography contains a source whose citation is
   incomplete, THE System SHALL include that source's best-effort citation
   (available elements only) AND SHALL mark that citation as incomplete rather
   than omitting the source.
5. WHEN no sources are indexed, THE System SHALL produce an empty
   Bibliography_Block AND return a no-sources indication.

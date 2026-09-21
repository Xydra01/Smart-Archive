# Requirements Document

## Introduction

Smart Archive currently indexes only the text it can extract from documents. Text from digital PDFs, scanned pages (where a text layer exists), saved webpages, plain text, Markdown, CSV, EPUB, and DOCX (including DOCX tables) is captured. But **visual content is ignored**: charts, graphs, diagrams, illustrations, figures, and image-based tables contribute nothing to the index, even though they often carry information that was meant for human eyes. Users storing textbooks and PDFs are silently losing that information from search and question answering.

This feature adds **ingest-time visual extraction**: during indexing, the system detects visuals in documents and uses a local vision-language model (VLM, default `qwen2.5vl:3b` via Ollama) to produce searchable, citable **text** that describes or extracts the visual's information. Because this work happens once at ingest and is persisted, query-time stays cheap — the chat LLM reasons over the indexed text alone.

The feature has four parts:

1. **Vision-based visual extraction at ingest** — opt-in, default off; extracts information from all visual types including figures/illustrations on scanned pages.
2. **Native PDF table extraction** — a fast, model-free path (pdfplumber) that captures tables with a real text layer as structured text, with the VLM as a fallback for image-only or complex tables.
3. **Ingest/query mutual-exclusion lock** — while an index job runs, query endpoints are hard-blocked (the machine cannot hold the vision model and the chat model at once on 8 GB), with a clear message; queries re-enable when the job finishes.
4. **Visual chunk metadata** — a `content_type` label on every chunk so visual-derived content is searchable and citable, and citations can indicate a figure or table and its page.

The design stays consistent with the local, single-user, 8 GB architecture: extraction runs inside the existing background index job, results are captured by the incremental manifest so unchanged files are skipped on re-ingest, and everything is configured through `backend/app/config.py` (`ARCHIVE_`-prefixed env overrides).

## Glossary

- **Archive**: The full collection of indexed sources in Smart Archive.
- **Source**: A single indexed document, identified by its `Source_Path` (path relative to `data/raw`).
- **Loader**: A per-format component that turns a Source into `LoadedSection`s (text + location + metadata).
- **LoadedSection**: A structural unit of a document (a PDF page, a DOCX heading group, a CSV row-batch, etc.) with a `location` label and metadata.
- **Chunk**: A token-sized, indexed unit derived from sections, carrying metadata (source, location, chunk index, token count, and — new — `Content_Type`).
- **Visual**: A non-text graphic within a Source — a chart, graph, diagram, illustration, figure, or image-based table — including such graphics on scanned (image-only) pages.
- **VLM**: The vision-language model (default `qwen2.5vl:3b`) invoked via Ollama with an image plus a text prompt, returning text.
- **Vision_Extraction**: The ingest-time process of detecting Visuals in a Source and using the VLM to produce searchable text from each.
- **Vision_Enabled**: The configuration flag that turns Vision_Extraction on or off (default off).
- **Content_Type**: A per-Chunk label indicating the origin of its text. Allowed values: `text`, `table`, `chart`, `figure`, `ocr`.
- **Native_Table_Extraction**: Model-free extraction of tables from digital PDFs using pdfplumber, preserving row/column structure as text.
- **Index_Job**: The existing background job (`run_index_job`) that ingests and indexes Sources, tracked by the JobManager and polled via `/api/index/status`.
- **Manifest**: The persisted record (`data/index_manifest.json`) that lets unchanged files be skipped on re-ingest, keyed by `Source_Path` with size, mtime, content hash, and chunk count.
- **Query_Endpoints**: The HTTP endpoints that serve retrieval to the user: `POST /api/search` and `POST /api/ask`.
- **Ingest_Lock**: The rule that Query_Endpoints are refused while an Index_Job is running.
- **Image_Cap**: A configurable upper bound on how many Visuals are processed per page and/or per Source, bounding worst-case ingest time.
- **API**: The FastAPI backend HTTP surface under `/api`.
- **Frontend**: The Next.js user interface.

## Requirements

### Requirement 1: Opt-in vision extraction

**User Story:** As a user, I want visual extraction to be an explicit setting, so that normal ingest stays fast and I only pay the vision cost when I choose to.

#### Acceptance Criteria

1. THE system SHALL provide a Vision_Enabled configuration flag that defaults to off.
2. WHILE Vision_Enabled is off, WHEN a Source is indexed, THE Loader SHALL extract text exactly as it does today and SHALL NOT invoke the VLM.
3. WHILE Vision_Enabled is on, WHEN a Source containing Visuals is indexed, THE system SHALL perform Vision_Extraction on those Visuals.
4. THE Vision_Enabled flag SHALL be overridable via an `ARCHIVE_`-prefixed environment variable, consistent with existing configuration.

### Requirement 2: Extract information from visuals into searchable text

**User Story:** As a user, I want charts, diagrams, illustrations, and figures turned into text, so that their information becomes searchable and the chat model can reason over it from the index.

#### Acceptance Criteria

1. WHILE Vision_Enabled is on, WHEN Vision_Extraction processes a Visual, THE system SHALL produce text derived from that Visual via the VLM.
2. WHEN Vision_Extraction produces text for a Visual, THE system SHALL create one or more Chunks from that text and add them to the index.
3. WHEN a Visual-derived Chunk is created, THE system SHALL record its originating `Source_Path` and a `location` identifying where in the Source the Visual appears (for example, its page).
4. WHEN Vision_Extraction processes a chart or graph, THE VLM prompt SHALL request the chart's textual information (such as title, axis labels, series, notable values, and overall trend).
5. WHEN Vision_Extraction processes a diagram, illustration, or figure, THE VLM prompt SHALL request a description of its content and any text or labels it contains.
6. WHERE a Source contains Visuals on scanned or image-only pages, THE system SHALL include those Visuals in Vision_Extraction while Vision_Enabled is on.

### Requirement 3: Native PDF table extraction with vision fallback

**User Story:** As a user, I want tables in my PDFs captured with their structure intact, so that searching returns correct rows and columns rather than jumbled text.

#### Acceptance Criteria

1. WHEN a digital PDF page containing a table with a text layer is indexed, THE system SHALL extract that table using Native_Table_Extraction, preserving its row and column structure in the resulting text.
2. WHEN a table is captured by Native_Table_Extraction, THE resulting Chunk SHALL have Content_Type `table`.
3. WHILE Vision_Enabled is on, WHEN a table cannot be extracted by Native_Table_Extraction because it is image-only, THE system SHALL attempt to extract it as a Visual via the VLM.
4. WHILE Vision_Enabled is off, WHEN a PDF page contains a table with a text layer, THE system SHALL still apply Native_Table_Extraction (Native_Table_Extraction does not require the VLM).
5. WHEN Native_Table_Extraction finds no tables on a page, THE system SHALL fall back to the existing page text extraction for that page's text content.

### Requirement 4: Content-type labeling of chunks

**User Story:** As a user, I want to know whether a result came from body text, a table, a chart, or a figure, so that I can trust and locate the information and scope searches by kind.

#### Acceptance Criteria

1. WHEN any Chunk is created, THE system SHALL assign it a Content_Type from the set {`text`, `table`, `chart`, `figure`, `ocr`}.
2. WHEN a Chunk is derived from ordinary extracted body text, THE system SHALL assign Content_Type `text`.
3. WHEN a Chunk is derived from Native_Table_Extraction or from a VLM-extracted table, THE system SHALL assign Content_Type `table`.
4. WHEN a Chunk is derived from VLM extraction of a chart or graph, THE system SHALL assign Content_Type `chart`.
5. WHEN a Chunk is derived from VLM extraction of a diagram, illustration, or figure, THE system SHALL assign Content_Type `figure`.
6. WHEN the API returns search results or answer citations, THE system SHALL include each result's Content_Type and its `location`.
7. WHEN a Chunk carries a Content_Type other than `text`, THE existing Chunk metadata (Source_Path, location, chunk index) SHALL still be present.

### Requirement 5: Per-visual failure isolation and timeouts

**User Story:** As a user, I want one unreadable image or a slow model call to not abort indexing of the whole document, so that ingest is robust over large, messy PDFs.

#### Acceptance Criteria

1. WHEN Vision_Extraction of a single Visual raises an error, THE system SHALL skip that Visual and continue indexing the remaining Visuals and text of the Source.
2. WHEN Vision_Extraction of a single Visual exceeds a configured per-Visual timeout, THE system SHALL abandon that Visual and continue indexing the remaining Visuals and text of the Source.
3. WHEN a Visual is skipped due to error or timeout, THE system SHALL record that the Visual was skipped in the Index_Job result for that Source.
4. WHEN Vision_Extraction of a Visual fails or times out, THE text and other successfully extracted Visuals of the Source SHALL still be indexed.

### Requirement 6: VLM availability handling

**User Story:** As a user, I want a clear outcome when the vision model isn't installed, so that I understand why visuals weren't extracted rather than getting a crash.

#### Acceptance Criteria

1. THE system SHALL provide a configurable VLM model name, defaulting to `qwen2.5vl:3b`.
2. IF Vision_Enabled is on AND the configured VLM is not available in Ollama when an Index_Job starts, THEN THE system SHALL record a clear message in the Index_Job status indicating the VLM is unavailable.
3. IF the configured VLM is unavailable, THEN THE Index_Job SHALL still complete text (and Native_Table_Extraction) indexing for its Sources.
4. WHEN the VLM is unavailable, THE system SHALL NOT abort the Index_Job.

### Requirement 7: Bounded worst-case extraction time

**User Story:** As a user, I want a limit on how many images are processed per document, so that a single huge PDF cannot make ingest run unbounded.

#### Acceptance Criteria

1. THE system SHALL provide a configurable Image_Cap bounding the number of Visuals processed per page.
2. WHEN the number of Visuals on a page exceeds the per-page Image_Cap, THE system SHALL process Visuals up to the cap and SHALL record that some Visuals on that page were not processed.
3. THE Image_Cap SHALL be overridable via an `ARCHIVE_`-prefixed environment variable.

### Requirement 8: Progress reporting for vision extraction

**User Story:** As a user, I want to see that visual extraction is happening during a long ingest, so that I know the job is progressing and not stuck.

#### Acceptance Criteria

1. WHILE Vision_Extraction is running for a Source, THE Index_Job status SHALL reflect that visual extraction is in progress for that Source.
2. WHEN the Frontend polls Index_Job status during Vision_Extraction, THE API SHALL return status information that lets the Frontend show ongoing progress.

### Requirement 9: Ingest/query mutual-exclusion (hard block)

**User Story:** As a user, I want querying to be paused while indexing runs, so that the machine never tries to hold the vision model and the chat model at the same time.

#### Acceptance Criteria

1. WHILE an Index_Job is running, WHEN a request is made to `POST /api/search`, THE API SHALL refuse the request with an error indicating that indexing is in progress and querying is paused, and SHALL NOT perform retrieval.
2. WHILE an Index_Job is running, WHEN a request is made to `POST /api/ask`, THE API SHALL refuse the request with an error indicating that indexing is in progress and querying is paused, and SHALL NOT perform retrieval or generation.
3. WHEN no Index_Job is running, THE Query_Endpoints SHALL serve requests normally.
4. WHEN an Index_Job completes (successfully or with error), THE Query_Endpoints SHALL serve requests normally without any further user action.
5. THE refusal response for a blocked query SHALL be distinguishable by the Frontend from other errors so it can show the paused-for-indexing state specifically.

### Requirement 10: Frontend reflects the ingest lock

**User Story:** As a user, I want the interface to show that querying is paused during indexing, so that I understand why I can't search and know it will resume.

#### Acceptance Criteria

1. WHILE an Index_Job is running, THE Frontend SHALL disable the Ask and Search controls.
2. WHILE an Index_Job is running, THE Frontend SHALL display a message indicating that querying is paused because indexing is in progress.
3. WHEN an Index_Job completes, THE Frontend SHALL re-enable the Ask and Search controls without requiring a page reload.
4. IF the user submits a query that is refused because indexing is in progress, THEN THE Frontend SHALL present the paused-for-indexing state rather than a generic error.

### Requirement 11: Interaction with the incremental manifest

**User Story:** As a user, I want visual extraction results to persist like everything else, so that re-running ingest skips unchanged files and I don't re-pay the vision cost unless I force it.

#### Acceptance Criteria

1. WHEN a Source has been indexed with Vision_Extraction, THE Manifest SHALL record it such that re-running the Index_Job skips that Source while it is unchanged, without re-invoking the VLM.
2. WHEN a Source's content changes (per the Manifest's size and modification-time signature), THE next Index_Job SHALL re-index that Source, including Vision_Extraction if Vision_Enabled is on.
3. WHEN a force re-index is requested, THE Index_Job SHALL re-index every Source, including Vision_Extraction if Vision_Enabled is on.
4. WHEN a Source is removed, THE system SHALL remove all of that Source's Chunks, including Visual-derived Chunks, consistent with existing source-removal behavior.

### Requirement 12: Preserve existing extraction behavior

**User Story:** As a user, I want everything that works today to keep working, so that adding vision does not regress current text search.

#### Acceptance Criteria

1. WHILE Vision_Enabled is off, WHEN any currently supported file type is indexed, THE resulting text Chunks SHALL be equivalent to those produced before this feature, each with Content_Type `text` (or `table` for DOCX tables and Native_Table_Extraction).
2. WHEN a scanned or webpage Source yields extractable text, THE system SHALL continue to index that text regardless of the Vision_Enabled setting.
3. WHEN Vision_Enabled is on but a Source contains no Visuals, THE indexed text for that Source SHALL be the same as when Vision_Enabled is off.

# Requirements Document

## Introduction

Building a Smart Archive index is expensive — embedding is CPU/GPU-bound, and vision extraction (charts, figures, tables) can take hours on a laptop. Querying, by contrast, is cheap. This feature separates those two phases across machines: a powerful machine (GPU desktop) builds the index once, exports it as a portable file, and any number of weaker machines import it to query — never re-embedding.

It also enables collaboration and incremental enrichment:
- A user can **export a curated subset** of sources (or a group) and hand the file to a collaborator for a specific project.
- A user who did a fast **text-only** index of a document can **import a vision-enabled** bundle of the same document from a GPU machine and gain the previously-missing chart/figure/table information — without duplicating the text chunks they already have.

The portable format is a **JSONL bundle** (gzip by default): a self-describing header line followed by one line per chunk, each carrying the chunk's text, embedding vector, and metadata. Import **merges by chunk id** (a content-derived hash), so importing is idempotent and additive: identical chunks dedup, new chunks (e.g. vision-derived) fill in. BM25 keyword indexing is rebuilt from imported text on the receiving machine, so the bundle stays storage-engine-independent.

Because bundles may come from other people, import treats bundle content as **untrusted data**: it is validated defensively, size-bounded, and never executed. The single hard correctness gate is the **embedding model**: a query vector must be produced by the same embedding model (and dimension) that built the stored vectors, so a bundle whose embedding model/dimension does not match the importing installation is refused.

This feature is CLI-driven (export/import scripts), consistent with the local, single-user, background-job architecture. It does not change query behavior; it only adds ways to move chunks between installations.

## Glossary

- **Archive**: The full collection of indexed content in a Smart Archive installation.
- **Chunk**: An indexed unit of text with an id, the text, an embedding vector, and metadata (source_path, content_type, location, chunk_index, etc.).
- **Chunk_Id**: The content-derived identifier of a Chunk (currently a hash of source path, location, index, and text). The unit of merge deduplication.
- **Content_Type**: A chunk label: one of `text`, `table`, `chart`, `figure`, `ocr`. Vision-derived chunks are `chart`/`figure`/`ocr` (and some `table`).
- **Embedding**: The numeric vector for a Chunk, produced by the Embedding_Model.
- **Embedding_Model**: The model that produces embeddings (default `nomic-embed-text`); its identity and vector dimension determine cross-installation compatibility.
- **Vector_Store**: The ChromaDB-backed store holding chunk vectors, text, and metadata.
- **Keyword_Index**: The in-process BM25 index, rebuilt from chunk text.
- **Manifest**: The per-file index record (`data/index_manifest.json`) used to skip unchanged files on re-ingest.
- **Bundle**: The portable export file: a JSONL document (gzip by default) with a Header line and one Chunk_Record per subsequent line.
- **Header**: The bundle's first line: a JSON object describing the bundle (format version, embedding model, embedding dimension, whether vision-derived chunks are included, chunk count, source summary, creation time, and a content checksum).
- **Chunk_Record**: One line of the bundle: a JSON object with a chunk's id, text, embedding, and metadata.
- **Export**: Producing a Bundle from an installation's Archive (all sources, selected sources, or a group).
- **Import**: Merging a Bundle's Chunk_Records into the importing installation's Archive.
- **Merge_By_Id**: Import semantics where a Chunk_Record whose Chunk_Id already exists is skipped, and one whose id is new is added.
- **Replace_Source**: An optional import mode that first removes an existing source's chunks, then adds the bundle's chunks for that source.
- **CLI**: The command-line entry points for export and import.

## Requirements

### Requirement 1: Export chunks to a portable bundle

**User Story:** As a user with a powerful machine, I want to export my indexed chunks (text and embeddings) to a file, so that I can move the index to a weaker machine without re-embedding.

#### Acceptance Criteria

1. WHEN the user runs Export, THE CLI SHALL write a Bundle containing one Chunk_Record for each exported Chunk.
2. WHEN a Chunk is exported, THE Chunk_Record SHALL include the Chunk_Id, the chunk text, the embedding vector, and the chunk metadata.
3. WHEN a Bundle is written, THE first line SHALL be a Header describing the bundle.
4. WHEN a Bundle is written without an explicit format option, THE CLI SHALL gzip-compress the Bundle by default.
5. WHERE the user requests an uncompressed Bundle, THE CLI SHALL write plain (non-gzipped) JSONL.
6. WHEN Export completes, THE CLI SHALL report the number of chunks exported and the output file path.

### Requirement 2: Selective export

**User Story:** As a user, I want to export only certain sources or a group, so that I can hand a collaborator exactly the material for a project.

#### Acceptance Criteria

1. WHERE the user specifies one or more source paths, THE Export SHALL include only Chunks whose source_path is among them.
2. WHERE the user specifies a group, THE Export SHALL include only Chunks whose source_path is a member of that group.
3. WHERE the user specifies neither sources nor a group, THE Export SHALL include all Chunks in the Archive.
4. IF the user specifies a source path or group that matches no Chunks, THEN THE CLI SHALL report that nothing matched and SHALL NOT write an empty-content bundle silently (it reports the zero result).
5. WHEN a selective Export completes, THE Header SHALL record which sources are included and their chunk counts.

### Requirement 3: Self-describing header

**User Story:** As a recipient, I want the bundle to describe itself, so that my installation can validate compatibility before importing.

#### Acceptance Criteria

1. THE Header SHALL record a bundle format version.
2. THE Header SHALL record the Embedding_Model name and the embedding vector dimension used to produce the bundle's vectors.
3. THE Header SHALL record whether the bundle includes vision-derived Chunks (content types chart, figure, or ocr).
4. THE Header SHALL record the total Chunk_Record count.
5. THE Header SHALL record a checksum over the bundle's Chunk_Records sufficient to detect truncation or corruption.
6. THE Header SHALL record the creation timestamp.

### Requirement 4: Import merges by chunk id (idempotent, additive)

**User Story:** As a user, I want importing a bundle to add its chunks without duplicating what I already have, so that re-imports and overlapping bundles are safe.

#### Acceptance Criteria

1. WHEN a Chunk_Record's Chunk_Id does not exist in the Archive, THE Import SHALL add that Chunk (text, embedding, metadata) to the Vector_Store.
2. WHEN a Chunk_Record's Chunk_Id already exists in the Archive, THE Import SHALL skip that Chunk_Record and SHALL NOT create a duplicate.
3. WHEN the same Bundle is imported twice, THE second Import SHALL add zero new Chunks.
4. WHEN Import completes, THE CLI SHALL report how many Chunks were added and how many were skipped as already present.
5. WHEN Import adds one or more Chunks, THE Keyword_Index SHALL be rebuilt so keyword search covers the imported text.

### Requirement 5: Text-only-then-vision enrichment

**User Story:** As a user who indexed a document text-only, I want to import a vision-enabled bundle of the same document, so that I gain its chart/figure/table chunks without duplicating the text chunks I already have.

#### Acceptance Criteria

1. WHEN a bundle contains vision-derived Chunks (content types chart, figure, ocr) for a source already present as text-only, THE Import SHALL add those vision-derived Chunks.
2. WHEN a bundle contains text Chunks identical (by Chunk_Id) to text Chunks already present for that source, THE Import SHALL skip those identical text Chunks.
3. WHEN such an enrichment Import completes, THE Archive SHALL contain the union of the prior text Chunks and the bundle's vision-derived Chunks for that source.

### Requirement 6: Embedding-model compatibility gate

**User Story:** As a recipient, I want an import refused when its embeddings are incompatible with mine, so that I never get silently corrupt search results.

#### Acceptance Criteria

1. IF the Header's Embedding_Model does not match the importing installation's configured Embedding_Model, THEN THE Import SHALL refuse and SHALL NOT add any Chunks.
2. IF the Header's embedding dimension does not match the importing installation's embedding dimension, THEN THE Import SHALL refuse and SHALL NOT add any Chunks.
3. WHEN an Import is refused for embedding incompatibility, THE CLI SHALL report the bundle's Embedding_Model and dimension and the installation's Embedding_Model and dimension.

### Requirement 7: Optional replace-source escape hatch

**User Story:** As a user, I want an option to fully replace a source's chunks from a bundle, so that I can overwrite a stale or partial local copy when I intend to.

#### Acceptance Criteria

1. WHERE the user requests Replace_Source for a source present in the bundle, THE Import SHALL remove the Archive's existing Chunks for that source before adding the bundle's Chunks for that source.
2. WHERE Replace_Source is not requested, THE Import SHALL use Merge_By_Id semantics.
3. WHEN Replace_Source removes existing Chunks, THE removal SHALL be limited to the sources present in the bundle and SHALL NOT affect other sources.

### Requirement 8: Untrusted bundle validation

**User Story:** As a recipient, I want malformed or hostile bundles rejected cleanly, so that importing someone else's file cannot corrupt or crash my archive.

#### Acceptance Criteria

1. IF a Bundle's first line is not a valid Header, THEN THE Import SHALL refuse the Bundle with a clear error and add no Chunks.
2. IF the Header's format version is not supported, THEN THE Import SHALL refuse the Bundle with a clear error and add no Chunks.
3. IF a Chunk_Record is not valid JSON or is missing required fields (id, text, embedding, metadata), THEN THE Import SHALL skip that record and continue, reporting the number of skipped invalid records.
4. IF a Chunk_Record's embedding vector length does not equal the Header's embedding dimension, THEN THE Import SHALL skip that record and continue, counting it as invalid.
5. THE Import SHALL NOT execute or evaluate any code or expression contained in the Bundle.
6. IF the bundle's computed checksum does not match the Header checksum, THEN THE Import SHALL warn that the bundle may be truncated or corrupt.
7. THE Import SHALL enforce a configurable maximum on individual Chunk_Record size so a single oversized record cannot exhaust memory.

### Requirement 9: Content-based, portable Chunk_Id

**User Story:** As a user merging archives from different people and machines, I want a chunk's id to depend only on its content (not on where the file happens to live or whether vision was enabled), so that identical chunks deduplicate on merge no matter their origin.

#### Acceptance Criteria

1. THE Chunk_Id SHALL be derived from stable content only: the source file name, a hash of the chunk's own text, the chunk's location label, and its content_type — and SHALL NOT include the source's relative directory path or a whole-document global chunk position.
2. WHEN a source is indexed text-only and the same source is indexed with vision enabled, THE Chunk_Ids of the text Chunks SHALL be identical between the two indexings.
3. WHEN vision-derived Chunks are added for a source, THE Chunk_Ids of that source's text Chunks SHALL NOT change compared to a text-only indexing of the source.
4. WHEN two installations index the same document content, THE Chunk_Ids of identical chunks SHALL match, independent of each installation's directory layout, so that Merge_By_Id deduplicates them.
5. IF two distinct chunks of the same source would otherwise collide on (file name, text, location, content_type), THEN THE Chunk_Id derivation SHALL disambiguate them so distinct chunks keep distinct ids within a source.

### Requirement 10: Cross-installation source identity and migration

**User Story:** As a user adopting the portable format, I want to understand how documents are identified and what changing the id scheme means for my existing index, so that I can rebuild if needed for reliable dedup.

#### Acceptance Criteria

1. THE system SHALL define source identity within Chunk_Id by the source file name (not its containing directory), so the same document content deduplicates across installations with different directory layouts.
2. WHEN a bundle is imported, THE imported Chunks' source_path metadata SHALL be preserved as recorded in the bundle so citations remain meaningful, even though the Chunk_Id itself does not depend on that path.
3. WHERE two genuinely different documents share the same file name, THE system SHALL document that content differences in their chunk text keep their Chunk_Ids distinct (identical file names dedupe only when the chunk text also matches).
4. THE system SHALL document that adopting the content-based Chunk_Id requires a one-time reindex of any archive built under the previous id scheme for its chunks to deduplicate against new-scheme bundles.

### Requirement 11: Export/import do not run during indexing

**User Story:** As a user, I want export and import to respect the same safety as querying, so that they don't run concurrently with an index job on a constrained machine.

#### Acceptance Criteria

1. WHILE an index job is running, WHEN the user attempts an Import, THE CLI SHALL refuse to run and report that indexing is in progress.
2. WHILE an index job is running, WHEN the user attempts an Export, THE CLI SHALL refuse to run and report that indexing is in progress.
3. WHEN no index job is running, THE Export and Import SHALL run normally.

### Requirement 12: Consistency after import

**User Story:** As a user, I want a queryable archive after import, so that imported chunks are immediately searchable.

#### Acceptance Criteria

1. WHEN Import adds Chunks, THE added Chunks SHALL be retrievable by semantic search using the installation's Embedding_Model.
2. WHEN Import adds Chunks, THE added Chunks SHALL be retrievable by keyword search after the Keyword_Index rebuild.
3. WHEN Import adds Chunks for a source, THE Manifest SHALL be updated so the source is recorded as present.
4. WHEN Import completes, THE Archive SHALL remain internally consistent between the Vector_Store, the Keyword_Index, and the Manifest.

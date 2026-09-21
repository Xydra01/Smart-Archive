# Requirements Document

## Introduction

Smart Archive currently searches the entire indexed archive on every Ask or Search query. As the number of indexed sources grows, users need to focus a query on a relevant subset of sources rather than the whole archive.

This feature adds two related capabilities:

1. **Per-search source selection** — for a given Ask or Search query, the user chooses which indexed sources the query runs against (a subset of all sources) instead of always searching everything.
2. **Source groups** — named, reusable collections of sources (for example, one group per project). A source can belong to zero or more groups. The user can scope a query to a group so that only that group's sources are searched, eliminating the tedium of hand-picking sources on every query.

The feature stays consistent with the existing local, single-user architecture: sources are identified by their `source_path` (path relative to `data/raw`), retrieval flows through the single `hybrid_search` fusion entry point (which fuses Chroma vector search and in-process BM25 keyword search), and persisted state is stored as JSON alongside the existing index manifest under `data/`.

## Glossary

- **Archive**: The full collection of indexed sources in the Smart Archive system.
- **Source**: A single indexed document, identified by its `Source_Path`. Reported by `/api/stats` as a key in the `sources` map.
- **Source_Path**: The unique identifier of a Source — its file path relative to `data/raw`. Stored in every chunk's Chroma metadata as `source_path`.
- **Selection**: The set of `Source_Path` values a single query is scoped to. May be an ad-hoc list of sources or the members of a Group. An empty or omitted Selection means the query runs against the whole Archive.
- **Group**: A named, reusable, persisted collection of `Source_Path` values.
- **Group_Id**: The stable unique identifier of a Group, independent of the Group's display name.
- **Group_Name**: The human-readable display name of a Group.
- **Group_Store**: The persistence component that stores Groups as JSON on disk under `data/`, analogous to the existing index Manifest.
- **Search_Service**: The backend retrieval component exposing `hybrid_search`, which fuses vector (Chroma) and keyword (BM25) results via Reciprocal Rank Fusion.
- **Vector_Store**: The ChromaDB-backed component providing semantic retrieval, supporting metadata `where` filters on `source_path`.
- **Keyword_Index**: The in-process BM25Plus component providing lexical retrieval; it returns matches across the whole corpus and must be filtered by `Source_Path`.
- **RAG_Service**: The component that produces the streamed Ask answer (summary, per-source findings, citations) from retrieved chunks.
- **Manifest**: The existing persisted JSON record of indexed files, keyed by `Source_Path`, at `data/index_manifest.json`.
- **API**: The FastAPI backend HTTP surface under `/api`.
- **Frontend**: The Next.js user interface that talks to the API.
- **Effective_Sources**: The set of `Source_Path` values that actually exist in the Vector_Store at query time and are also members of the requested Selection.

## Requirements

### Requirement 1: Scope a query to a subset of sources

**User Story:** As a user, I want to choose which indexed sources a query runs against, so that I can focus results on the sources relevant to my current task instead of the whole archive.

#### Acceptance Criteria

1. WHEN a Search request includes a non-empty Selection of `Source_Path` values, THE Search_Service SHALL restrict retrieval to chunks whose `source_path` is a member of the Effective_Sources and SHALL exclude every chunk whose `source_path` is not in the Effective_Sources.
2. WHEN an Ask request includes a non-empty Selection of `Source_Path` values, THE RAG_Service SHALL restrict retrieval to chunks whose `source_path` is a member of the Effective_Sources and SHALL exclude every chunk whose `source_path` is not in the Effective_Sources.
3. WHEN a request omits a Selection, THE Search_Service SHALL retrieve from all sources in the Archive.
4. WHEN a request includes an empty Selection, THE Search_Service SHALL retrieve from all sources in the Archive.
5. WHERE a Selection is applied, THE Search_Service SHALL apply the Selection filter to both Vector_Store retrieval and Keyword_Index retrieval before Reciprocal Rank Fusion.
6. WHEN a Selection is applied to a Search request, THE Search_Service SHALL return only chunks whose `source_path` is in the Effective_Sources.
7. WHEN a Selection contains duplicate `Source_Path` values, THE Search_Service SHALL treat the Selection as the set of its distinct `Source_Path` values and SHALL produce the same Effective_Sources as an equivalent Selection with no duplicates.
8. IF a Selection contains more than 10,000 `Source_Path` values, THEN THE API SHALL reject the request with a validation error indicating the Selection exceeds the maximum permitted size, and SHALL NOT perform retrieval.
9. WHEN a Selection is applied to an Ask request, THE RAG_Service SHALL emit citations only for chunks whose `source_path` is in the Effective_Sources.

### Requirement 2: Selection filtering across both retrievers

**User Story:** As a user, I want source scoping to apply to both semantic and keyword retrieval, so that scoped results are complete and no out-of-scope source leaks into results through either retriever.

#### Acceptance Criteria

1. WHEN a Selection is applied, THE Vector_Store SHALL filter semantic results using a metadata condition matching `source_path` against the Selection.
2. WHEN a Selection is applied, THE Keyword_Index SHALL exclude from its returned results every chunk whose `source_path` is not in the Selection.
3. WHEN a Selection is applied, THE Search_Service SHALL fuse only the filtered semantic results and the filtered keyword results.
4. WHILE a Selection excludes a source, THE Search_Service SHALL exclude every chunk belonging to that source from the fused results.

### Requirement 3: List available sources for selection

**User Story:** As a user, I want to see the list of sources I can select from, so that I can pick the ones relevant to my query.

#### Acceptance Criteria

1. WHEN the Frontend requests the selectable sources, THE API SHALL return the set of `Source_Path` values currently present in the Vector_Store.
2. THE API SHALL include, for each returned Source, its chunk count as reported by the Vector_Store.

### Requirement 4: Create a group

**User Story:** As a user, I want to create a named group, so that I can reuse a curated set of sources for a project instead of picking sources every time.

#### Acceptance Criteria

1. WHEN the user submits a request to create a Group with a Group_Name, THE Group_Store SHALL create a Group with a unique Group_Id and the provided Group_Name.
2. WHEN a Group is created, THE Group_Store SHALL persist the Group to JSON on disk under `data/`.
3. WHEN a Group is created without an initial member list, THE Group_Store SHALL create the Group with an empty set of member `Source_Path` values.
4. IF a create request provides a Group_Name that is empty after trimming surrounding whitespace, THEN THE API SHALL reject the request with a validation error.

### Requirement 5: Rename a group

**User Story:** As a user, I want to rename a group, so that its name reflects the project it represents as my work evolves.

#### Acceptance Criteria

1. WHEN the user submits a rename request for an existing Group_Id with a new Group_Name, THE Group_Store SHALL update that Group's Group_Name to the new Group_Name.
2. WHEN a Group is renamed, THE Group_Store SHALL persist the updated Group_Name to JSON on disk under `data/`.
3. WHEN a Group is renamed, THE Group_Store SHALL retain the Group's Group_Id and its member `Source_Path` values unchanged.
4. IF a rename request references a Group_Id that does not exist, THEN THE API SHALL respond with a not-found error.
5. IF a rename request provides a Group_Name that is empty after trimming surrounding whitespace, THEN THE API SHALL reject the request with a validation error.

### Requirement 6: Delete a group

**User Story:** As a user, I want to delete a group I no longer need, so that my list of groups stays relevant.

#### Acceptance Criteria

1. WHEN the user submits a delete request for an existing Group_Id, THE Group_Store SHALL remove that Group from the persisted JSON on disk.
2. WHEN a Group is deleted, THE Group_Store SHALL leave the sources that were members of the Group present in the Archive.
3. IF a delete request references a Group_Id that does not exist, THEN THE API SHALL respond with a not-found error.

### Requirement 7: Add sources to a group

**User Story:** As a user, I want to add sources to a group, so that queries scoped to the group cover those sources.

#### Acceptance Criteria

1. WHEN the user adds one or more `Source_Path` values to an existing Group, THE Group_Store SHALL add those `Source_Path` values to the Group's member set.
2. WHEN a `Source_Path` that is already a member is added to a Group, THE Group_Store SHALL keep that Group's member set unchanged for that `Source_Path`.
3. WHEN sources are added to a Group, THE Group_Store SHALL persist the updated member set to JSON on disk under `data/`.
4. IF an add-sources request references a Group_Id that does not exist, THEN THE API SHALL respond with a not-found error.

### Requirement 8: Remove sources from a group

**User Story:** As a user, I want to remove sources from a group, so that the group reflects the current set of relevant sources.

#### Acceptance Criteria

1. WHEN the user removes one or more `Source_Path` values from an existing Group, THE Group_Store SHALL remove those `Source_Path` values from the Group's member set.
2. WHEN a `Source_Path` that is not a member is removed from a Group, THE Group_Store SHALL keep that Group's member set unchanged.
3. WHEN sources are removed from a Group, THE Group_Store SHALL persist the updated member set to JSON on disk under `data/`.
4. IF a remove-sources request references a Group_Id that does not exist, THEN THE API SHALL respond with a not-found error.

### Requirement 9: List groups and their members

**User Story:** As a user, I want to view my groups and the sources in each, so that I can choose a group to scope a query and see what it contains.

#### Acceptance Criteria

1. WHEN the Frontend requests the list of Groups, THE API SHALL return every persisted Group with its Group_Id, Group_Name, and member `Source_Path` values.
2. WHEN the Frontend requests a single Group by Group_Id, THE API SHALL return that Group's Group_Id, Group_Name, and member `Source_Path` values.
3. IF a single-Group request references a Group_Id that does not exist, THEN THE API SHALL respond with a not-found error.

### Requirement 10: Scope a query to a group

**User Story:** As a user, I want to scope an Ask or Search query to a group, so that only that group's sources are searched without re-picking sources each time.

#### Acceptance Criteria

1. WHEN a Search or Ask request references an existing Group_Id and does not supply an ad-hoc Selection, THE Search_Service SHALL set the Selection to the set of `Source_Path` values of that Group's current members.
2. WHEN a Selection has been established for a Search or Ask request, THE Search_Service SHALL compute the Effective_Sources as the intersection of the Selection with the `Source_Path` values currently present in the Vector_Store, and SHALL retrieve chunks only from the Effective_Sources.
3. IF a Search or Ask request references a Group_Id that does not exist, THEN THE API SHALL reject the request with a not-found error identifying the missing Group_Id, and SHALL return no search results or answer content.
4. WHERE a Search or Ask request supplies both a Group_Id and an ad-hoc Selection, THE Search_Service SHALL use the ad-hoc Selection and SHALL ignore the Group_Id.
5. IF the Effective_Sources for a Search or Ask request is empty, THEN THE Search_Service SHALL return zero results (for Search) or an answer indicating that no sources are in scope (for Ask), and SHALL NOT retrieve from any source outside the Effective_Sources.

### Requirement 11: Empty or fully unavailable scope behavior

**User Story:** As a user, I want predictable behavior when a group is empty or none of its sources are indexed, so that I get clearly empty results instead of an accidental whole-archive search.

#### Acceptance Criteria

1. WHEN a request references a Group whose member set is empty, THE Search_Service SHALL return zero results and SHALL NOT retrieve from the whole Archive.
2. WHEN a request applies a Selection whose Effective_Sources is empty, THE Search_Service SHALL return zero results and SHALL NOT retrieve from the whole Archive.
3. WHEN an Ask request resolves to an empty Effective_Sources, THE RAG_Service SHALL emit an empty citations list and a message indicating nothing relevant was found in the selected scope.

### Requirement 12: Selection referencing removed or non-indexed sources

**User Story:** As a user, I want scoping to ignore sources that are no longer indexed, so that stale group membership does not break a query.

#### Acceptance Criteria

1. WHEN a Selection includes a `Source_Path` that is not present in the Vector_Store, THE Search_Service SHALL ignore that `Source_Path` and retrieve using the remaining Effective_Sources.
2. WHILE a Group contains a `Source_Path` that is not present in the Vector_Store, THE Group_Store SHALL retain that `Source_Path` in the Group's member set.
3. WHEN the Frontend requests a Group whose members include `Source_Path` values not present in the Vector_Store, THE API SHALL indicate which member `Source_Path` values are currently present in the Vector_Store.

### Requirement 13: Group persistence across restarts

**User Story:** As a user, I want my groups to survive application restarts, so that my project scopes are durable without re-creating them.

#### Acceptance Criteria

1. THE Group_Store SHALL persist Groups as JSON on disk under `data/`, separate from the Manifest file.
2. WHEN the backend starts, THE Group_Store SHALL load previously persisted Groups from disk.
3. WHEN the Group_Store writes Groups to disk, THE Group_Store SHALL write to a temporary file and replace the target file so that a persisted Groups file is never left partially written.
4. IF the persisted Groups file is missing or cannot be parsed as valid JSON, THEN THE Group_Store SHALL start with an empty set of Groups.

### Requirement 14: Interaction with source removal and index reset

**User Story:** As a user, I want group membership to reflect sources being removed or the index being reset, so that groups do not silently point at content that no longer exists.

#### Acceptance Criteria

1. WHEN a Source is removed from the index via the source-removal operation, THE Group_Store SHALL remove that Source's `Source_Path` from every Group's member set.
2. WHEN the index is reset, THE Group_Store SHALL remove all `Source_Path` members from every Group.
3. WHEN Group membership changes as a result of source removal or index reset, THE Group_Store SHALL persist the updated Groups to disk.
4. WHEN a Source is removed from the index, THE Group_Store SHALL retain every Group's Group_Id and Group_Name.

### Requirement 15: Frontend selection and group controls

**User Story:** As a user, I want the interface to let me pick sources or a group before querying, so that I can control query scope without editing requests by hand.

#### Acceptance Criteria

1. WHEN the user opens the query interface, THE Frontend SHALL display the selectable sources retrieved from the API.
2. WHERE the user selects one or more sources for a query, THE Frontend SHALL include those `Source_Path` values as the Selection in the subsequent Search or Ask request.
3. WHERE the user chooses a Group to scope a query, THE Frontend SHALL include that Group_Id in the subsequent Search or Ask request.
4. WHEN no source and no Group is selected, THE Frontend SHALL send the request without a Selection so that the query runs against the whole Archive.
5. THE Frontend SHALL provide controls to create a Group, rename a Group, delete a Group, and add or remove sources for a Group.

# Smart Archive — Local AI Search Engine

A fully local, AI-driven hybrid search engine over your personal document
archive. Ingests many file types, indexes them for **semantic + keyword**
retrieval, and uses a local LLM (via [Ollama](https://ollama.com)) to curate
cited answers. Nothing leaves your machine.

The default model is `llama3.2:3b`, but **any local Ollama chat model works** —
pick one that fits your hardware (see [Choosing a model](#choosing-a-model)).

## Screenshots

**Ask mode** — a synthesized, cited answer up top, followed by per-source
findings. Here the archive is answering "What is a variable?" from an indexed
1,100-page textbook:

![Ask mode: synthesized answer with inline citations and a per-source findings
breakdown](docs/screenshots/ask-view.png)

**Sources & ingest** — every answer links back to the exact source and page,
and the "Add to archive" panel handles uploads, folder import, and incremental
re-indexing:

![Sources list with page-level citations, plus the add-to-archive panel for
uploads and folder import](docs/screenshots/sources-and-ingest.png)

> Note: these screenshots predate the rename, so the header still reads
> "Archive AI Search" — the app is now titled **Smart Archive**.

## Stack

| Layer         | Choice                                                  |
| ------------- | ------------------------------------------------------- |
| LLM (default) | `llama3.2:3b` (Ollama, ~2 GB) — swappable, see below    |
| Embeddings    | `nomic-embed-text` (Ollama)                             |
| Vector store  | ChromaDB (persistent, cosine)                           |
| Keyword index | BM25Plus (rank-bm25)                                    |
| Fusion        | Reciprocal Rank Fusion (RRF)                            |
| Backend       | FastAPI + Uvicorn (Python)                              |
| Frontend      | Next.js (App Router, TypeScript)                        |

## How it works

1. **Ingest** — files in `data/raw/` are loaded per type (PDF pages, docx
   headings/tables, CSV row-batches, epub chapters, html, txt/md).
2. **Chunk & label** — token-based chunking (512 tokens, 64 overlap) with rich
   metadata: source file, type, location (page/chapter/rows), chunk index.
3. **Index** — each chunk is embedded into ChromaDB and added to a BM25 index.
4. **Search** — a query hits both indexes; results are fused with RRF so
   meaning-related chunks surface even without keyword overlap.
5. **Answer** — the top fused chunks are handed to the local LLM, which returns
   two parts: a synthesized, plain-language **summary** that aggregates across
   sources (with inline `[n]` citations), and a **per-source findings** section
   noting what each source contributes — useful for research and fact-checking.

## Prerequisites

- [Ollama](https://ollama.com) running locally
- Python 3.11+ and Node 18+

```bash
ollama pull llama3.2:3b        # default LLM (fast, ~2 GB)
ollama pull nomic-embed-text   # embeddings
```

### Choosing a model

This app is model-agnostic: it talks to Ollama, so **any local chat model you
can pull will work**. The default is `llama3.2:3b` because it gives direct,
cited answers fast on a laptop with 8 GB of RAM.

Swap the model without touching code — set an environment variable (or add it to
`backend/.env`) and restart the backend:

```bash
export ARCHIVE_LLM_MODEL="qwen3.5:4b"
ollama pull qwen3.5:4b     # make sure it's downloaded first
./run_backend.sh
```

**Realistic picks by hardware.** Sizes are the Ollama download; running a model
needs headroom *beyond* that (context window + OS + this app), so leave a few GB
free. If a model is close to your RAM ceiling, Ollama spills layers to CPU and
generation slows to a crawl.

| Model (Ollama tag)                              | ~Size  | Good for                                   | Notes |
| ----------------------------------------------- | ------ | ------------------------------------------ | ----- |
| `llama3.2:1b`                                    | ~1.3 GB| Very low RAM / older machines              | Fast; answers are basic |
| `llama3.2:3b` **(default)**                      | ~2 GB  | 8 GB RAM laptops                           | Fast, direct, cited answers |
| `qwen2.5:3b`                                      | ~2 GB  | 8 GB RAM alternative                        | Solid instruct model |
| `gemma2:2b`                                       | ~1.6 GB| 8 GB RAM alternative                        | Google's small model |
| `phi3.5:3.8b`                                     | ~2.2 GB| Reasoning on small hardware                 | Strong for its size |
| `qwen3.5:4b`                                      | ~3.4 GB| 8–16 GB RAM                                 | Reasoning model (see note below) |
| `mistral:7b`                                      | ~4.1 GB| 16 GB RAM                                   | Well-rounded general model |
| `llama3.1:8b`                                     | ~4.7 GB| 16 GB RAM                                   | Noticeably better answers |
| `qwen2.5:7b` / `qwen3.5:8b`                       | ~4.7–5 GB| 16 GB RAM                                 | Strong reasoning + long context |
| `gemma2:9b`                                       | ~5.4 GB| 16 GB+ RAM                                  | High quality |
| `MobiusDevelopment/Bonsai-27B-Q1_0-gguf:latest`  | ~4.4 GB| 16 GB+ RAM or a GPU                         | 1-bit 27B; impressive but wants headroom to be fast |
| `llama3.1:70b` / `qwen2.5:32b`                    | 20–40 GB| Workstations / big GPUs                    | Best quality; not for laptops |

> **Reasoning ("thinking") models** — e.g. the `qwen3.x` family — spend tokens
> on an internal chain-of-thought. This app disables that by default
> (`ARCHIVE_LLM_THINK=false`) so tokens go to the actual answer. If you use a
> thinking model and want to see its reasoning, set `ARCHIVE_LLM_THINK=true`.

> **Memory tip.** On 8 GB RAM, close other heavy apps (browsers, music apps)
> before loading anything bigger than ~4 GB. If answers are extremely slow,
> check `ollama ps` — if the model shows a CPU split, it doesn't fully fit in
> memory; drop to a smaller model.

The embedding model (`nomic-embed-text`) is separate from the chat model and
rarely needs changing, but you can override it with `ARCHIVE_EMBED_MODEL`. If
you change embedding models, run **Force re-index all**, since existing vectors
were built with the old one.

## Run it

From the **project root**, use the launcher scripts (they handle directories
and sanity checks for you). Run each in its own terminal:

```bash
./run_backend.sh     # starts FastAPI on http://localhost:8000
./run_frontend.sh    # starts Next.js on http://localhost:3000
```

Open http://localhost:3000. Upload documents, click index, then Ask or Search.

### First-time setup

The scripts assume the backend venv and frontend deps exist. If starting fresh:

```bash
# backend deps
python3 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements.txt

# frontend deps (run_frontend.sh also does this automatically if missing)
cd frontend && npm install && cd ..
```

### Running manually (without the scripts)

```bash
# backend — must be run from inside backend/
cd backend && ./.venv/bin/uvicorn app.main:app --port 8000

# frontend — must be run from inside frontend/
cd frontend && npm run dev
```

## Bulk ingest & incremental indexing

The archive is built to hold a lot of material without re-doing work.

- **Incremental indexing.** A manifest (`data/index_manifest.json`) records each
  indexed file's size, modification time, content hash, and chunk count.
  Re-running indexing only embeds files that are **new or changed** — unchanged
  files are skipped in milliseconds. This means restarting or re-indexing a big
  library doesn't re-embed everything.
- **Folder import.** Point the archive at an existing folder on disk and it
  copies (or hard-links) all supported files in, preserving structure:

  ```bash
  curl -X POST http://localhost:8000/api/import-folder \
    -H 'Content-Type: application/json' \
    -d '{"folder": "/Users/you/Documents/books"}'
  ```

  Or use the "Import a folder path" box in the UI. After importing, click
  **Index new / changed**.
- **Force re-index.** To re-embed everything (e.g. after changing chunk
  settings), use **Force re-index all** in the UI or `POST /api/index?force=true`.
- **Deletions** are handled automatically: files removed from `data/raw` are
  pruned from both the vector store and the manifest on the next index run.

## Portable index bundles (export / import / merge)

Indexing is the expensive part — embedding a large library can take hours. A
**portable bundle** lets you do that work **once** and share the result. Build
the index on a fast machine (or a GPU desktop), export a bundle, and hand it to
a laptop or a collaborator who can then query it **without re-embedding
anything**. Bundles also **merge**, so several people can pool their indexed
material into one archive.

A bundle is a single file containing the indexed chunks — their text, their
embedding vectors, and metadata (source, page/location, content type). It does
**not** contain your raw documents, only the derived index.

### Export

Run from inside `backend/` with the venv:

```bash
# everything in the index
./.venv/bin/python export_index.py archive.jsonl.gz

# only specific source files
./.venv/bin/python export_index.py project.jsonl.gz --sources report.pdf notes.md

# only the sources in a group
./.venv/bin/python export_index.py grp.jsonl.gz --group <group_id>

# plain JSONL instead of gzip (larger, but human-inspectable)
./.venv/bin/python export_index.py plain.jsonl --no-gzip
```

Export prints how many chunks were written, the embedding model and dimension
they were built with, whether vision-derived chunks are included, and a
per-source count.

### Import & merge

```bash
./.venv/bin/python import_index.py archive.jsonl.gz
```

Import **merges by content**: each chunk has an id derived from its content
(source file name, location, content type, and text), so importing is a
**union** —

- chunks you already have are recognized and **skipped** (no duplicates),
- chunks you don't have are **added**,
- re-importing the same bundle is safe and adds nothing (idempotent).

This makes pooling archives painless: two people export their bundles, import
each other's, and both end up with the union of the two indexes.

Use `--replace-sources` to instead **replace** your existing chunks for every
source that appears in the bundle (scoped to just those sources) before merging
— handy when a source was re-indexed with better settings and you want the
incoming version to win rather than dedup against the old one.

### Text-only → vision enrichment

Because chunk ids are content-based, a **text-only** archive and a
**vision-enabled** archive of the same documents share ids for their text
chunks but differ on the extra chunks vision produces (chart/figure/table
descriptions). So if you indexed without vision and a collaborator indexed the
same files *with* vision, importing their bundle **fills in exactly the missing
visual chunks** — the text chunks dedup, and you gain the information that was
locked in images and charts, without re-running vision yourself.

### Embedding-model compatibility (important)

Embeddings from different models live in **different vector spaces** and cannot
be mixed — doing so would silently corrupt retrieval. Import therefore
**refuses** a bundle whose embedding model or dimension doesn't match your
installation (`ARCHIVE_EMBED_MODEL`). If you need to combine archives built with
different embedding models, pick one model and re-index on both sides first.

A **checksum mismatch** (a truncated or corrupted file) only prints a warning
rather than refusing, since a partial import may still be useful; re-export if
you see it.

### Format & safety

- **Format.** gzip-compressed JSONL (`.jsonl.gz`; `--no-gzip` for plain
  `.jsonl`). The first line is a header (embedding model, dimension, chunk
  count, checksum, whether vision chunks are included, per-source counts); each
  remaining line is one chunk record. BM25 is rebuilt from the imported text on
  import, so bundles don't depend on any keyword-index internals.
- **Concurrency.** Export and import refuse to run while an indexing job is in
  progress (and vice-versa), so the index is never read or written half-formed.
- **Untrusted input.** Import validates every record and enforces size caps,
  skipping malformed lines (and reporting the count) rather than trusting a
  bundle blindly.

### One-time re-index to adopt content-based ids

Content-based chunk ids are what make cross-machine dedup and merging work. If
your archive was indexed by an **older** version of Smart Archive (which used
path-based ids), run **Force re-index all** once (UI, or
`POST /api/index?force=true`) so your chunks pick up the new ids before you
export or import bundles. This is a one-time step.

## Configuration

All tunables live in `backend/app/config.py` and can be overridden with
`ARCHIVE_`-prefixed environment variables (or a `backend/.env` — see
`backend/.env.example`). Key ones:

**Models**
- `ARCHIVE_LLM_MODEL` — the Ollama chat model (default `llama3.2:3b`)
- `ARCHIVE_EMBED_MODEL` — the embedding model (default `nomic-embed-text`)
- `ARCHIVE_LLM_THINK` — allow reasoning models to emit their chain-of-thought
  (default `false`)
- `ARCHIVE_LLM_NUM_CTX` — context window (default `4096`; kept modest for 8 GB RAM)
- `ARCHIVE_LLM_NUM_PREDICT` — max answer length in tokens
- `ARCHIVE_OLLAMA_HOST` — where Ollama is listening (default
  `http://localhost:11434`)

**Chunking & retrieval**
- `ARCHIVE_CHUNK_TOKENS`, `ARCHIVE_CHUNK_OVERLAP_TOKENS`
- `ARCHIVE_FUSION_TOP_K` — chunks handed to the LLM after fusion
- `ARCHIVE_SEMANTIC_TOP_K`, `ARCHIVE_KEYWORD_TOP_K`

**Portable bundles** (see [Portable index bundles](#portable-index-bundles-export--import--merge))
- `ARCHIVE_BUNDLE_DEFAULT_GZIP` — gzip exported bundles by default (default `true`)
- `ARCHIVE_BUNDLE_MAX_RECORD_BYTES` — per-record size cap on import, in bytes
  (default `16777216`); guards against malformed/oversized input

## Notes

- This app is model-agnostic — any local Ollama chat model works. See
  [Choosing a model](#choosing-a-model) for picks by hardware.
- On 8 GB RAM, smaller models (≤4 GB) run best; close other heavy apps for
  headroom.
- `data/` (your documents, the vector store, and the index manifest) is
  git-ignored by design, so cloning this repo won't carry your archive with it.

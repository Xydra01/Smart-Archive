# Smart Archive — Local AI Search Engine

A fully local, AI-driven hybrid search engine over your personal document
archive. Ingests many file types, indexes them for **semantic + keyword**
retrieval, and uses a local 1-bit **Bonsai 27B** model (via Ollama) to curate
cited answers. Nothing leaves your machine.

## Stack

| Layer         | Choice                                                  |
| ------------- | ------------------------------------------------------- |
| LLM (default) | `llama3.2:3b` (Ollama, ~2 GB) — fast, direct answers    |
| Embeddings    | `nomic-embed-text` (Ollama)                             |
| Vector store  | ChromaDB (persistent, cosine)                           |
| Keyword index | BM25 (rank-bm25)                                        |
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
5. **Answer** — the top fused chunks are handed to Bonsai 27B, which writes a
   concise answer citing sources inline as `[n]`.

## Prerequisites

- [Ollama](https://ollama.com) running locally
- Python 3.11+ and Node 18+

```bash
ollama pull llama3.2:3b        # default LLM (fast, ~2 GB)
ollama pull nomic-embed-text   # embeddings
```

### Choosing a model

The default is `llama3.2:3b` because it gives direct, cited answers quickly on a
laptop with 8 GB of RAM. You can swap the LLM via `ARCHIVE_LLM_MODEL`:

- `qwen3.5:4b` — a reasoning model; capable but slower and burns tokens on an
  internal chain-of-thought.
- `MobiusDevelopment/Bonsai-27B-Q1_0-gguf:latest` — a 1-bit 27B model. It's
  impressive tech (~3.9 GB on disk), but on an 8 GB machine it spills to CPU and
  is too slow for an interactive UX. Use it on a machine with more RAM/VRAM.

> Memory tip: a 27B model needs headroom beyond its file size. On 8 GB RAM,
> close other heavy apps (browsers, Spotify) before loading larger models, or
> stick with the 3B/4B options.

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

## Configuration

All tunables live in `backend/app/config.py` and can be overridden with
`ARCHIVE_`-prefixed environment variables (or a `backend/.env`). Key ones:

- `ARCHIVE_CHUNK_TOKENS`, `ARCHIVE_CHUNK_OVERLAP_TOKENS`
- `ARCHIVE_FUSION_TOP_K` (chunks handed to the LLM)
- `ARCHIVE_LLM_NUM_CTX` (context window; kept modest for 8 GB RAM)

## Notes

- On 8 GB RAM the model runs but leaves little headroom; close other heavy apps.
- `data/` (documents + indexes) is git-ignored by design.

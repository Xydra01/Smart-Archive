"""ChromaDB-backed vector store for semantic retrieval.

Uses a persistent client so the index survives restarts. Embeddings are
supplied explicitly (computed via Ollama) rather than letting Chroma pick a
default embedder, so the whole pipeline stays local and consistent.
"""

from __future__ import annotations

import chromadb
from chromadb.config import Settings as ChromaSettings

from ..config import settings
from ..ingestion.chunker import Chunk
from ..llm import ollama_client


class VectorStore:
    def __init__(self) -> None:
        self._client = chromadb.PersistentClient(
            path=str(settings.chroma_dir),
            settings=ChromaSettings(anonymized_telemetry=False, allow_reset=True),
        )
        self._collection = self._client.get_or_create_collection(
            name=settings.collection_name,
            metadata={"hnsw:space": "cosine"},
        )

    # -- write -------------------------------------------------------------
    def add_chunks(
        self, chunks: list[Chunk], progress=None, batch_size: int = 64
    ) -> int:
        """Embed and upsert chunks in batches.

        ``progress`` is an optional callable(done, total) invoked after each
        batch so long-running indexing jobs can report status.
        """
        if not chunks:
            return 0
        total = len(chunks)
        done = 0
        for start in range(0, total, batch_size):
            batch = chunks[start : start + batch_size]
            embeddings = ollama_client.embed_texts([c.text for c in batch])
            self._collection.upsert(
                ids=[c.id for c in batch],
                documents=[c.text for c in batch],
                metadatas=[c.to_metadata() for c in batch],
                embeddings=embeddings,
            )
            done += len(batch)
            if progress:
                progress(done, total)
        return total

    def add_precomputed(
        self,
        ids: list[str],
        texts: list[str],
        metadatas: list[dict],
        embeddings: list[list[float]],
        batch_size: int = 256,
    ) -> int:
        """Upsert chunks whose embeddings already exist (import path).

        Unlike ``add_chunks`` this never calls the embedder — the vectors come
        from an imported bundle. Batched to bound peak memory on large imports.
        """
        n = len(ids)
        if n == 0:
            return 0
        for start in range(0, n, batch_size):
            end = start + batch_size
            self._collection.upsert(
                ids=ids[start:end],
                documents=texts[start:end],
                metadatas=metadatas[start:end],
                embeddings=embeddings[start:end],
            )
        return n

    def delete_by_source(self, source_path: str) -> None:
        self._collection.delete(where={"source_path": source_path})

    def reset(self) -> None:
        self._client.delete_collection(settings.collection_name)
        self._collection = self._client.get_or_create_collection(
            name=settings.collection_name,
            metadata={"hnsw:space": "cosine"},
        )

    # -- read --------------------------------------------------------------
    def query(
        self, query_text: str, top_k: int, where_sources: set[str] | None = None
    ) -> list[dict]:
        if self.count() == 0:
            return []
        query_vec = ollama_client.embed_query(query_text)
        # Filter server-side to the in-scope source paths when a selection is
        # supplied; ``None`` preserves the unfiltered whole-archive behavior.
        where = (
            {"source_path": {"$in": sorted(where_sources)}} if where_sources else None
        )
        res = self._collection.query(
            query_embeddings=[query_vec],
            n_results=min(top_k, self.count()),
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        out: list[dict] = []
        ids = res.get("ids", [[]])[0]
        docs = res.get("documents", [[]])[0]
        metas = res.get("metadatas", [[]])[0]
        dists = res.get("distances", [[]])[0]
        for cid, doc, meta, dist in zip(ids, docs, metas, dists):
            out.append(
                {
                    "id": cid,
                    "text": doc,
                    "metadata": meta,
                    # cosine distance -> similarity score in [0, 1]
                    "score": 1.0 - float(dist),
                }
            )
        return out

    def all_documents(self) -> list[dict]:
        """Return every stored chunk (id, text, metadata) for BM25 rebuilds."""
        res = self._collection.get(include=["documents", "metadatas"])
        out: list[dict] = []
        for cid, doc, meta in zip(
            res.get("ids", []), res.get("documents", []), res.get("metadatas", [])
        ):
            out.append({"id": cid, "text": doc, "metadata": meta})
        return out

    def get_all_ids(self) -> set[str]:
        """All chunk ids currently in the collection (for merge dedup on import)."""
        res = self._collection.get(include=[])  # ids are always returned
        return set(res.get("ids", []))

    def iter_export(self, source_paths: set[str] | None = None):
        """Yield (id, text, metadata, embedding) for export.

        When ``source_paths`` is given, only chunks whose ``source_path``
        metadata is in that set are yielded; otherwise every chunk is yielded.
        Embeddings are explicitly included so a bundle can be built without
        re-embedding on the receiving machine.
        """
        where = {"source_path": {"$in": sorted(source_paths)}} if source_paths else None
        res = self._collection.get(
            where=where, include=["documents", "metadatas", "embeddings"]
        )
        # Chroma returns embeddings as a numpy array, so avoid truthiness checks
        # ("or []") which raise on arrays; test for None explicitly.
        ids = res.get("ids")
        ids = list(ids) if ids is not None else []
        docs = res.get("documents")
        docs = list(docs) if docs is not None else []
        metas = res.get("metadatas")
        metas = list(metas) if metas is not None else []
        embs = res.get("embeddings")
        embs = list(embs) if embs is not None else []
        for i, cid in enumerate(ids):
            emb = embs[i] if i < len(embs) else None
            if emb is None:
                continue
            yield (
                cid,
                docs[i] if i < len(docs) else "",
                metas[i] if i < len(metas) else {},
                [float(x) for x in emb],
            )

    def count(self) -> int:
        return self._collection.count()

    def sources(self) -> dict[str, int]:
        """Map each source file path to its chunk count."""
        res = self._collection.get(include=["metadatas"])
        counts: dict[str, int] = {}
        for meta in res.get("metadatas", []):
            sp = meta.get("source_path", "unknown")
            counts[sp] = counts.get(sp, 0) + 1
        return counts


_store: VectorStore | None = None


def get_store() -> VectorStore:
    global _store
    if _store is None:
        _store = VectorStore()
    return _store

"""Retrieval-augmented generation: curate a cited answer from search hits.

The LLM's job here is curation, not recall: it is given the top fused chunks
and must answer *only* from them, citing sources by an index that maps back to
concrete files and locations. This keeps answers grounded and navigable.
"""
from __future__ import annotations

from typing import Iterator

from ..search.hybrid import hybrid_search
from . import ollama_client

SYSTEM_PROMPT = (
    "You are the curator of a personal knowledge archive. Answer the user's "
    "question using ONLY the numbered sources provided. Cite the sources you "
    "use inline with bracketed numbers like [1] or [2]. If the sources do not "
    "contain the answer, say so plainly instead of guessing. Be concise and "
    "accurate."
)


def _format_context(hits: list[dict]) -> tuple[str, list[dict]]:
    """Build the numbered context block and a parallel citation list."""
    lines: list[str] = []
    citations: list[dict] = []
    for i, hit in enumerate(hits, start=1):
        md = hit.get("metadata", {})
        source = md.get("source_file", "unknown")
        location = md.get("location", "")
        label = f"{source} ({location})" if location else source
        lines.append(f"[{i}] {label}\n{hit.get('text', '').strip()}")
        citations.append(
            {
                "index": i,
                "source_file": source,
                "source_path": md.get("source_path"),
                "location": location,
                "file_type": md.get("file_type"),
                "matched_by": hit.get("matched_by", []),
                "rrf_score": hit.get("rrf_score"),
            }
        )
    return "\n\n".join(lines), citations


def _build_prompt(question: str, context: str) -> str:
    return (
        f"Sources:\n{context}\n\n"
        f"Question: {question}\n\n"
        "Answer (cite sources inline with [n]):"
    )


def answer(question: str, top_k: int | None = None) -> dict:
    """Non-streaming: retrieve, curate, and return answer + citations."""
    hits = hybrid_search(question, top_k=top_k)
    if not hits:
        return {
            "answer": "I couldn't find anything relevant in the archive for that query.",
            "citations": [],
            "hits": [],
        }
    context, citations = _format_context(hits)
    prompt = _build_prompt(question, context)
    text = ollama_client.generate(prompt, system=SYSTEM_PROMPT)
    return {"answer": text, "citations": citations, "hits": hits}


def answer_stream(question: str, top_k: int | None = None):
    """Streaming variant: yields ('citations', data) once, then ('token', str)."""
    hits = hybrid_search(question, top_k=top_k)
    if not hits:
        yield ("citations", [])
        yield ("token", "I couldn't find anything relevant in the archive for that query.")
        return
    context, citations = _format_context(hits)
    yield ("citations", citations)
    prompt = _build_prompt(question, context)
    for delta in ollama_client.generate_stream(prompt, system=SYSTEM_PROMPT):
        yield ("token", delta)

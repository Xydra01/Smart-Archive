"""Retrieval-augmented generation: curate a cited answer from search hits.

Produces a two-part response:
  1. A synthesized, humanized SUMMARY that aggregates across sources.
  2. A PER-SOURCE breakdown of what each relevant source contributes.

Both are grounded in the retrieved chunks and cite sources by an index that
maps back to concrete files and locations, so the answer stays useful for
research while still reading like a real answer.
"""

from __future__ import annotations

from typing import Iterator

from ..search.hybrid import hybrid_search
from . import ollama_client

# --- Pass 1: synthesized summary ------------------------------------------
SUMMARY_SYSTEM = (
    "You are the curator of a personal knowledge archive. Write a clear, "
    "humanized answer to the user's question by SYNTHESIZING across the "
    "numbered sources provided.\n"
    "\n"
    "Rules:\n"
    "- Answer in your own words as one coherent explanation. Do NOT go through "
    "the sources one at a time.\n"
    "- Combine partial or differently-worded descriptions into a single unified "
    "answer. Sources describing different facets of the same idea should be "
    "merged, not treated as conflicting.\n"
    "- Cite inline with [n] (e.g. [1], [3]) to mark which sources back each "
    "claim. Several citations per sentence are fine.\n"
    "- The sources are excerpts and may be rough; reconcile them into the best "
    "answer they collectively support. Do NOT refuse just because no single "
    "source says everything.\n"
    "- Ground every claim in the sources; don't invent facts.\n"
    "- Write plain prose (a short paragraph). Do not use headings or the "
    "literal token '[n]'."
)

# --- Pass 2: per-source findings ------------------------------------------
PERSOURCE_SYSTEM = (
    "You are analyzing sources for a research answer. For each numbered source "
    "that is RELEVANT to the question, write one line: the citation number "
    "followed by a concise note of what that specific source contributes to "
    "answering the question.\n"
    "\n"
    "Rules:\n"
    "- Format each line exactly as: [n] <what this source says about the "
    "question>.\n"
    "- Include only sources that actually bear on the question; skip irrelevant "
    "ones.\n"
    "- Keep each line to one sentence. Stay faithful to the source text.\n"
    "- Output only the lines, nothing else."
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


def _summary_prompt(question: str, context: str) -> str:
    return (
        f"Sources:\n{context}\n\n"
        f"Question: {question}\n\n"
        "Write a synthesized, plain-language answer (a short paragraph) that "
        "combines what the sources collectively say, citing them inline with "
        "[n]. Begin with the direct answer.\n\n"
        "Answer:"
    )


def _persource_prompt(question: str, context: str) -> str:
    return (
        f"Sources:\n{context}\n\n"
        f"Question: {question}\n\n"
        "List, one per line as '[n] ...', what each relevant source contributes "
        "to answering the question. Skip irrelevant sources.\n\n"
        "Findings:"
    )


def _empty_response() -> dict:
    return {
        "summary": "I couldn't find anything relevant in the archive for that query.",
        "per_source": "",
        "citations": [],
        "hits": [],
    }


def answer(question: str, top_k: int | None = None) -> dict:
    """Non-streaming: retrieve and produce summary + per-source + citations."""
    hits = hybrid_search(question, top_k=top_k)
    if not hits:
        return _empty_response()
    context, citations = _format_context(hits)
    summary = ollama_client.generate(
        _summary_prompt(question, context), system=SUMMARY_SYSTEM
    )
    per_source = ollama_client.generate(
        _persource_prompt(question, context), system=PERSOURCE_SYSTEM
    )
    return {
        "summary": summary.strip(),
        "per_source": per_source.strip(),
        "citations": citations,
        "hits": hits,
    }


def answer_stream(question: str, top_k: int | None = None):
    """Streaming variant.

    Emits, in order:
      ('citations', [...])           once
      ('section', 'summary')         marker
      ('token', str) ...             summary tokens
      ('section', 'per_source')      marker
      ('token', str) ...             per-source tokens
    """
    hits = hybrid_search(question, top_k=top_k)
    if not hits:
        yield ("citations", [])
        yield ("section", "summary")
        yield (
            "token",
            "I couldn't find anything relevant in the archive for that query.",
        )
        return

    context, citations = _format_context(hits)
    yield ("citations", citations)

    # Pass 1: synthesized summary
    yield ("section", "summary")
    for delta in ollama_client.generate_stream(
        _summary_prompt(question, context), system=SUMMARY_SYSTEM
    ):
        yield ("token", delta)

    # Pass 2: per-source findings
    yield ("section", "per_source")
    for delta in ollama_client.generate_stream(
        _persource_prompt(question, context), system=PERSOURCE_SYSTEM
    ):
        yield ("token", delta)

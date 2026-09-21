"""Tests for the Smart Archive "vision-ingest" feature.

Feature: vision-ingest. Covers the ingest/query mutual-exclusion lock and the
content_type surfacing that the vision pipeline relies on. The design's
correctness properties exercised here are:

  * Property 8  — query is blocked exactly while indexing (409 iff running).
  * Property 9  — a blocked query does no retrieval/generation work.
  * Property 10 — the manifest skip avoids re-extraction (no VLM on re-run).
  * Property 11 — content_type + location surface in results and citations.

Isolation strategy (the app must never touch real data / Chroma / Ollama):

  * ``/api/search`` does ``from .search.hybrid import hybrid_search`` *inside*
    the handler, so we patch ``app.search.hybrid.hybrid_search`` with a fake.
  * ``/api/ask`` streams via ``rag.answer_stream`` (``rag`` imported into main),
    so we patch ``app.llm.rag.answer_stream``.
  * The lock reads ``app.main.job_manager`` — specifically
    ``job_manager.is_indexing()``, which returns ``latest().status == "running"``.
    We drive it by patching ``job_manager.latest`` to return a fake job with a
    generated status, so no real job or thread ever runs.
  * P10 drives ``indexer.run_index_job`` against fake manifest/store/keyword
    collaborators and a spy ``vision.extract_visuals``; the real Chroma,
    keyword index, and VLM are never touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings as hyp_settings
from hypothesis import strategies as st

import app.main as main
import app.search.hybrid as hybrid_mod
import app.llm.rag as rag_mod
import app.indexing.indexer as indexer_mod
from app.indexing.jobs import Job


# ---------------------------------------------------------------------------
# Fakes / fixtures
# ---------------------------------------------------------------------------
class FakeJob:
    """Minimal stand-in for a Job: is_indexing() only reads ``.status``."""

    def __init__(self, status):
        self.status = status
        # latest() picks the max by started_at; give it a stable value.
        self.started_at = 0.0


class HybridSpy:
    """A fake hybrid_search that records calls and returns canned hits."""

    def __init__(self, hits=None):
        self.calls = 0
        self.hits = hits if hits is not None else []

    def __call__(self, query, top_k=None, scope=None):
        self.calls += 1
        return list(self.hits)


class AnswerStreamSpy:
    """A fake rag.answer_stream that records calls and yields simple events."""

    def __init__(self):
        self.calls = 0

    def __call__(self, question, top_k=None, scope=None):
        self.calls += 1
        # A generator that yields a couple of harmless events (mimics the real
        # empty-hits shape) so the streaming response completes cleanly.
        def _gen():
            yield ("citations", [])
            yield ("section", "summary")
            yield ("token", "")
        return _gen()


@pytest.fixture
def client() -> TestClient:
    return TestClient(main.app)


def _set_latest_status(monkeypatch, status):
    """Point job_manager.latest() at a fake job with ``status`` (or None)."""
    job = FakeJob(status) if status is not None else None
    monkeypatch.setattr(main.job_manager, "latest", lambda: job)


# The set of statuses the design enumerates, plus None (no job yet).
JOB_STATUSES = ["pending", "running", "done", "error", None]


# ===========================================================================
# Property 8 — query is blocked exactly while indexing (409 iff running)
# ===========================================================================
@hyp_settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(status=st.sampled_from(JOB_STATUSES))
def test_query_blocked_iff_indexing(status, client, monkeypatch):
    """Feature: vision-ingest, Property 8: Query is blocked exactly while
    indexing.

    ``/api/search`` and ``/api/ask`` return HTTP 409 with detail code
    ``indexing_in_progress`` if and only if the latest job status is
    ``running``. For every other status the endpoints serve normally (never the
    indexing lock)."""
    _set_latest_status(monkeypatch, status)
    # Make the non-blocked path return cleanly without touching real code.
    monkeypatch.setattr(hybrid_mod, "hybrid_search", HybridSpy(hits=[]))
    monkeypatch.setattr(rag_mod, "answer_stream", AnswerStreamSpy())

    should_block = status == "running"

    search_resp = client.post("/api/search", json={"query": "q"})
    ask_resp = client.post("/api/ask", json={"question": "q"})

    if should_block:
        assert search_resp.status_code == 409
        assert search_resp.json()["detail"]["code"] == "indexing_in_progress"
        assert ask_resp.status_code == 409
        assert ask_resp.json()["detail"]["code"] == "indexing_in_progress"
    else:
        # Must not be the indexing lock; a clean serve is 200 here.
        assert search_resp.status_code != 409
        assert ask_resp.status_code != 409
        assert search_resp.status_code == 200
        assert ask_resp.status_code == 200


# ===========================================================================
# Property 9 — a blocked query does no retrieval/generation work
# ===========================================================================
def test_blocked_query_does_no_work(client, monkeypatch):
    """Feature: vision-ingest, Property 9: A blocked query does no work.

    While a job is running, the guard raises before any retrieval or
    generation. We install spies on ``hybrid_search`` and ``answer_stream`` and
    assert both endpoints 409 and neither spy is ever called."""
    _set_latest_status(monkeypatch, "running")
    search_spy = HybridSpy(hits=[{"text": "x", "metadata": {}}])
    ask_spy = AnswerStreamSpy()
    monkeypatch.setattr(hybrid_mod, "hybrid_search", search_spy)
    monkeypatch.setattr(rag_mod, "answer_stream", ask_spy)

    search_resp = client.post("/api/search", json={"query": "q"})
    ask_resp = client.post("/api/ask", json={"question": "q"})

    assert search_resp.status_code == 409
    assert search_resp.json()["detail"]["code"] == "indexing_in_progress"
    assert ask_resp.status_code == 409
    assert ask_resp.json()["detail"]["code"] == "indexing_in_progress"

    # No retrieval, no generation happened while blocked.
    assert search_spy.calls == 0
    assert ask_spy.calls == 0


# ===========================================================================
# Property 11 — content_type + location surface in results and citations
# ===========================================================================
def _fake_hits():
    """Fake hybrid_search hits, each carrying content_type + location metadata."""
    return [
        {
            "text": "A bar chart of quarterly revenue.",
            "matched_by": ["semantic"],
            "rrf_score": 0.9,
            "metadata": {
                "source_file": "report.pdf",
                "source_path": "report.pdf",
                "file_type": ".pdf",
                "location": "p. 3 (figure 1)",
                "content_type": "chart",
            },
        },
        {
            "text": "col a | col b\n1 | 2",
            "matched_by": ["keyword"],
            "rrf_score": 0.5,
            "metadata": {
                "source_file": "data.docx",
                "source_path": "data.docx",
                "file_type": ".docx",
                "location": "table 2",
                "content_type": "table",
            },
        },
    ]


def test_search_results_carry_content_type(client, monkeypatch):
    """Feature: vision-ingest, Property 11: Content_type surfaces in results.

    With indexing idle, ``/api/search`` returns each hit's metadata verbatim, so
    every result's metadata includes ``content_type`` and ``location``."""
    _set_latest_status(monkeypatch, None)
    monkeypatch.setattr(hybrid_mod, "hybrid_search", HybridSpy(hits=_fake_hits()))

    resp = client.post("/api/search", json={"query": "revenue"})
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert len(results) == 2
    for r in results:
        md = r["metadata"]
        assert "content_type" in md
        assert "location" in md
    assert {r["metadata"]["content_type"] for r in results} == {"chart", "table"}


def test_ask_citations_carry_content_type():
    """Feature: vision-ingest, Property 11: Content_type surfaces in citations.

    Unit-test ``rag._format_context`` directly: given hits whose metadata carry
    content_type/location, every returned citation dict includes both."""
    _, citations = rag_mod._format_context(_fake_hits())
    assert len(citations) == 2
    for cite in citations:
        assert "content_type" in cite
        assert "location" in cite
    assert citations[0]["content_type"] == "chart"
    assert citations[0]["location"] == "p. 3 (figure 1)"
    assert citations[1]["content_type"] == "table"
    assert citations[1]["location"] == "table 2"


# ===========================================================================
# Property 10 — the manifest skip avoids re-extraction (no VLM on re-run)
# ===========================================================================
class VisionSpy:
    """A fake vision.extract_visuals recording how many times it ran."""

    def __init__(self):
        self.calls = 0

    def __call__(self, path, job=None):
        self.calls += 1
        return [], 0  # (sections, skipped) — no visuals, none skipped


class FakeStore:
    """Minimal VectorStore stand-in for the indexer control flow."""

    def __init__(self):
        self.added = 0

    def delete_by_source(self, rel):
        pass

    def add_chunks(self, chunks, progress=None):
        n = len(list(chunks))
        self.added += n
        return n

    def all_documents(self):
        return []

    def count(self):
        return self.added


class FakeManifest:
    """A manifest whose is_unchanged is scripted per call.

    ``unchanged_sequence`` is a list of booleans consumed in order by
    is_unchanged (e.g. [False, True] => index first run, skip second).
    ``record`` and ``prune_missing`` are no-ops that satisfy the control flow.
    """

    def __init__(self, unchanged_sequence):
        self._seq = list(unchanged_sequence)
        self._i = 0
        self.recorded = []

    def prune_missing(self, existing_rels):
        return []

    def is_unchanged(self, path, rel):
        val = self._seq[self._i] if self._i < len(self._seq) else True
        self._i += 1
        return val

    def record(self, path, rel, chunk_count):
        self.recorded.append(rel)

    def entries(self):
        return {}


@pytest.fixture
def raw_txt(tmp_path: Path, monkeypatch) -> Path:
    """A tmp raw dir with one tiny .txt file; settings pointed at tmp dirs."""
    raw = tmp_path / "raw"
    raw.mkdir()
    f = raw / "note.txt"
    f.write_text("hello world, a small note.", encoding="utf-8")
    monkeypatch.setattr(indexer_mod.settings, "raw_dir", raw)
    monkeypatch.setattr(indexer_mod.settings, "chroma_dir", tmp_path / "chroma")
    monkeypatch.setattr(indexer_mod.settings, "keyword_dir", tmp_path / "keyword")
    monkeypatch.setattr(indexer_mod.settings, "vision_enabled", True)
    return f


def test_index_file_runs_vision_when_enabled(raw_txt, monkeypatch):
    """Feature: vision-ingest, Property 10 (extract level): with vision enabled,
    ``index_file`` invokes the vision extractor for the file.

    This anchors the "re-extraction" the skip must avoid: a real (re)index runs
    the VLM path, so skipping an unchanged file is what saves that work."""
    spy = VisionSpy()
    store = FakeStore()
    manifest = FakeManifest([False])
    monkeypatch.setattr(indexer_mod.vision, "extract_visuals", spy)
    monkeypatch.setattr(indexer_mod, "get_store", lambda: store)
    monkeypatch.setattr(indexer_mod, "get_manifest", lambda: manifest)
    monkeypatch.setattr(indexer_mod, "_rebuild_keyword_index", lambda: None)

    indexer_mod.index_file(raw_txt, rebuild_keyword=False, vision_enabled=True)

    assert spy.calls == 1  # vision ran on this (re)index


def test_manifest_skip_avoids_reextraction(raw_txt, monkeypatch):
    """Feature: vision-ingest, Property 10: The manifest skip avoids
    re-extraction.

    Run ``run_index_job`` twice on the same unchanged file. The scripted
    manifest reports the file changed on the first run (indexed, vision runs)
    and unchanged on the second (skipped). After both runs the vision spy has
    been called exactly once — the unchanged re-run does no loader/VLM work."""
    spy = VisionSpy()
    store = FakeStore()
    # First run: is_unchanged False (index it). Second run: True (skip it).
    manifest = FakeManifest([False, True])

    monkeypatch.setattr(indexer_mod.vision, "extract_visuals", spy)
    monkeypatch.setattr(indexer_mod, "get_store", lambda: store)
    monkeypatch.setattr(indexer_mod, "get_manifest", lambda: manifest)
    monkeypatch.setattr(indexer_mod, "_rebuild_keyword_index", lambda: None)
    # Vision is enabled and the VLM reports available, so run_index_job turns
    # vision on for the job.
    monkeypatch.setattr(
        indexer_mod.ollama_client, "vision_available", lambda: True
    )

    job1 = Job(id="j1", kind="index")
    indexer_mod.run_index_job(job1)
    assert job1.processed_files == 1
    assert job1.skipped_files == 0
    assert spy.calls == 1  # first run extracted

    job2 = Job(id="j2", kind="index")
    indexer_mod.run_index_job(job2)
    assert job2.processed_files == 0
    assert job2.skipped_files == 1
    # No re-extraction on the unchanged re-run.
    assert spy.calls == 1

"""In-process background job tracking for long-running indexing.

Indexing a large document (e.g. a 1000-page textbook -> thousands of chunks)
takes longer than a single HTTP request should block for. The API starts a job
in a background thread and returns immediately; the client polls
``/api/index/status`` for progress.

This is intentionally simple (single process, in-memory). It's the right fit
for a local single-user app. For multi-user or restart-durable jobs you'd move
this to a real task queue.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class Job:
    id: str
    kind: str                      # e.g. "index"
    status: str = "pending"        # pending | running | done | error
    total_files: int = 0
    processed_files: int = 0
    current_file: Optional[str] = None
    total_chunks: int = 0          # chunks embedded so far (running total)
    file_chunk_done: int = 0       # progress within the current file
    file_chunk_total: int = 0
    message: str = ""
    error: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    results: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["elapsed_s"] = round((self.finished_at or time.time()) - self.started_at, 1)
        return d


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self, kind: str) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind)
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def latest(self) -> Optional[Job]:
        with self._lock:
            if not self._jobs:
                return None
            return max(self._jobs.values(), key=lambda j: j.started_at)

    def run_in_thread(self, job: Job, target) -> None:
        """Run ``target(job)`` in a daemon thread, updating status/errors."""

        def _wrapper():
            job.status = "running"
            try:
                target(job)
                job.status = "done"
            except Exception as e:  # surface the error to the client
                job.status = "error"
                job.error = str(e)
            finally:
                job.finished_at = time.time()

        threading.Thread(target=_wrapper, daemon=True).start()


manager = JobManager()

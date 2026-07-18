"""Job dataclass + in-memory JobStore (thread-safe)."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class Job:
    id: str
    url: str
    option_id: str
    platform: str
    dir: str
    title: str | None = None
    status: str = "queued"  # queued | downloading | processing | done | error
    progress: float = 0.0
    downloaded_bytes: int | None = None
    total_bytes: int | None = None
    speed: float | None = None
    eta: int | None = None
    filepath: str | None = None
    filename: str | None = None
    filesize: int | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    served_at: float | None = None
    # Playlist / multi-item batch (None for single-file jobs)
    batch_total: int | None = None
    batch_done: int | None = None
    batch_failed: int | None = None
    batch_zip: bool | None = None

    def to_public(self) -> dict:
        out = {
            "job_id": self.id,
            "status": self.status,
            "progress": round(self.progress, 1),
            "downloaded_bytes": self.downloaded_bytes,
            "total_bytes": self.total_bytes,
            "speed": self.speed,
            "eta": self.eta,
            "filename": self.filename,
            "filesize": self.filesize,
            "error": self.error,
        }
        if self.batch_total is not None:
            out["batch"] = {
                "total": self.batch_total,
                "done": self.batch_done or 0,
                "failed": self.batch_failed or 0,
                "zip": bool(self.batch_zip),
            }
        return out


class JobStore:
    """Dict of jobs guarded by a threading.Lock — yt-dlp progress hooks
    mutate jobs from worker threads while the event loop reads them."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def add(self, job: Job) -> None:
        with self._lock:
            self._jobs[job.id] = job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def update(self, job_id: str, **fields) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            for key, value in fields.items():
                setattr(job, key, value)

    def snapshot(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.to_public() if job else None

    def all(self) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())

    def prune(self, keep: int = 200) -> list[Job]:
        """Drop oldest finished (done/error) jobs beyond `keep`; return them."""
        with self._lock:
            finished = sorted(
                (j for j in self._jobs.values() if j.status in ("done", "error")),
                key=lambda j: j.created_at,
            )
            excess = finished[:-keep] if keep > 0 else finished
            for job in excess:
                del self._jobs[job.id]
            return excess

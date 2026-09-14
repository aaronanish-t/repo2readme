"""Hosted demo: submit a public GitHub URL, poll for progress, get the README back.

Run locally:  uvicorn repo2readme.web.app:app --reload
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field, replace
from pathlib import Path

import anthropic
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..config import HOSTED
from ..fetch import FetchError, parse_github_url
from ..llm import ClaudeLLM, StructuredLLM
from ..pipeline import PipelineError, Progress, generate

log = logging.getLogger("repo2readme.web")

STATIC = Path(__file__).parent / "static"
SETTINGS = replace(HOSTED, model=os.environ.get("REPO2README_MODEL", HOSTED.model))
MAX_CONCURRENT_JOBS = int(os.environ.get("REPO2README_MAX_CONCURRENT_JOBS", "2"))
RATE_LIMIT_PER_HOUR = int(os.environ.get("REPO2README_RATE_LIMIT_PER_HOUR", "5"))
RESULT_TTL_S = int(os.environ.get("REPO2README_RESULT_TTL_S", "3600"))
TRUST_PROXY = os.environ.get("REPO2README_TRUST_PROXY", "0") == "1"
MAX_JOBS_KEPT = 200


@dataclass
class Job:
    id: str
    repo: str
    status: str = "queued"  # queued | running | done | error
    created: float = field(default_factory=time.time)
    finished: float | None = None
    events: list[dict] = field(default_factory=list)
    result: dict | None = None
    error: str | None = None

    def public(self) -> dict:
        latest: dict[str, dict] = {}
        for e in self.events:
            latest[e["stage"]] = e
        return {
            "id": self.id,
            "repo": self.repo,
            "status": self.status,
            "stages": list(latest.values()),
            "result": self.result,
            "error": self.error,
        }


class JobRequest(BaseModel):
    repo_url: str


class JobStore:
    def __init__(self) -> None:
        self.jobs: OrderedDict[str, Job] = OrderedDict()
        self.by_repo: dict[str, str] = {}
        self.hits: dict[str, deque[float]] = {}
        self.tasks: set[asyncio.Task] = set()
        self.sem = asyncio.Semaphore(MAX_CONCURRENT_JOBS)

    def reusable(self, repo: str) -> Job | None:
        job = self.jobs.get(self.by_repo.get(repo, ""))
        if not job or job.status == "error":
            return None
        if job.status == "done" and job.finished and time.time() - job.finished > RESULT_TTL_S:
            return None
        return job

    def allow(self, client: str) -> bool:
        now = time.time()
        q = self.hits.setdefault(client, deque())
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= RATE_LIMIT_PER_HOUR:
            return False
        q.append(now)
        return True

    def add(self, job: Job) -> None:
        self.jobs[job.id] = job
        self.by_repo[job.repo] = job.id
        while len(self.jobs) > MAX_JOBS_KEPT:
            old_id, old = self.jobs.popitem(last=False)
            if self.by_repo.get(old.repo) == old_id:
                del self.by_repo[old.repo]


def create_app(llm_factory=lambda: ClaudeLLM(SETTINGS.model), settings=SETTINGS) -> FastAPI:
    app = FastAPI(title="repo2readme", docs_url=None, redoc_url=None)
    store = JobStore()
    app.state.store = store

    async def run_job(job: Job) -> None:
        def on_progress(p: Progress) -> None:
            job.events.append({"stage": p.stage, "done": p.done, "total": p.total, "message": p.message[:200]})
            if len(job.events) > 500:
                del job.events[:250]

        async with store.sem:
            job.status = "running"
            llm: StructuredLLM = llm_factory()
            try:
                result = await generate(
                    f"https://github.com/{job.repo}", llm, settings, on_progress, allow_local=False
                )
                job.result = {"markdown": result.readme, "report": result.report}
                job.status = "done"
            except (FetchError, PipelineError) as e:
                job.error, job.status = str(e), "error"
            except anthropic.RateLimitError:
                job.error, job.status = "The demo is over its model rate limit right now. Try again shortly.", "error"
            except anthropic.APIError as e:
                log.exception("model error for %s", job.repo)
                job.error, job.status = f"Model request failed ({type(e).__name__}).", "error"
            except Exception:
                log.exception("job %s failed", job.id)
                job.error, job.status = "Unexpected server error.", "error"
            finally:
                job.finished = time.time()

    @app.post("/api/jobs")
    async def create_job(body: JobRequest, request: Request) -> dict:
        try:
            ref = parse_github_url(body.repo_url)
        except FetchError as e:
            raise HTTPException(422, str(e))
        if existing := store.reusable(ref.slug):
            return existing.public()
        client = request.client.host if request.client else "unknown"
        if TRUST_PROXY and (fwd := request.headers.get("x-forwarded-for")):
            client = fwd.split(",")[0].strip()
        if not store.allow(client):
            raise HTTPException(429, f"Limit is {RATE_LIMIT_PER_HOUR} new repositories per hour.")
        job = Job(uuid.uuid4().hex, ref.slug)
        store.add(job)
        task = asyncio.create_task(run_job(job))
        store.tasks.add(task)
        task.add_done_callback(store.tasks.discard)
        return job.public()

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str) -> dict:
        job = store.jobs.get(job_id)
        if not job:
            raise HTTPException(404, "No such job")
        return job.public()

    @app.get("/healthz")
    async def healthz() -> dict:
        running = sum(1 for j in store.jobs.values() if j.status == "running")
        return {"ok": True, "running": running, "model": settings.model}

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    return app


app = create_app()

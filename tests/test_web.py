from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from repo2readme.config import Settings
from repo2readme.pipeline import Result
from repo2readme.web import app as web


def test_rejects_non_github_and_local_paths(fake_llm):
    client = TestClient(web.create_app(llm_factory=lambda: fake_llm))
    for bad in ["/etc", "C:\\Users", "https://example.com/a/b", "file:///tmp/x"]:
        r = client.post("/api/jobs", json={"repo_url": bad})
        assert r.status_code == 422, bad
    assert client.get("/api/jobs/nope").status_code == 404
    assert client.get("/healthz").json()["ok"] is True
    assert "repo2readme" in client.get("/").text
    for asset, marker in [("/static/app.js", "renderTimeline"), ("/static/app.css", "--accent")]:
        r = client.get(asset)
        assert r.status_code == 200 and marker in r.text


def test_job_lifecycle_dedupe_and_rate_limit(monkeypatch, fake_llm):
    calls = []

    async def fake_generate(target, llm, settings, progress, *, allow_local):
        calls.append((target, allow_local))
        progress(web.Progress("map", 1, 1, "chunk 1"))
        await asyncio.sleep(0)
        return Result("# hi\n", None, {"ok": True})

    monkeypatch.setattr(web, "generate", fake_generate)
    monkeypatch.setattr(web, "RATE_LIMIT_PER_HOUR", 2)
    with TestClient(web.create_app(llm_factory=lambda: fake_llm, settings=Settings())) as client:
        job = client.post("/api/jobs", json={"repo_url": "https://github.com/a/b"}).json()
        for _ in range(50):
            state = client.get(f"/api/jobs/{job['id']}").json()
            if state["status"] == "done":
                break
        assert state["status"] == "done" and state["result"]["markdown"] == "# hi\n"
        assert calls == [("https://github.com/a/b", False)]

        again = client.post("/api/jobs", json={"repo_url": "github.com/a/b"}).json()
        assert again["id"] == job["id"]  # cached, doesn't count against the limit

        assert client.post("/api/jobs", json={"repo_url": "a/c"}).status_code == 200
        assert client.post("/api/jobs", json={"repo_url": "a/d"}).status_code == 429

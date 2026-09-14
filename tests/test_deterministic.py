from __future__ import annotations

import pytest

from repo2readme.chunk import plan_chunks, split_file
from repo2readme.config import Settings
from repo2readme.fetch import FetchError, parse_github_url
from repo2readme.graph import build_graph
from repo2readme.manifests import extract_repo_facts
from repo2readme.parse import extract
from repo2readme.walk import FileRecord, classify, walk


def _prepare(root):
    walked = walk(root, Settings())
    facts = {f.path: extract(f) for f in walked.files}
    texts = {f.path: f.text for f in walked.files}
    return walked, facts, texts


@pytest.mark.parametrize(
    ("value", "slug"),
    [
        ("https://github.com/pallets/click", "pallets/click"),
        ("https://github.com/pallets/click.git", "pallets/click"),
        ("github.com/spf13/cobra/", "spf13/cobra"),
        ("owner/repo.name", "owner/repo.name"),
    ],
)
def test_parse_github_url_accepts(value, slug):
    assert parse_github_url(value).slug == slug


@pytest.mark.parametrize(
    "value",
    ["https://gitlab.com/a/b", "https://github.com/a/b/tree/main", "file:///etc", "../../etc", "https://github.com/a/.."],
)
def test_parse_github_url_rejects(value):
    with pytest.raises(FetchError):
        parse_github_url(value)


def test_classify_roles():
    assert classify("src/app.py") == "source"
    assert classify("tests/test_app.py") == "test"
    assert classify("web/src/app.test.ts") == "test"
    assert classify("package.json") == "manifest"
    assert classify("docs/guide.md") == "doc"
    assert classify("node_modules/x/index.js") is None
    assert classify("yarn.lock") is None
    assert classify(".env") is None
    assert classify(".env.example") == "config"
    assert classify(".github/workflows/ci.yml") == "ci"
    assert classify(".github/dependabot.yml") is None
    assert classify(".gitignore") is None
    assert classify("CODE_OF_CONDUCT.md") is None
    assert classify("CONTRIBUTING.md") == "doc"


def test_walk_skips_binaries_vendored_and_secrets(fixture_repo):
    walked = walk(fixture_repo, Settings())
    paths = {f.path for f in walked.files}
    assert "src/myapp/core.py" in paths
    assert "assets/logo.png" not in paths
    assert "node_modules/leftpad/index.js" not in paths
    assert ".env" not in paths
    assert "src/myapp/storage/__init__.py" not in paths  # empty


def test_parse_symbols_and_imports(fixture_repo):
    _, facts, _ = _prepare(fixture_repo)
    core = facts["src/myapp/core.py"]
    quals = {s.qualname for s in core.symbols}
    assert {"Engine", "Engine.run", "helper"} <= quals
    run = next(s for s in core.symbols if s.qualname == "Engine.run")
    assert run.start_line == 8
    cli = facts["src/myapp/cli.py"]
    assert {i.spec for i in cli.imports} >= {"os", ".core", "myapp.storage.db"}


def test_graph_resolves_python_and_ts(fixture_repo):
    _, facts, texts = _prepare(fixture_repo)
    graph = build_graph(facts, texts)
    assert graph.edges["src/myapp/cli.py"] == {"src/myapp/core.py", "src/myapp/storage/db.py"}
    assert graph.edges["web/src/index.ts"] == {"web/src/util.ts"}
    assert "src/myapp/core.py" in graph.edges["tests/test_core.py"]


def test_graph_rust_crates_and_mods():
    texts = {
        "Cargo.toml": '[package]\nname = "app"\n',
        "src/main.rs": "mod search;\nuse grep_core::Matcher;\nfn main() {}\n",
        "src/search.rs": "use crate::main;\npub fn go() {}\n",
        "crates/core/Cargo.toml": '[package]\nname = "grep-core"\nversion = "0.1.0"\n',
        "crates/core/src/lib.rs": "pub struct Matcher;\n",
    }
    records = [FileRecord(p, len(t), "manifest" if p.endswith(".toml") else "source",
                          "rust" if p.endswith(".rs") else "toml", t) for p, t in texts.items()]
    facts = {r.path: extract(r) for r in records}
    graph = build_graph(facts, texts)
    assert graph.edges["src/main.rs"] == {"src/search.rs", "crates/core/src/lib.rs"}


def test_manifest_facts(fixture_repo):
    walked, _, _ = _prepare(fixture_repo)
    rf = extract_repo_facts(walked.files, walked.tree, {"python": 30})
    assert "myapp" in rf.project_names and "myapp-web" in rf.project_names
    assert rf.binaries["myapp"] == "myapp.cli:main"
    assert rf.scripts["web/package.json"]["dev"] == "vite"
    assert {"test", "lint"} <= set(rf.make_targets)
    assert {e.name for e in rf.env_vars} == {"MYAPP_TOKEN", "MYAPP_DB_URL"}
    assert rf.license == "MIT"
    assert "SECRET" not in rf.known_tokens()


def test_split_large_file_at_definitions():
    text = "\n".join(f"def f{i}():\n" + "\n".join(f"    x{j} = {j}" for j in range(40)) for i in range(30)) + "\n"
    rec = FileRecord("big.py", len(text), "source", "python", text)
    facts = extract(rec)
    segs = split_file(rec, facts, budget=3000)
    assert len(segs) > 1
    assert segs[0].start_line == 1 and segs[-1].end_line == rec.line_count
    for a, b in zip(segs, segs[1:]):
        assert b.start_line == a.end_line + 1  # contiguous, nothing dropped
    starts = {s.start_line for s in facts.top_level}
    assert all(s.start_line in starts or s.start_line == 1 for s in segs)


def test_plan_respects_budget_and_skeletonizes(fixture_repo):
    walked, facts, texts = _prepare(fixture_repo)
    graph = build_graph(facts, texts)
    plan = plan_chunks(walked.files, facts, graph.in_degree(), chunk_tokens=600, max_chunks=2)
    sent = {p for c in plan.chunks for p in c.paths}
    assert "README.md" in plan.skeleton_only
    assert len(plan.chunks) <= 3
    assert sent and sent.isdisjoint(plan.skeleton_only)
    assert "src/myapp/cli.py" in sent  # entrypoint prioritized

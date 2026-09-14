from __future__ import annotations

from dataclasses import replace

from repo2readme.config import Settings
from repo2readme.pipeline import generate, prepare
from repo2readme.schemas import Evidence, FileSummary, SymbolNote
from repo2readme.verify import MapStats, RepoIndex, check_markdown, verify_file_summary


async def test_end_to_end_with_fake_llm(fixture_repo, fake_llm):
    result = await generate(str(fixture_repo), fake_llm, Settings())
    md, report = result.readme, result.report

    # README structure
    assert md.startswith("# myapp")
    assert "```mermaid\nflowchart LR" in md
    assert "## Installation" in md and "## Configuration" in md and "## License" in md

    # Diagram edges come from real imports only: cli -> core, cli -> storage, core has no import of storage
    edges = {(e["from"], e["to"]) for e in report["diagram_edges"]}
    assert edges == {("cli", "core"), ("cli", "storage")}
    assert "cli -->|1| core" in md

    # Map-pass hallucinations were dropped
    mv = report["map_verification"]
    assert mv["symbols_dropped"] >= 1 and mv["facts_dropped"] >= 2 and mv["summaries_dropped"] >= 1
    synth_input = next(u for s, u in fake_llm.calls if s == "synthesize")
    assert "ghost_function_xyz" not in synth_input
    assert "InventedClass" not in synth_input
    assert "old readme" not in synth_input

    # Draft-level problems were dropped structurally or fixed by the repair round
    assert report["repair_rounds"] == 1
    assert "src/imaginary.py" not in md and "Imaginary feature" not in md
    assert "docs/" not in md
    assert "MYAPP_MADE_UP" not in md
    assert "src/myapp/cache" not in md
    assert "deploy-prod" not in md and "QuantumScheduler" not in md and "--turbo" not in md
    assert report["unverified_mentions"] == []
    dropped = {d["item"] for d in report["dropped_from_draft"]}
    assert {"src/myapp/cache", "docs/", "MYAPP_MADE_UP", "Imaginary feature"} <= dropped
    assert mv["facts_kept"] >= 1  # real, correctly cited claims survive
    assert "MYAPP_TOKEN" in md


async def test_hierarchical_reduce_when_summaries_exceed_budget(fixture_repo, fake_llm):
    settings = replace(Settings(), reduce_input_tokens=200)
    result = await generate(str(fixture_repo), fake_llm, settings)
    assert result.report["reduce_mode"].startswith("hierarchical")
    assert any(stage == "reduce_modules" for stage, _ in fake_llm.calls)


def test_evidence_must_quote_cited_lines(fixture_repo):
    p = prepare(str(fixture_repo), Settings())
    stats = MapStats()
    summary = FileSummary(
        path="src/myapp/core.py",
        purpose="engine",
        key_symbols=[SymbolNote(name="Engine.run", role="runs"), SymbolNote(name="Nope", role="x")],
        facts=[
            Evidence(statement="`save_job` is called per arg", start_line=9, end_line=10),
            Evidence(statement="`save_job` is called in helper", start_line=15, end_line=16),
        ],
        is_entrypoint=False,
    )
    out = verify_file_summary(summary, {"src/myapp/core.py"}, p.index, {}, stats)
    assert [s.name for s in out.key_symbols] == ["Engine.run"]
    assert len(out.facts) == 1 and out.facts[0].start_line == 9


def test_command_checks(fixture_repo):
    p = prepare(str(fixture_repo), Settings())
    idx: RepoIndex = p.index
    assert idx.check_command("npm run build") is None
    assert "not defined" in idx.check_command("npm run deploy")
    assert idx.check_command("make lint") is None
    assert "not defined" in idx.check_command("make release")
    assert idx.check_command("python -m myapp") is None
    assert "not found" in idx.check_command("python -m otherapp")
    assert idx.check_command("myapp jobs") is None
    issues = check_markdown("usage", "Use `Engine` and `--frobnicate` via `src/myapp/cli.py`.", idx)
    assert [i.item for i in issues] == ["--frobnicate"]

from __future__ import annotations

from repo2readme import cli
from repo2readme.config import Settings
from repo2readme.pipeline import generate


async def test_facts_only_readme_is_fully_grounded(fixture_repo):
    result = await generate(str(fixture_repo), None, Settings())
    md, report = result.readme, result.report

    assert report["mode"] == "facts-only"
    assert report["usage"]["estimated_cost_usd"] == 0.0
    assert report["unverified_mentions"] == [] and report["dropped_from_draft"] == []

    assert md.startswith("# myapp\n")
    assert "> A tiny app for tests" in md
    assert "pip install -e ." in md
    assert "make test" in md and "make lint" in md
    assert "`myapp` (`myapp.cli:main`)" in md
    assert "`MYAPP_TOKEN`" in md and "`MYAPP_DB_URL`" in md
    assert "## License\n\nMIT." in md
    assert "```mermaid" in md
    assert "tests" not in {e["from"] for e in report["diagram_edges"]}  # test code isn't a component


def test_cli_requires_credentials_unless_no_model(fixture_repo, monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert cli.main([str(fixture_repo)]) == 3
    assert "--no-model" in capsys.readouterr().err

    out = tmp_path / "README.md"
    assert cli.main([str(fixture_repo), "--no-model", "-o", str(out)]) == 0
    assert out.read_text(encoding="utf-8").startswith("# myapp")

"""A deterministic stand-in for Claude that mixes real and hallucinated claims, so tests can check
that grounding removes exactly the fabricated parts."""

from __future__ import annotations

import json
import re

from repo2readme.llm import Usage
from repo2readme.schemas import (
    ChunkSummary,
    Component,
    ConfigEntry,
    Evidence,
    Feature,
    FileSummary,
    ModuleBatch,
    ModuleSummary,
    ReadmeDraft,
    StructureEntry,
    SymbolNote,
)

_PATHS = re.compile(r"Return one entry for each of these paths: (\[.*\])")
_FILE = re.compile(r'<file path="([^"]+)"[^>]*>\n(.*?)\n</file>', re.S)
_SYMS = re.compile(r"^- `([^`]+)`: (.*)$", re.M)


class _U:
    input_tokens = 1000
    output_tokens = 200
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class FakeLLM:
    def __init__(self) -> None:
        self.usage = Usage("fake-model")
        self.calls: list[tuple[str, str]] = []

    async def generate(self, *, stage, system, user, schema, effort, max_tokens):
        self.calls.append((stage, user))
        self.usage.record(stage, _U())
        if schema is ChunkSummary:
            return self._map(user)
        if schema is ModuleBatch:
            mods = json.loads(re.search(r"Return one entry for each module: (\[.*\])", user).group(1))
            return ModuleBatch(modules=[ModuleSummary(path=m, summary=f"Module {m}.", responsibilities=["things"],
                                                      key_files=[]) for m in mods])
        if schema is ReadmeDraft:
            return self._draft(repair=stage == "repair")
        raise AssertionError(schema)

    def _map(self, user: str) -> ChunkSummary:
        paths = json.loads(_PATHS.search(user).group(1))
        bodies = {m.group(1): m.group(2) for m in _FILE.finditer(user)}
        syms = {m.group(1): re.findall(r"`([^`]+)`", m.group(2)) for m in _SYMS.finditer(user)}
        out = []
        for path in paths:
            facts = []
            first = bodies.get(path, "").splitlines()[:1]
            if first:
                n, _, code = first[0].partition("| ")
                word = next((w for w in re.findall(r"[A-Za-z_]\w+", code)), None)
                if word:
                    facts.append(Evidence(statement=f"Starts with `{word}`.", start_line=int(n), end_line=int(n)))
            facts.append(Evidence(statement="Calls `ghost_function_xyz` to phone home.", start_line=1, end_line=1))
            facts.append(Evidence(statement="Out of range claim.", start_line=9999, end_line=10000))
            out.append(FileSummary(
                path=path,
                purpose=f"Purpose of {path}.",
                key_symbols=[SymbolNote(name=s, role="does work") for s in syms.get(path, [])[:2]]
                + [SymbolNote(name="InventedClass", role="not real")],
                facts=facts,
                is_entrypoint=path.endswith("cli.py"),
            ))
        out.append(FileSummary(path="src/not_requested.py", purpose="x", key_symbols=[], facts=[], is_entrypoint=False))
        return ChunkSummary(files=out)

    def _draft(self, repair: bool) -> ReadmeDraft:
        bad_install = "" if repair else "\nnpm run deploy-prod"
        return ReadmeDraft(
            title="myapp",
            tagline="Runs jobs and stores them.",
            overview="`myapp` wires an `Engine` to a `Database`." + ("" if repair else " It uses `QuantumScheduler`."),
            features=[
                Feature(text="Job runner via `Engine.run`", evidence_paths=["src/myapp/core.py"]),
                Feature(text="Imaginary feature", evidence_paths=["src/imaginary.py"]),
            ],
            architecture="The CLI builds the engine, which persists jobs through storage.",
            components=[
                Component(id="cli", label="CLI", description="Entry point", paths=["src/myapp/cli.py"]),
                Component(id="core", label="Core engine", description="Job execution", paths=["src/myapp/core.py"]),
                Component(id="storage", label="Storage", description="Persistence", paths=["src/myapp/storage", "src/myapp/cache"]),
                Component(id="web", label="Web UI", description="Front end", paths=["web/src"]),
            ],
            installation=f"```bash\npip install -e .\nmake test{bad_install}\n```",
            usage="Run `myapp` with job names." + ("" if repair else " Pass `--turbo` for speed."),
            configuration=[
                ConfigEntry(name="MYAPP_TOKEN", description="API token"),
                ConfigEntry(name="MYAPP_MADE_UP", description="Not read anywhere"),
            ],
            project_structure=[
                StructureEntry(path="src/myapp", description="Python package"),
                StructureEntry(path="docs/", description="Docs that don't exist"),
            ],
            development="```bash\nmake lint\n```",
        )

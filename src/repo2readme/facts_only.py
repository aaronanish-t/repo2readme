"""A README built purely from extracted facts, with no model calls.

Used when no Anthropic credentials are configured (e.g. a fresh deployment) and via `--no-model`.
Everything here comes from the parser, manifests and import graph, so it is thin but never invented.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from .schemas import Component, ConfigEntry, ReadmeDraft, StructureEntry

if TYPE_CHECKING:
    from .pipeline import Prepared

MAX_COMPONENTS = 8


def _module_of(path: str) -> str:
    from .pipeline import module_of

    return module_of(path)


def _fence(lines: list[str]) -> str:
    return "```bash\n" + "\n".join(lines) + "\n```" if lines else ""


def facts_only_draft(p: Prepared) -> ReadmeDraft:
    rf = p.repo_facts
    source = [f for f in p.walk.files if f.role == "source" and f.language and f.path in p.facts]
    in_degree = p.graph.in_degree()
    root_names = {PurePosixPath(t).name for t in p.walk.tree if "/" not in t}

    # ---- components: the largest modules by source file count
    from .manifests import is_non_product

    product = [f for f in source if not is_non_product(f.path) and not f.path.startswith(".")]
    source = product or source
    counts = Counter(_module_of(f.path) for f in source)
    components: list[Component] = []
    for mod, n in counts.most_common(MAX_COMPONENTS):
        files = [f.path for f in source if _module_of(f.path) == mod]
        paths = [mod] if mod != "(root)" else files[:10]
        hubs = sorted(files, key=lambda f: -in_degree.get(f, 0))[:3]
        symbols = [s.name for f in hubs for s in p.facts[f].top_level[:3]]
        desc = f"{n} source file{'s' if n != 1 else ''}"
        if symbols:
            desc += "; defines " + ", ".join(f"`{s}`" for s in dict.fromkeys(symbols))
        cid = re.sub(r"\W+", "_", mod).strip("_") or "root"
        label = "root" if mod == "(root)" else mod.split("/")[-1]
        components.append(Component(id=cid, label=label, description=desc, paths=paths))

    if len(components) == 1 and len(source) > 3:
        # One package: a single box says nothing, so show its most-imported files instead.
        hubs = sorted(source, key=lambda f: (-in_degree.get(f.path, 0), f.path))[: MAX_COMPONENTS - 1]
        components = []
        for f in hubs:
            names = [s.name for s in p.facts[f.path].top_level[:4]]
            desc = f"imported by {in_degree.get(f.path, 0)} files"
            if names:
                desc += "; defines " + ", ".join(f"`{n}`" for n in names)
            cid = re.sub(r"\W+", "_", f.path).strip("_")
            components.append(Component(id=cid, label=PurePosixPath(f.path).name, description=desc, paths=[f.path]))

    # ---- install / usage / development commands, only from manifests that exist
    pm = next((m for m in rf.package_managers if m in {"pnpm", "yarn", "bun", "npm"}), "npm")
    root_scripts = rf.scripts.get("package.json", {})
    install: list[str] = []
    if "pyproject.toml" in root_names:
        install.append("pip install -e .")
    elif "requirements.txt" in root_names:
        install.append("pip install -r requirements.txt")
    if "package.json" in root_names:
        install.append(f"{pm} install")
    if "go.mod" in root_names:
        install.append("go build ./...")
    if "Cargo.toml" in root_names:
        install.append("cargo build --release")

    usage_parts: list[str] = []
    if rf.binaries:
        usage_parts.append(
            "Installed commands:\n\n" + "\n".join(f"- `{name}` (`{target}`)" for name, target in rf.binaries.items())
        )
    run_scripts = [s for s in ("start", "dev", "serve") if s in root_scripts]
    if run_scripts:
        usage_parts.append(_fence([f"{pm} run {s}" for s in run_scripts]))

    dev: list[str] = []
    for target in ("test", "lint", "check", "build"):
        if target in rf.make_targets:
            dev.append(f"make {target}")
        elif target in root_scripts:
            dev.append(f"{pm} run {target}")
    if not dev:
        has_tests = any(f.role == "test" for f in p.walk.files)
        if "Cargo.toml" in root_names:
            dev.append("cargo test")
        elif "go.mod" in root_names:
            dev.append("go test ./...")
        elif has_tests and "python" in rf.languages:
            dev.append("pytest")

    # ---- prose that restates facts
    langs = sorted(rf.languages.items(), key=lambda kv: -kv[1])
    lang_text = ", ".join(f"{name} ({loc:,} lines)" for name, loc in langs[:4]) or "no parsed source"
    overview = [
        f"{len(source)} source files across {len(counts)} module{'s' if len(counts) != 1 else ''}. Languages: {lang_text}.",
    ]
    if rf.package_managers:
        overview.append(f"Package management: {', '.join(rf.package_managers)}.")

    edge_count = sum(len(v) for v in p.graph.edges.values())
    architecture = (
        ("Components below are the most-imported files" if len(counts) == 1 and len(source) > 3
         else "Components below are the largest modules by file count")
        + f". Arrows come from {edge_count} resolved import statements between files."
    )

    structure = [
        StructureEntry(path=mod, description=f"{n} source file{'s' if n != 1 else ''}")
        for mod, n in counts.most_common(12)
        if mod != "(root)"
    ]

    title = rf.project_names[0].split("/")[-1] if rf.project_names else p.name.split("/")[-1]
    return ReadmeDraft(
        title=title,
        tagline=rf.descriptions[0] if rf.descriptions else "",
        overview=" ".join(overview[:1]) + ("\n\n" + " ".join(overview[1:]) if len(overview) > 1 else ""),
        features=[],
        architecture=architecture,
        components=components,
        installation=_fence(install),
        usage="\n\n".join(usage_parts),
        configuration=[ConfigEntry(name=e.name, description=f"Read in `{e.path}` (line {e.line})") for e in rf.env_vars[:30]],
        project_structure=structure,
        development=_fence(dev),
    )

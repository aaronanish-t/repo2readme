"""Facts read straight out of manifests and config: names, scripts, dependencies, env vars, license.

Install/run instructions in the README are checked against these, so the model can't invent a
`npm run dev` that doesn't exist.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .walk import FileRecord

_ENV_PATTERNS = [
    re.compile(r"""os\.environ(?:\.get)?\s*[\[(]\s*["']([A-Z][A-Z0-9_]{1,})["']"""),
    re.compile(r"""os\.getenv\(\s*["']([A-Z][A-Z0-9_]{1,})["']"""),
    re.compile(r"""process\.env\.([A-Z][A-Z0-9_]{1,})"""),
    re.compile(r"""process\.env\[\s*["']([A-Z][A-Z0-9_]{1,})["']"""),
    re.compile(r"""import\.meta\.env\.([A-Z][A-Z0-9_]{1,})"""),
    re.compile(r"""os\.(?:Getenv|LookupEnv)\(\s*"([A-Z][A-Z0-9_]{1,})"""),
    re.compile(r"""env::var\(\s*"([A-Z][A-Z0-9_]{1,})"""),
    re.compile(r"""ENV\[\s*["']([A-Z][A-Z0-9_]{1,})["']"""),
    re.compile(r"""System\.getenv\(\s*"([A-Z][A-Z0-9_]{1,})"""),
    re.compile(r"""Environment\.GetEnvironmentVariable\(\s*"([A-Z][A-Z0-9_]{1,})"""),
]
_DOTENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]{1,})\s*=", re.M)
_MAKE_TARGET = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)\s*:(?!=)", re.M)
_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)", re.M)
_GO_REQUIRE = re.compile(r"^\s*(?:require\s+)?([a-z0-9.-]+\.[a-z]+/\S+)\s+v", re.M)


@dataclass
class EnvVar:
    name: str
    path: str
    line: int


@dataclass
class RepoFacts:
    project_names: list[str] = field(default_factory=list)
    descriptions: list[str] = field(default_factory=list)
    languages: dict[str, int] = field(default_factory=dict)  # language -> lines of code
    package_managers: list[str] = field(default_factory=list)
    scripts: dict[str, dict[str, str]] = field(default_factory=dict)  # manifest path -> name -> command
    binaries: dict[str, str] = field(default_factory=dict)  # console command -> target
    dependencies: dict[str, list[str]] = field(default_factory=dict)  # manifest path -> names
    make_targets: list[str] = field(default_factory=list)
    docker: list[str] = field(default_factory=list)
    env_vars: list[EnvVar] = field(default_factory=list)
    license: str | None = None
    ci_files: list[str] = field(default_factory=list)
    runtime_versions: list[str] = field(default_factory=list)

    def known_tokens(self) -> set[str]:
        """Every literal a README may legitimately mention without it appearing in code symbols."""
        toks: set[str] = set(self.project_names) | set(self.make_targets) | set(self.binaries)
        for scripts in self.scripts.values():
            toks |= set(scripts)
        for deps in self.dependencies.values():
            toks |= set(deps)
        toks |= {e.name for e in self.env_vars}
        return toks

    def to_prompt(self) -> str:
        lines = ["## Manifest facts (extracted deterministically; authoritative)"]
        if self.project_names:
            lines.append(f"- Declared project names: {', '.join(self.project_names)}")
        for d in self.descriptions:
            lines.append(f"- Declared description: {d}")
        if self.languages:
            langs = sorted(self.languages.items(), key=lambda kv: -kv[1])
            lines.append("- Languages by lines of code: " + ", ".join(f"{k} ({v})" for k, v in langs[:8]))
        if self.package_managers:
            lines.append(f"- Package managers (from lockfiles/manifests): {', '.join(self.package_managers)}")
        if self.runtime_versions:
            lines.append(f"- Runtime constraints: {'; '.join(self.runtime_versions)}")
        for path, scripts in self.scripts.items():
            lines.append(f"- Scripts in `{path}`:")
            lines.extend(f"  - `{k}`: `{v}`" for k, v in list(scripts.items())[:30])
        if self.binaries:
            lines.append("- Installed commands: " + ", ".join(f"`{k}` -> `{v}`" for k, v in self.binaries.items()))
        if self.make_targets:
            lines.append("- Make targets: " + ", ".join(f"`{t}`" for t in self.make_targets[:30]))
        for path, deps in self.dependencies.items():
            shown = ", ".join(deps[:40]) + (f" (+{len(deps) - 40} more)" if len(deps) > 40 else "")
            lines.append(f"- Dependencies in `{path}`: {shown}")
        lines.extend(f"- Docker: {d}" for d in self.docker)
        if self.env_vars:
            lines.append("- Environment variables read by the code:")
            lines.extend(f"  - `{e.name}` ({e.path}:{e.line})" for e in self.env_vars[:50])
        if self.license:
            lines.append(f"- License: {self.license}")
        if self.ci_files:
            lines.append(f"- CI workflows: {', '.join(self.ci_files)}")
        return "\n".join(lines)


def _license_name(text: str) -> str | None:
    head = text[:1500]
    for needle, name in [
        ("MIT License", "MIT"), ("Permission is hereby granted, free of charge", "MIT"),
        ("Apache License", "Apache-2.0"), ("GNU AFFERO GENERAL PUBLIC LICENSE", "AGPL-3.0"),
        ("GNU LESSER GENERAL PUBLIC LICENSE", "LGPL"), ("GNU GENERAL PUBLIC LICENSE", "GPL"),
        ("Mozilla Public License", "MPL-2.0"), ("BSD 3-Clause", "BSD-3-Clause"),
        ("Redistribution and use in source and binary forms", "BSD"), ("The Unlicense", "Unlicense"),
        ("ISC License", "ISC"),
    ]:
        if needle.lower() in head.lower():
            return name
    return None


def extract_repo_facts(files: list[FileRecord], tree: list[str], loc_by_lang: dict[str, int]) -> RepoFacts:
    facts = RepoFacts(languages=loc_by_lang)
    names = {PurePosixPath(p).name for p in tree}
    for lock, pm in [
        ("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("package-lock.json", "npm"), ("bun.lockb", "bun"),
        ("bun.lock", "bun"), ("uv.lock", "uv"), ("poetry.lock", "poetry"), ("Pipfile.lock", "pipenv"),
        ("Cargo.lock", "cargo"), ("go.sum", "go modules"),
    ]:
        if lock in names:
            facts.package_managers.append(pm)

    for f in files:
        name = PurePosixPath(f.path).name
        depth = f.path.count("/")
        try:
            if name == "package.json" and depth <= 2:
                data = json.loads(f.text)
                if data.get("name"):
                    facts.project_names.append(data["name"])
                if data.get("description") and depth == 0:
                    facts.descriptions.append(str(data["description"])[:300])
                if isinstance(data.get("scripts"), dict) and data["scripts"]:
                    facts.scripts[f.path] = {k: str(v) for k, v in data["scripts"].items()}
                bins = data.get("bin")
                if isinstance(bins, str) and data.get("name"):
                    facts.binaries[data["name"].split("/")[-1]] = bins
                elif isinstance(bins, dict):
                    facts.binaries.update({k: str(v) for k, v in bins.items()})
                deps = [*(data.get("dependencies") or {}), *(data.get("devDependencies") or {})]
                if deps:
                    facts.dependencies[f.path] = deps
                if isinstance(data.get("engines"), dict):
                    facts.runtime_versions += [f"{k} {v}" for k, v in data["engines"].items()]
                if isinstance(data.get("packageManager"), str):
                    facts.package_managers.append(data["packageManager"])
            elif name == "pyproject.toml" and depth <= 2:
                data = tomllib.loads(f.text)
                proj = data.get("project") or {}
                poetry = (data.get("tool") or {}).get("poetry") or {}
                if proj.get("name") or poetry.get("name"):
                    facts.project_names.append(proj.get("name") or poetry.get("name"))
                if (d := proj.get("description") or poetry.get("description")) and depth == 0:
                    facts.descriptions.append(str(d)[:300])
                facts.binaries.update({k: str(v) for k, v in (proj.get("scripts") or poetry.get("scripts") or {}).items()})
                deps = [re.split(r"[\s<>=!~;\[]", d)[0] for d in proj.get("dependencies") or []]
                deps += [k for k in (poetry.get("dependencies") or {}) if k != "python"]
                for extra in (proj.get("optional-dependencies") or {}).values():
                    deps += [re.split(r"[\s<>=!~;\[]", d)[0] for d in extra]
                if deps:
                    facts.dependencies[f.path] = sorted(set(deps))
                if proj.get("requires-python"):
                    facts.runtime_versions.append(f"python {proj['requires-python']}")
                if "uv" not in facts.package_managers and (data.get("tool") or {}).get("uv") is not None:
                    facts.package_managers.append("uv")
            elif name == "Cargo.toml" and depth <= 2:
                data = tomllib.loads(f.text)
                pkg = data.get("package") or {}
                if pkg.get("name"):
                    facts.project_names.append(pkg["name"])
                for b in data.get("bin") or []:
                    if b.get("name"):
                        facts.binaries[b["name"]] = b.get("path", "")
                deps = list(data.get("dependencies") or {})
                if deps:
                    facts.dependencies[f.path] = deps
            elif name == "go.mod":
                if m := re.search(r"^module\s+(\S+)", f.text, re.M):
                    facts.project_names.append(m.group(1))
                if m := re.search(r"^go\s+(\S+)", f.text, re.M):
                    facts.runtime_versions.append(f"go {m.group(1)}")
                deps = _GO_REQUIRE.findall(f.text)
                if deps:
                    facts.dependencies[f.path] = deps
            elif name.startswith("requirements") and name.endswith(".txt"):
                deps = [d for d in _REQ_NAME.findall(f.text) if not d.startswith("-")]
                if deps:
                    facts.dependencies[f.path] = deps
            elif name in {"Makefile", "justfile"}:
                facts.make_targets += [t for t in _MAKE_TARGET.findall(f.text) if not t.startswith(".")]
            elif name == "Dockerfile" or name.startswith("Dockerfile."):
                for line in f.text.splitlines():
                    if line.strip().upper().startswith(("EXPOSE", "CMD", "ENTRYPOINT")):
                        facts.docker.append(f"{f.path}: {line.strip()[:160]}")
            elif name.upper().startswith(("LICENSE", "COPYING")) and depth == 0:
                facts.license = _license_name(f.text)
            elif f.role == "ci":
                facts.ci_files.append(f.path)
        except (ValueError, tomllib.TOMLDecodeError, AttributeError, TypeError):
            continue  # malformed manifest: skip rather than guess

    seen: set[str] = set()
    for f in files:
        if f.role not in {"source", "config", "manifest"}:
            continue
        name = PurePosixPath(f.path).name
        patterns = [_DOTENV_LINE] if name.startswith(".env") else _ENV_PATTERNS
        for pat in patterns:
            for m in pat.finditer(f.text):
                var = m.group(1)
                if var not in seen:
                    seen.add(var)
                    facts.env_vars.append(EnvVar(var, f.path, f.text.count("\n", 0, m.start()) + 1))
    facts.make_targets = list(dict.fromkeys(facts.make_targets))
    facts.package_managers = list(dict.fromkeys(facts.package_managers))
    return facts

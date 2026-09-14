"""Grounding checks. Model output that cannot be traced back to the repository is dropped or flagged."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .manifests import RepoFacts
from .parse import FileFacts
from .schemas import Evidence, FileSummary, ReadmeDraft

_INLINE_CODE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
_FENCE = re.compile(r"```([\w+-]*)\n(.*?)```", re.S)
_CHECKABLE = re.compile(r"^[A-Za-z_@.~/][\w@./:\\-]*(\(\))?$")
SHELL_LANGS = {"", "sh", "bash", "shell", "console", "zsh", "powershell", "ps1", "cmd"}
SHELL_BUILTINS = {
    "cd", "git", "export", "set", "echo", "cp", "mv", "mkdir", "source", "curl", "docker", "docker-compose",
    "sudo", "brew", "apt", "apt-get", "pip", "pip3", "pipx", "uv", "poetry", "python", "python3", "py",
    "npm", "npx", "pnpm", "yarn", "bun", "deno", "node", "cargo", "go", "make", "just", "mvn", "gradle",
    "./gradlew", "dotnet", "bundle", "gem", "rake", "composer", "php", "java", "rustup", "cmake", "pytest",
    "uvicorn", "ls", "cat", "open", "start", "venv", ".venv/bin/activate", "conda",
}
PM_SUBCOMMANDS = {
    "install", "i", "ci", "add", "remove", "test", "start", "build", "run", "exec", "dlx", "create", "init",
    "sync", "lock", "update", "upgrade", "publish", "link", "dev", "-m", "--version", "version", "fmt", "check",
    "clippy", "get", "mod", "tidy", "shell", "venv", "tool", "compose", "up", "down", "clone",
}


@dataclass
class Issue:
    field: str
    item: str
    reason: str

    def render(self) -> str:
        return f"- [{self.field}] `{self.item}`: {self.reason}"


@dataclass
class MapStats:
    files_summarized: int = 0
    summaries_dropped: int = 0
    symbols_kept: int = 0
    symbols_dropped: int = 0
    facts_kept: int = 0
    facts_dropped: int = 0
    dropped_examples: list[str] = field(default_factory=list)

    def note(self, msg: str) -> None:
        if len(self.dropped_examples) < 25:
            self.dropped_examples.append(msg)


class RepoIndex:
    def __init__(self, texts: dict[str, str], facts: dict[str, FileFacts], repo_facts: RepoFacts, tree: list[str]):
        self.texts = texts
        self.lines = {p: t.splitlines() for p, t in texts.items()}
        self.facts = facts
        self.repo_facts = repo_facts
        self.files = set(tree)
        self.dirs = {str(parent) for p in tree for parent in PurePosixPath(p).parents if str(parent) != "."}
        self.symbols: set[str] = set()
        for f in facts.values():
            for s in f.symbols:
                self.symbols.add(s.name)
                self.symbols.add(s.qualname)
        self.known = repo_facts.known_tokens()
        # The existing README is excluded: a stale README must not vouch for a claim.
        self._corpus = "\n".join(t for p, t in texts.items() if not re.fullmatch(r"(?i)readme(\.\w+)?", p))
        self._corpus_lower: str | None = None
        self.scripts = {name for s in repo_facts.scripts.values() for name in s}

    def path_exists(self, path: str) -> bool:
        p = path.strip().strip("/").removeprefix("./")
        return p == "" or p in self.files or p in self.dirs

    def token_grounded(self, token: str) -> bool:
        t = token.strip()
        bare = t.removesuffix("()").rstrip(":;,")
        if not bare:
            return True
        if bare in self.known or bare in self.symbols or self.path_exists(bare):
            return True
        if "." in bare and bare.split(".")[-1] in self.symbols:
            return True  # Class.method, module.func
        if bare in self._corpus:
            return True
        if self._corpus_lower is None:
            self._corpus_lower = self._corpus.lower()
        return len(bare) >= 4 and bare.lower() in self._corpus_lower  # HTTP headers, SQL keywords

    def check_command(self, line: str) -> str | None:
        """Return a reason string if a shell command references something that doesn't exist."""
        line = line.strip().removeprefix("$ ").strip()
        if not line or line.startswith("#"):
            return None
        try:
            argv = shlex.split(line, posix=True)
        except ValueError:
            return None
        if not argv:
            return None
        head = argv[0]
        if head in {"npm", "pnpm", "yarn", "bun"}:
            if len(argv) >= 3 and argv[1] == "run" and argv[2] not in self.scripts:
                return f"script `{argv[2]}` is not defined in any package.json"
            if head != "npm" and len(argv) >= 2 and argv[1] not in PM_SUBCOMMANDS and not argv[1].startswith("-"):
                if argv[1] not in self.scripts:
                    return f"`{head} {argv[1]}` is neither a {head} command nor a declared script"
            return None
        if head in {"make", "just"} and len(argv) >= 2 and not argv[1].startswith("-"):
            if argv[1] not in self.repo_facts.make_targets:
                return f"target `{argv[1]}` is not defined in the Makefile/justfile"
            return None
        if head in {"python", "python3", "py"} and len(argv) >= 3 and argv[1] == "-m":
            mod = argv[2]
            as_path = mod.replace(".", "/")
            if not (
                mod in self.known
                or any(self.path_exists(c) for c in (f"{as_path}.py", as_path, f"src/{as_path}.py", f"src/{as_path}"))
                or mod in {"pip", "venv", "pytest", "uvicorn", "http.server", "build", "twine", "unittest", "mypy", "ruff"}
            ):
                return f"module `{mod}` not found in the repository or its dependencies"
            return None
        if head in {"python", "python3", "py", "node", "deno", "bun", "go"} and len(argv) >= 2:
            target = argv[2] if head == "go" and argv[1] == "run" and len(argv) >= 3 else argv[1]
            if re.search(r"\.(py|js|mjs|ts|go)$", target) and not self.path_exists(target):
                return f"file `{target}` does not exist"
            return None
        if head in SHELL_BUILTINS or head.startswith(("./", "../")) and self.path_exists(head):
            return None
        if head in self.repo_facts.binaries or head in self.known:
            return None
        if head.startswith(("./", "../")):
            return f"`{head}` does not exist"
        return None  # unknown external tool: not something we can check


def verify_file_summary(
    summary: FileSummary,
    allowed_paths: set[str],
    index: RepoIndex,
    ranges: dict[str, list[tuple[int, int]]],
    stats: MapStats,
) -> FileSummary | None:
    if summary.path not in allowed_paths:
        stats.summaries_dropped += 1
        stats.note(f"summary for unrequested path `{summary.path}`")
        return None
    facts = index.facts.get(summary.path)
    names = {s.name for s in facts.symbols} | {s.qualname for s in facts.symbols} if facts else set()
    lines = index.lines.get(summary.path, [])
    shown = ranges.get(summary.path, [(1, len(lines))])

    symbols = []
    for sym in summary.key_symbols:
        if sym.name in names:
            symbols.append(sym)
            stats.symbols_kept += 1
        else:
            stats.symbols_dropped += 1
            stats.note(f"{summary.path}: symbol `{sym.name}` not defined in file")

    kept: list[Evidence] = []
    for ev in summary.facts:
        reason = _check_evidence(ev, lines, shown)
        if reason:
            stats.facts_dropped += 1
            stats.note(f"{summary.path}: {reason}")
        else:
            kept.append(ev)
            stats.facts_kept += 1
    stats.files_summarized += 1
    return summary.model_copy(update={"key_symbols": symbols, "facts": kept})


def _check_evidence(ev: Evidence, lines: list[str], shown: list[tuple[int, int]]) -> str | None:
    if ev.start_line < 1 or ev.end_line < ev.start_line or ev.end_line > len(lines):
        return f"line range {ev.start_line}-{ev.end_line} out of bounds"
    if not any(a <= ev.start_line and ev.end_line <= b + 2 for a, b in shown):
        return f"cites lines {ev.start_line}-{ev.end_line} that were not shown"
    window = "\n".join(lines[max(0, ev.start_line - 3) : ev.end_line + 2])
    for token in _INLINE_CODE.findall(ev.statement):
        t = token.strip().removesuffix("()")
        if t and t not in window and t.strip("'\"") not in window:
            return f"`{t}` is not in cited lines {ev.start_line}-{ev.end_line}"
    return None


def merge_parts(parts: list[FileSummary]) -> FileSummary:
    """Combine summaries of a file that was split across chunks."""
    if len(parts) == 1:
        return parts[0]
    seen: set[str] = set()
    symbols = []
    for p in parts:
        for s in p.key_symbols:
            if s.name not in seen:
                seen.add(s.name)
                symbols.append(s)
    purposes = list(dict.fromkeys(p.purpose for p in parts))
    return FileSummary(
        path=parts[0].path,
        purpose=" ".join(purposes[:3]),
        key_symbols=symbols,
        facts=[f for p in parts for f in p.facts],
        is_entrypoint=any(p.is_entrypoint for p in parts),
    )


def verify_draft(draft: ReadmeDraft, index: RepoIndex) -> tuple[ReadmeDraft, list[Issue], list[Issue]]:
    """Returns (cleaned draft, entries dropped from it, mentions left in it that couldn't be verified).

    Structured entries (components, features, structure, config) with nothing real behind them are
    removed outright. Prose can't be edited safely, so unverifiable mentions there are flagged for repair.
    """
    issues: list[Issue] = []  # dropped entries

    components = []
    for c in draft.components:
        good = [p for p in c.paths if index.path_exists(p)]
        for bad in set(c.paths) - set(good):
            issues.append(Issue(f"components.{c.id}", bad, "path does not exist"))
        if good:
            components.append(c.model_copy(update={"paths": [p.strip("/").removeprefix("./") for p in good]}))

    features = []
    for f in draft.features:
        good = [p for p in f.evidence_paths if index.path_exists(p)]
        if not good:
            issues.append(Issue("features", f.text[:80], "no existing evidence path"))
            continue
        features.append(f.model_copy(update={"evidence_paths": good}))

    structure = []
    for s in draft.project_structure:
        if index.path_exists(s.path):
            structure.append(s)
        else:
            issues.append(Issue("project_structure", s.path, "path does not exist"))

    config = []
    env_names = {e.name for e in index.repo_facts.env_vars}
    for c in draft.configuration:
        if c.name in env_names or index.token_grounded(c.name):
            config.append(c)
        else:
            issues.append(Issue("configuration", c.name, "not found in code or config files"))

    cleaned = draft.model_copy(
        update={"components": components, "features": features, "project_structure": structure, "configuration": config}
    )

    prose_fields = {
        "tagline": cleaned.tagline,
        "overview": cleaned.overview,
        "architecture": cleaned.architecture,
        "installation": cleaned.installation,
        "usage": cleaned.usage,
        "development": cleaned.development,
        **{f"features[{i}]": f.text for i, f in enumerate(cleaned.features)},
        **{f"components.{c.id}": c.description for c in cleaned.components},
    }
    labels = {c.label for c in cleaned.components} | {cleaned.title}
    unverified: list[Issue] = []
    for name, text in prose_fields.items():
        unverified.extend(check_markdown(name, text, index, labels))
    return cleaned, issues, unverified


def check_markdown(field_name: str, text: str, index: RepoIndex, allowed: set[str] = frozenset()) -> list[Issue]:
    issues: list[Issue] = []
    for lang, body in _FENCE.findall(text):
        if lang.lower() in SHELL_LANGS:
            for line in body.splitlines():
                if reason := index.check_command(line):
                    issues.append(Issue(field_name, line.strip(), reason))
    prose = _FENCE.sub("", text)
    for token in _INLINE_CODE.findall(prose):
        t = token.strip()
        if t in allowed:
            continue
        if " " in t:
            if reason := index.check_command(t):
                issues.append(Issue(field_name, t, reason))
            continue
        if re.fullmatch(r"--?[A-Za-z][\w-]*", t):  # CLI flags are a classic hallucination
            if t not in index._corpus:
                issues.append(Issue(field_name, t, "flag not found in the repository"))
            continue
        if _CHECKABLE.match(t) and not index.token_grounded(t):
            issues.append(Issue(field_name, t, "not found in the repository"))
    return issues

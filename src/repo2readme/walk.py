"""Discover the files worth reading, and classify each one's role."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import tree_sitter_language_pack as tslp

from .config import Settings

IGNORED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "bower_components", "vendor", "third_party", "thirdparty",
    "dist", "build", "out", "target", ".next", ".nuxt", ".venv", "venv", "env", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".tox", ".gradle", ".idea", ".vscode", "coverage", ".cache",
    "site-packages", "Pods", "DerivedData", ".terraform",
}
IGNORED_SUFFIXES = (
    ".min.js", ".min.css", ".map", ".lock", ".sum", ".snap", ".svg", ".png", ".jpg", ".jpeg", ".gif",
    ".ico", ".webp", ".pdf", ".zip", ".gz", ".tar", ".jar", ".class", ".so", ".dll", ".dylib", ".exe",
    ".wasm", ".woff", ".woff2", ".ttf", ".otf", ".mp4", ".mp3", ".pyc", ".bin", ".pb", ".onnx", ".pt",
    ".parquet", ".sqlite", ".db", ".ipynb",
)
IGNORED_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Cargo.lock", "go.sum",
    "composer.lock", "Gemfile.lock", "uv.lock", ".DS_Store",
}
HOUSEKEEPING_STEMS = {
    "CODE_OF_CONDUCT", "CONDUCT", "SECURITY", "MAINTAINERS", "AUTHORS", "CODEOWNERS", "CONTRIBUTORS",
    "FUNDING", "SUPPORT", "GOVERNANCE", "NOTICE", "PULL_REQUEST_TEMPLATE", "ISSUE_TEMPLATE",
}
MANIFEST_NAMES = {
    "package.json", "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Cargo.toml",
    "go.mod", "Gemfile", "composer.json", "pom.xml", "build.gradle", "build.gradle.kts", "Makefile",
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yaml", "justfile",
    "CMakeLists.txt", "deno.json", "mix.exs", "pubspec.yaml", "Package.swift",
}
CONFIG_SUFFIXES = (".toml", ".yaml", ".yml", ".json", ".ini", ".cfg", ".env.example", ".conf")
DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc")
# Languages we treat as prose/config rather than code, even though tree-sitter can parse them.
NON_CODE_LANGS = {"markdown", "json", "yaml", "toml", "ini", "xml", "csv", "html", "css", "scss"}


@dataclass
class FileRecord:
    path: str  # POSIX, relative to repo root
    size: int
    role: str  # manifest | source | test | doc | config | ci
    language: str | None
    text: str = field(default="", repr=False)

    @property
    def line_count(self) -> int:
        return self.text.count("\n") + (0 if self.text.endswith("\n") or not self.text else 1)


@dataclass
class WalkResult:
    files: list[FileRecord]
    skipped: dict[str, int]  # reason -> count
    tree: list[str]  # every discovered path (incl. skipped binaries), for the directory overview


def _list_paths(root: Path) -> list[str]:
    if (root / ".git").exists():
        try:
            out = subprocess.run(
                ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                cwd=root, capture_output=True, timeout=60, check=True,
            ).stdout
            return sorted({p for p in out.decode("utf-8", "replace").split("\0") if p})
        except (subprocess.SubprocessError, OSError):
            pass
    paths = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
        for f in filenames:
            paths.append(Path(dirpath, f).relative_to(root).as_posix())
    return sorted(paths)


def classify(path: str) -> str | None:
    p = PurePosixPath(path)
    name, lower = p.name, path.lower()
    parts = [s.lower() for s in p.parts[:-1]]
    if name in IGNORED_NAMES or lower.endswith(IGNORED_SUFFIXES):
        return None
    if name.upper().split(".")[0] in HOUSEKEEPING_STEMS:
        return None
    if lower.startswith(".github/") and not lower.startswith(".github/workflows/"):
        return None  # issue templates, dependabot, labeler, funding
    if name.startswith(".") and not name.startswith(".env") and "/" not in path:
        return None  # root dotfiles: .gitignore, .editorconfig, linter configs
    if any(part in IGNORED_DIRS for part in p.parts[:-1]):
        return None
    if lower.startswith((".github/workflows/", ".gitlab-ci", ".circleci/")):
        return "ci"
    if name in MANIFEST_NAMES or (name.startswith("requirements") and name.endswith(".txt")):
        return "manifest"
    if name.startswith(".env") and name != ".env":
        return "config"  # .env.example etc. document configuration; a real .env is never read
    if name == ".env":
        return None
    if lower.endswith(DOC_SUFFIXES) or name.upper().startswith(("LICENSE", "COPYING")):
        return "doc"
    stem = name.lower()
    if (
        any(part in {"test", "tests", "__tests__", "spec", "specs", "testdata", "fixtures"} for part in parts)
        or stem.startswith("test_")
        or ".test." in stem or ".spec." in stem or stem.endswith(("_test.go", "_test.py", "tests.rs"))
    ):
        return "test"
    if lower.endswith(CONFIG_SUFFIXES):
        return "config"
    return "source"


def _read_text(abs_path: Path) -> str | None:
    try:
        data = abs_path.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def walk(root: Path, settings: Settings) -> WalkResult:
    skipped: dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    all_paths = _list_paths(root)
    files: list[FileRecord] = []
    for rel in all_paths:
        if len(files) >= settings.max_files:
            skip("over max_files")
            continue
        abs_path = root / rel
        # Never follow symlinks: a hostile repo could point one at a host file.
        if abs_path.is_symlink() or not abs_path.is_file():
            skip("symlink or not a regular file")
            continue
        role = classify(rel)
        if role is None:
            skip("ignored (vendored, generated, binary type, or lockfile)")
            continue
        size = abs_path.stat().st_size
        if size > settings.max_file_bytes:
            skip("too large (likely generated)")
            continue
        if size == 0:
            skip("empty")
            continue
        text = _read_text(abs_path)
        if text is None:
            skip("binary or non-UTF-8")
            continue
        language = tslp.detect_language_from_path(rel)
        if role == "source" and (language is None or language in NON_CODE_LANGS):
            role = "config" if language in NON_CODE_LANGS else "source"
        files.append(FileRecord(rel, size, role, language, text))
    return WalkResult(files, skipped, all_paths)

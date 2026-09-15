"""Resolve a repo reference (GitHub URL or local path) to a checked-out directory."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .config import Settings

_GITHUB_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/"
    r"(?P<repo>[A-Za-z0-9._-]{1,100}?)"
    r"(?:\.git)?/?$"
)
_SHORTHAND_RE = re.compile(r"^(?P<owner>[A-Za-z0-9-]{1,39})/(?P<repo>[A-Za-z0-9._-]{1,100})$")


class FetchError(Exception):
    pass


@dataclass(frozen=True)
class RepoRef:
    owner: str
    name: str

    @property
    def clone_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.name}.git"

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True)
class Checkout:
    root: Path
    display_name: str
    commit: str | None
    source_url: str | None


def parse_github_url(value: str) -> RepoRef:
    value = value.strip()
    m = _GITHUB_RE.match(value) or _SHORTHAND_RE.match(value)
    if not m:
        raise FetchError(f"Not a GitHub repository URL: {value!r} (expected https://github.com/owner/repo)")
    repo = m.group("repo")
    if repo in {".", ".."}:
        raise FetchError(f"Invalid repository name: {repo!r}")
    return RepoRef(m.group("owner"), repo)


def _git(args: list[str], cwd: Path | None, timeout: int) -> str:
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",  # private/nonexistent repos fail instead of prompting
        "GIT_LFS_SKIP_SMUDGE": "1",
    }
    try:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError as e:
        raise FetchError("git is not installed or not on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise FetchError(f"git {args[0]} timed out after {timeout}s") from e
    if proc.returncode != 0:
        if args[0] == "clone" and re.search(r"not found|could not read Username|Authentication failed", proc.stderr):
            raise FetchError("Repository not found. Check the name, and note that private repositories aren't supported.")
        err = proc.stderr.strip().splitlines()
        tail = err[-1] if err else f"exit code {proc.returncode}"
        raise FetchError(f"git {args[0]} failed: {tail}")
    return proc.stdout


def _dir_bytes(root: Path) -> int:
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for f in filenames:
            p = os.path.join(dirpath, f)
            if not os.path.islink(p):
                total += os.path.getsize(p)
    return total


@contextmanager
def checkout(target: str, settings: Settings, *, allow_local: bool = True) -> Iterator[Checkout]:
    """Yield a checkout for `target`. Remote clones are deleted on exit."""
    local = Path(target).expanduser()
    if allow_local and local.is_dir():
        commit = None
        if (local / ".git").exists():
            try:
                commit = _git(["rev-parse", "HEAD"], local, 10).strip()
            except FetchError:
                pass
        yield Checkout(local.resolve(), local.resolve().name, commit, None)
        return

    ref = parse_github_url(target)
    tmp = Path(tempfile.mkdtemp(prefix="repo2readme-"))
    dest = tmp / ref.name
    try:
        _git(
            [
                "clone", "--depth", "1", "--single-branch", "--no-tags",
                "-c", "core.symlinks=false",
                ref.clone_url, str(dest),
            ],
            None,
            settings.clone_timeout_s,
        )
        size = _dir_bytes(dest)
        if size > settings.max_repo_bytes:
            raise FetchError(
                f"{ref.slug} is {size / 1e6:.0f} MB checked out; limit is {settings.max_repo_bytes / 1e6:.0f} MB"
            )
        commit = _git(["rev-parse", "HEAD"], dest, 10).strip()
        yield Checkout(dest, ref.slug, commit, f"https://github.com/{ref.slug}")
    finally:
        shutil.rmtree(tmp, onexc=_force_remove)


def _force_remove(func, path, _exc):  # git pack files are read-only on Windows
    try:
        os.chmod(path, 0o700)
        func(path)
    except OSError:
        pass

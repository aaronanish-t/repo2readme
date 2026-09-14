from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fakes import FakeLLM  # noqa: E402

FIXTURE_FILES = {
    "pyproject.toml": """\
[project]
name = "myapp"
description = "A tiny app for tests"
requires-python = ">=3.12"
dependencies = ["httpx>=0.27"]

[project.scripts]
myapp = "myapp.cli:main"
""",
    "LICENSE": "MIT License\n\nCopyright (c) 2026 Test\n",
    "Makefile": ".PHONY: test\ntest:\n\tpytest\n\nlint:\n\truff check .\n",
    "README.md": "# old readme that should not be paraphrased\n",
    "src/myapp/__init__.py": '"""myapp."""\n',
    "src/myapp/cli.py": """\
import os
import sys

from .core import Engine
from myapp.storage.db import Database


def main() -> int:
    token = os.environ["MYAPP_TOKEN"]
    engine = Engine(token, Database(os.getenv("MYAPP_DB_URL", "sqlite://")))
    return engine.run(sys.argv[1:])
""",
    "src/myapp/core.py": """\
class Engine:
    \"\"\"Runs jobs.\"\"\"

    def __init__(self, token: str, db) -> None:
        self.token = token
        self.db = db

    def run(self, args: list[str]) -> int:
        for name in args:
            self.db.save_job(name)
        return 0


def helper(x: int) -> int:
    return x * 2
""",
    "src/myapp/storage/__init__.py": "",
    "src/myapp/storage/db.py": """\
class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        self.rows: list[str] = []

    def save_job(self, name: str) -> None:
        self.rows.append(name)
""",
    "web/package.json": json.dumps({"name": "myapp-web", "scripts": {"dev": "vite", "build": "vite build"},
                                    "dependencies": {"vite": "^5"}}),
    "web/src/index.ts": 'import { format } from "./util";\nexport function start(port: number) {\n  console.log(format(port));\n}\n',
    "web/src/util.ts": "export function format(n: number): string {\n  return `port ${n}`;\n}\n",
    "tests/test_core.py": "from myapp.core import helper\n\n\ndef test_helper():\n    assert helper(2) == 4\n",
    "assets/logo.png": "\x89PNG\x00\x00binary",
    "node_modules/leftpad/index.js": "module.exports = 1\n",
    ".env": "SECRET=do-not-read\n",
}


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    root = tmp_path / "myapp"
    for rel, content in FIXTURE_FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return root


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()

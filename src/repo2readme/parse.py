"""Deterministic per-file facts from tree-sitter. These are the ground truth the model is checked against."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import tree_sitter_language_pack as tslp

from .walk import FileRecord

log = logging.getLogger(__name__)

PARSEABLE_ROLES = {"source", "test"}


@dataclass(frozen=True)
class Symbol:
    name: str
    qualname: str  # Class.method for nested definitions
    kind: str
    start_line: int  # 1-based, inclusive
    end_line: int
    signature: str


@dataclass(frozen=True)
class ImportRef:
    spec: str  # module specifier as written: "./lib/foo", ".core", "github.com/a/b"
    items: tuple[str, ...]
    line: int


@dataclass
class FileFacts:
    path: str
    language: str | None
    role: str
    line_count: int
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[ImportRef] = field(default_factory=list)
    exports: list[str] = field(default_factory=list)
    parse_error: str | None = None

    @property
    def top_level(self) -> list[Symbol]:
        return [s for s in self.symbols if "." not in s.qualname]


_QUOTED = re.compile(r"""["'`]([^"'`\s]+)["'`]""")
_PY_FROM = re.compile(r"^\s*from\s+([.\w]+)\s+import\s+(.+)$", re.S)
_PY_IMPORT = re.compile(r"^\s*import\s+(.+)$", re.S)
_JS_REQUIRE = re.compile(r"""\b(?:require|import)\s*\(\s*["']([^"']+)["']\s*\)""")
_KEYWORD_PATH = re.compile(r"^\s*(?:import|use|using|package)\s+(?:static\s+)?([\w.:\\]+)")


def _import_specs(language: str, statement: str) -> list[tuple[str, tuple[str, ...]]]:
    """Turn a raw import statement into (specifier, imported names) pairs."""
    stmt = statement.strip()
    if language == "python":
        if m := _PY_FROM.match(stmt):
            names = tuple(
                n.strip().split(" as ")[0].strip("() \n")
                for n in m.group(2).split(",")
                if n.strip().strip("()")
            )
            return [(m.group(1), names)]
        if m := _PY_IMPORT.match(stmt):
            return [(part.strip().split(" as ")[0].strip(), ()) for part in m.group(1).split(",") if part.strip()]
        return []
    if m := _QUOTED.search(stmt):
        return [(m.group(1), ())]
    if m := _KEYWORD_PATH.match(stmt):
        return [(m.group(1).rstrip(";"), ())]
    return []


def _flatten(items, prefix: str, out: list[Symbol]) -> None:
    for item in items:
        if not item.name:
            continue
        qual = f"{prefix}.{item.name}" if prefix else item.name
        out.append(
            Symbol(
                name=item.name,
                qualname=qual,
                kind=str(item.kind).split(".")[-1],
                start_line=item.span.start_line + 1,
                end_line=item.span.end_line + 1,
                signature=(item.signature or "").strip()[:200],
            )
        )
        _flatten(item.children or [], qual, out)


def extract(record: FileRecord) -> FileFacts:
    facts = FileFacts(record.path, record.language, record.role, record.line_count)
    if record.role not in PARSEABLE_ROLES or not record.language:
        return facts
    try:
        cfg = tslp.ProcessConfig(language=record.language, structure=True, imports=True, exports=True)
        result = tslp.process(record.text, cfg)
    except Exception as e:  # grammar download failure, unsupported language, parser crash
        facts.parse_error = f"{type(e).__name__}: {e}"[:200]
        log.debug("parse failed for %s: %s", record.path, facts.parse_error)
        return facts

    _flatten(result.structure or [], "", facts.symbols)
    for imp in result.imports or []:
        for spec, items in _import_specs(record.language, imp.source or ""):
            facts.imports.append(ImportRef(spec, items or tuple(imp.items or ()), imp.span.start_line + 1))
    if record.language in {"javascript", "typescript", "tsx", "jsx"}:
        seen = {i.spec for i in facts.imports}
        for m in _JS_REQUIRE.finditer(record.text):
            if m.group(1) not in seen:
                line = record.text.count("\n", 0, m.start()) + 1
                facts.imports.append(ImportRef(m.group(1), (), line))
    facts.exports = [e.name for e in (result.exports or []) if e.name]
    return facts

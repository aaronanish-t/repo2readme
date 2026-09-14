"""Resolve import specifiers to files inside the repo, producing a deterministic dependency graph.

Diagram edges come from here, never from the model, so the architecture diagram cannot invent a
dependency that the code doesn't have.
"""

from __future__ import annotations

import posixpath
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .parse import FileFacts

JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts", ".vue", ".svelte")
JS_LANGS = {"javascript", "typescript", "tsx", "jsx", "vue", "svelte"}
_GO_MODULE = re.compile(r"^module\s+(\S+)", re.M)


@dataclass
class RepoGraph:
    edges: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))  # importer -> imported
    unresolved_internal: int = 0

    def importers_of(self, path: str) -> set[str]:
        return {src for src, dsts in self.edges.items() if path in dsts}

    def in_degree(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for dsts in self.edges.values():
            for d in dsts:
                counts[d] += 1
        return counts

    def group_edges(self, assign: dict[str, str]) -> dict[tuple[str, str], int]:
        """Collapse file edges into edges between groups (e.g. diagram components). Self-loops dropped."""
        out: dict[tuple[str, str], int] = defaultdict(int)
        for src, dsts in self.edges.items():
            gs = assign.get(src)
            if gs is None:
                continue
            for d in dsts:
                gd = assign.get(d)
                if gd is not None and gd != gs:
                    out[(gs, gd)] += 1
        return dict(out)


class _Index:
    def __init__(self, paths: list[str]):
        self.paths = set(paths)
        self.dirs: dict[str, list[str]] = defaultdict(list)
        self.by_stem_suffix: dict[str, list[str]] = defaultdict(list)
        for p in paths:
            pp = PurePosixPath(p)
            self.dirs[str(pp.parent) if str(pp.parent) != "." else ""].append(p)
            no_ext = str(pp.with_suffix(""))
            parts = no_ext.split("/")
            for i in range(len(parts)):
                self.by_stem_suffix["/".join(parts[i:])].append(p)

    def first(self, *candidates: str) -> str | None:
        for c in candidates:
            c = posixpath.normpath(c).lstrip("/")
            if c in self.paths:
                return c
        return None

    def suffix(self, dotted: str) -> str | None:
        hits = self.by_stem_suffix.get(dotted)
        return hits[0] if hits and len(hits) == 1 else None


def _resolve_js(idx: _Index, importer: str, spec: str) -> str | None:
    base = posixpath.dirname(importer)
    if spec.startswith("."):
        target = posixpath.normpath(posixpath.join(base, spec))
    elif spec.startswith(("@/", "~/")):
        target = spec[2:]
        hit = _js_candidates(idx, f"src/{target}")
        if hit:
            return hit
    elif spec.startswith("/"):
        target = spec[1:]
    else:
        return None  # bare package specifier: external dependency
    return _js_candidates(idx, target)


def _js_candidates(idx: _Index, target: str) -> str | None:
    return idx.first(
        target,
        *(target + e for e in JS_EXTS),
        *(f"{target}/index{e}" for e in JS_EXTS),
        # TS ESM style: import "./foo.js" that actually points at foo.ts
        *(re.sub(r"\.(m|c)?js$", e, target) for e in (".ts", ".tsx", ".mts", ".cts")),
    )


def _python_roots(idx: _Index) -> list[str]:
    roots = [""]
    for candidate in ("src", "lib", "python"):
        if any(p.startswith(candidate + "/") for p in idx.paths):
            roots.append(candidate)
    return roots


def _resolve_python(idx: _Index, importer: str, spec: str, items: tuple[str, ...], roots: list[str]) -> list[str]:
    if spec.startswith("."):
        level = len(spec) - len(spec.lstrip("."))
        base = posixpath.dirname(importer)
        for _ in range(level - 1):
            base = posixpath.dirname(base)
        rest = spec[level:].replace(".", "/")
        mod_bases = [posixpath.join(base, rest) if rest else base]
    else:
        rest = spec.replace(".", "/")
        mod_bases = [posixpath.join(r, rest) if r else rest for r in roots]

    hits: list[str] = []
    for mb in mod_bases:
        mod = idx.first(mb + ".py", mb + "/__init__.py")
        # `from pkg import mod` may import submodules rather than names
        subs = [idx.first(f"{mb}/{it}.py", f"{mb}/{it}/__init__.py") for it in items if it and it != "*"]
        subs = [s for s in subs if s]
        if subs:
            hits.extend(subs)
        if mod and (not subs or len(subs) < len(items)):
            hits.append(mod)
        if hits:
            break
    return hits


def _resolve_go(idx: _Index, spec: str, module: str | None) -> list[str]:
    if not module or not (spec == module or spec.startswith(module + "/")):
        return []
    rel = spec[len(module):].lstrip("/")
    return [p for p in idx.dirs.get(rel, []) if p.endswith(".go") and not p.endswith("_test.go")][:1]


_RUST_MOD = re.compile(r"^\s*(?:pub(?:\([\w: ]+\))?\s+)?mod\s+(\w+)\s*;", re.M)
_CARGO_NAME = re.compile(r"^\[package\][^\[]*?^name\s*=\s*\"([^\"]+)\"", re.M | re.S)


def _rust_crates(all_texts: dict[str, str]) -> dict[str, str]:
    """Workspace crate name (as used in `use` paths) -> that crate's src directory."""
    crates = {}
    for path, text in all_texts.items():
        if PurePosixPath(path).name == "Cargo.toml" and (m := _CARGO_NAME.search(text)):
            crate_dir = posixpath.dirname(path)
            crates[m.group(1).replace("-", "_")] = posixpath.join(crate_dir, "src") if crate_dir else "src"
    return crates


def _rust_mod_edges(idx: _Index, importer: str, text: str) -> list[str]:
    """`mod foo;` pulls in foo.rs / foo/mod.rs relative to the declaring file."""
    d = posixpath.dirname(importer)
    stem = PurePosixPath(importer).stem
    base = d if stem in {"mod", "lib", "main"} else posixpath.join(d, stem)
    hits = []
    for m in _RUST_MOD.finditer(text):
        hit = idx.first(f"{base}/{m.group(1)}.rs", f"{base}/{m.group(1)}/mod.rs")
        if hit:
            hits.append(hit)
    return hits


def _resolve_rust(idx: _Index, importer: str, spec: str, crates: dict[str, str]) -> str | None:
    parts = [p for p in re.split(r"::", spec.split("{")[0]) if p and p != "*"]
    if not parts:
        return None
    if parts[0] in crates:
        base, parts = crates[parts[0]], parts[1:]
        if not parts:
            return idx.first(f"{base}/lib.rs", f"{base}/main.rs")
    elif parts[0] == "crate":
        crate_root = importer.split("src/")[0] + "src" if "src/" in importer else "src"
        base, parts = crate_root, parts[1:]
    elif parts[0] in {"super", "self"}:
        base = posixpath.dirname(importer)
        while parts and parts[0] in {"super", "self"}:
            if parts[0] == "super":
                base = posixpath.dirname(base)
            parts = parts[1:]
    else:
        return None
    # Longest module path that exists; trailing segments are items (types, fns).
    for n in range(len(parts), 0, -1):
        mb = posixpath.join(base, *parts[:n])
        hit = idx.first(mb + ".rs", mb + "/mod.rs")
        if hit:
            return hit
    # Items re-exported from the crate root (`use grep_regex::RegexMatcher`).
    if parts and base.endswith("src"):
        return idx.first(f"{base}/lib.rs", f"{base}/main.rs")
    return None


def build_graph(facts: dict[str, FileFacts], all_texts: dict[str, str]) -> RepoGraph:
    idx = _Index(list(facts))
    graph = RepoGraph()
    roots = _python_roots(idx)
    crates = _rust_crates(all_texts)
    go_module = None
    if "go.mod" in all_texts and (m := _GO_MODULE.search(all_texts["go.mod"])):
        go_module = m.group(1)

    for path, f in facts.items():
        lang = f.language or ""
        for imp in f.imports:
            spec = imp.spec
            targets: list[str] = []
            if lang in JS_LANGS:
                t = _resolve_js(idx, path, spec)
                targets = [t] if t else []
                if not t and spec.startswith("."):
                    graph.unresolved_internal += 1
            elif lang == "python":
                targets = _resolve_python(idx, path, spec, imp.items, roots)
                if not targets and spec.startswith("."):
                    graph.unresolved_internal += 1
            elif lang == "go":
                targets = _resolve_go(idx, spec, go_module)
            elif lang == "rust":
                t = _resolve_rust(idx, path, spec, crates)
                targets = [t] if t else []
            elif lang in {"c", "cpp", "objc"}:
                t = idx.first(posixpath.join(posixpath.dirname(path), spec), spec, "include/" + spec)
                targets = [t] if t else []
            else:
                # Java/Kotlin/C#/Scala/PHP/Ruby: dotted or slashed path matched against unique file stems.
                dotted = re.sub(r"[.\\:]+", "/", spec.strip("'\"; "))
                t = idx.suffix(dotted)
                targets = [t] if t else []
            for t in targets:
                if t != path:
                    graph.edges[path].add(t)
        if lang == "rust" and path in all_texts:
            for t in _rust_mod_edges(idx, path, all_texts[path]):
                graph.edges[path].add(t)
    return graph

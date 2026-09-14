"""Mermaid architecture diagram: nodes chosen by the model, edges computed from the import graph."""

from __future__ import annotations

import re

from .graph import RepoGraph
from .schemas import Component


def assign_files(components: list[Component], files: list[str]) -> dict[str, str]:
    """Map each file to the component whose path is the longest prefix match."""
    prefixes = sorted(
        ((p.strip("/"), c.id) for c in components for p in c.paths),
        key=lambda pc: -len(pc[0]),
    )
    out: dict[str, str] = {}
    for f in files:
        for prefix, cid in prefixes:
            if prefix == "" or f == prefix or f.startswith(prefix + "/"):
                out[f] = cid
                break
    return out


def _node_id(raw: str, used: set[str]) -> str:
    base = re.sub(r"\W+", "_", raw).strip("_") or "component"
    if base[0].isdigit():
        base = "c_" + base
    nid, n = base, 2
    while nid in used:
        nid, n = f"{base}_{n}", n + 1
    used.add(nid)
    return nid


def _label(text: str) -> str:
    return text.replace('"', "'").replace("\n", " ").strip()[:60]


def build_mermaid(components: list[Component], graph: RepoGraph, files: list[str]) -> tuple[str, list[tuple[str, str, int]]]:
    used: set[str] = set()
    ids = {c.id: _node_id(c.id, used) for c in components}
    assignment = assign_files(components, files)
    edges = sorted(graph.group_edges(assignment).items(), key=lambda kv: (-kv[1], kv[0]))

    lines = ["flowchart LR"]
    for c in components:
        lines.append(f'    {ids[c.id]}["{_label(c.label)}"]')
    for (src, dst), count in edges:
        lines.append(f"    {ids[src]} -->|{count}| {ids[dst]}")
    return "\n".join(lines), [(src, dst, n) for (src, dst), n in edges]

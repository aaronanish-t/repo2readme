"""Pack files into map-pass chunks.

Rules:
- Files are never silently truncated. A file too big for one chunk is split at top-level definition
  boundaries (from tree-sitter), and each part is labelled with its line range.
- When the repo exceeds the chunk budget, the lowest-priority files are *not* sent to the model; they
  are represented by their deterministic skeleton (symbols + imports) and listed in the report.
- Chunks are packed in path order so each call sees neighbouring files from the same directory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .parse import FileFacts
from .walk import FileRecord

ENTRYPOINT_STEMS = {"main", "__main__", "index", "app", "server", "cli", "lib", "mod", "cmd", "manage", "wsgi", "asgi"}
ROLE_WEIGHT = {"source": 100, "manifest": 60, "test": 20, "doc": 15, "config": 12, "ci": 5}


def estimate_tokens(text: str) -> int:
    # Conservative for code (dense punctuation). Used only for packing, never for billing.
    return len(text) // 3 + 1


@dataclass
class Segment:
    path: str
    start_line: int
    end_line: int
    total_lines: int
    body: str  # rendered with line numbers

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.body) + 20


@dataclass
class Chunk:
    id: int
    segments: list[Segment] = field(default_factory=list)

    @property
    def tokens(self) -> int:
        return sum(s.tokens for s in self.segments)

    @property
    def paths(self) -> list[str]:
        return list(dict.fromkeys(s.path for s in self.segments))

    def render(self) -> str:
        parts = []
        for s in self.segments:
            span = "" if (s.start_line == 1 and s.end_line == s.total_lines) else f", lines {s.start_line}-{s.end_line}"
            parts.append(f'<file path="{s.path}" total_lines="{s.total_lines}"{span}>\n{s.body}\n</file>')
        return "\n\n".join(parts)


@dataclass
class ChunkPlan:
    chunks: list[Chunk]
    skeleton_only: list[str]  # paths represented by symbols only (over budget)


def priority(record: FileRecord, facts: FileFacts, in_degree: int) -> float:
    p = PurePosixPath(record.path)
    score = ROLE_WEIGHT.get(record.role, 0)
    if p.stem.lower() in ENTRYPOINT_STEMS:
        score += 40
    if record.path.count("/") == 0 and record.role == "source":
        score += 15
    if record.path.startswith(("cmd/", "bin/", "src/bin/")):
        score += 25
    score += min(in_degree, 12) * 4
    score -= record.path.count("/") * 3
    if record.role == "doc" and p.name.lower() == "readme.md" and record.path.count("/") == 0:
        score = -1  # the existing README may be stale; never let the model paraphrase it
    if p.name.upper().startswith(("LICENSE", "COPYING", "CHANGELOG", "HISTORY", "CHANGES")):
        score = -1  # license is detected deterministically; changelogs are long and historical
    return score


def _numbered(lines: list[str], start: int) -> str:
    width = len(str(start + len(lines)))
    return "\n".join(f"{i:>{width}}| {line}" for i, line in enumerate(lines, start=start))


def split_file(record: FileRecord, facts: FileFacts, budget: int) -> list[Segment]:
    lines = record.text.splitlines()
    total = len(lines)
    whole = Segment(record.path, 1, total, total, _numbered(lines, 1))
    if whole.tokens <= budget:
        return [whole]

    # Per-line cost including the "NNN| " prefix, as prefix sums: cost(a..b) = prefix[b] - prefix[a-1].
    width = len(str(total + 1))
    prefix = [0]
    for line in lines:
        prefix.append(prefix[-1] + (len(line) + width + 3) / 3)

    def cost(a: int, b: int) -> float:
        return prefix[b] - prefix[a - 1] + 21

    target = budget * 0.9
    segments: list[Segment] = []

    def emit(a: int, b: int) -> None:  # inclusive, 1-based
        segments.append(Segment(record.path, a, b, total, _numbered(lines[a - 1 : b], a)))

    def emit_windows(a: int, b: int) -> None:
        # A single definition bigger than the budget: fall back to line windows.
        s = a
        while s <= b:
            e = s
            while e < b and cost(s, e + 1) <= target:
                e += 1
            emit(s, e)
            s = e + 1

    # Cut only where a top-level definition starts, packing as many definitions as fit.
    cuts = sorted({s.start_line for s in facts.top_level if 1 < s.start_line <= total})
    boundaries = [1, *cuts, total + 1]
    seg_start = 1
    for i in range(1, len(boundaries)):
        nxt = boundaries[i]  # candidate segment [seg_start, nxt - 1]
        if cost(seg_start, nxt - 1) <= target:
            continue
        prev = boundaries[i - 1]
        if prev > seg_start:
            emit(seg_start, prev - 1)
            seg_start = prev
        if cost(seg_start, nxt - 1) > target:
            emit_windows(seg_start, nxt - 1)
            seg_start = nxt
    if seg_start <= total:
        emit(seg_start, total) if cost(seg_start, total) <= target else emit_windows(seg_start, total)
    return segments


def plan_chunks(
    records: list[FileRecord],
    facts: dict[str, FileFacts],
    in_degree: dict[str, int],
    *,
    chunk_tokens: int,
    max_chunks: int,
) -> ChunkPlan:
    eligible = [r for r in records if r.role in {"source", "test", "doc", "config", "manifest"}]
    ranked = sorted(eligible, key=lambda r: -priority(r, facts[r.path], in_degree.get(r.path, 0)))

    capacity = int(chunk_tokens * max_chunks * 0.9)
    # Tests and docs help, but must not crowd out source: cap their share of what source leaves over.
    segments = {r.path: split_file(r, facts[r.path], chunk_tokens) for r in ranked}
    source_cost = sum(
        sum(s.tokens for s in segments[r.path]) for r in ranked if r.role in {"source", "manifest"}
    )
    role_caps = {
        "test": max(chunk_tokens, int(min(capacity, source_cost) * 0.25)),
        "doc": max(chunk_tokens // 2, int(min(capacity, source_cost) * 0.10)),
    }
    role_used: dict[str, int] = {}
    selected: dict[str, list[Segment]] = {}
    skeleton_only: list[str] = []
    used = 0
    for r in ranked:
        if priority(r, facts[r.path], in_degree.get(r.path, 0)) < 0:
            skeleton_only.append(r.path)
            continue
        segs = segments[r.path]
        cost = sum(s.tokens for s in segs)
        over_role = r.role in role_caps and role_used.get(r.role, 0) + cost > role_caps[r.role]
        if used + cost > capacity or over_role:
            skeleton_only.append(r.path)
            continue
        selected[r.path] = segs
        used += cost
        role_used[r.role] = role_used.get(r.role, 0) + cost

    chunks: list[Chunk] = []
    current = Chunk(0)
    for path in sorted(selected):
        for seg in selected[path]:
            if current.segments and current.tokens + seg.tokens > chunk_tokens:
                chunks.append(current)
                current = Chunk(len(chunks))
            current.segments.append(seg)
    if current.segments:
        chunks.append(current)
    return ChunkPlan(chunks, sorted(skeleton_only))

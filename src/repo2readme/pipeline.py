"""End-to-end orchestration.

    fetch -> walk -> tree-sitter facts -> import graph -> manifest facts -> chunk plan     (deterministic)
    MAP:    each chunk -> per-file summaries -> verified against source lines            (pass 1)
    REDUCE: verified summaries (+ module summaries if too big) -> README draft           (pass 2)
    VERIFY: draft paths/symbols/commands checked -> repair round -> diagram from imports -> markdown
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable

import anthropic

from . import prompts
from .chunk import Chunk, ChunkPlan, estimate_tokens, plan_chunks
from .config import Settings
from .diagram import build_mermaid
from .fetch import checkout
from .graph import RepoGraph, build_graph
from .llm import LLMError, StructuredLLM, TruncatedError
from .manifests import RepoFacts, extract_repo_facts
from .parse import FileFacts, extract
from .render import render_readme
from .schemas import ChunkSummary, FileSummary, ModuleBatch, ModuleSummary, ReadmeDraft
from .verify import Issue, MapStats, RepoIndex, merge_parts, verify_draft, verify_file_summary
from .walk import WalkResult, walk

log = logging.getLogger(__name__)

MAP_MAX_TOKENS = 32_000
MODULE_MAX_TOKENS = 32_000
SYNTH_MAX_TOKENS = 64_000
WRAPPER_DIRS = {"src", "lib", "packages", "apps", "cmd", "internal", "pkg", "services", "crates", "modules"}
# Errors that mean the whole run is misconfigured; retrying other chunks would fail the same way.
FATAL_API_ERRORS = (
    anthropic.AuthenticationError,
    anthropic.PermissionDeniedError,
    anthropic.NotFoundError,
    anthropic.BadRequestError,
)


@dataclass
class Progress:
    stage: str
    done: int
    total: int
    message: str = ""


ProgressFn = Callable[[Progress], None]


def _noop(_: Progress) -> None:
    pass


@dataclass
class Prepared:
    name: str
    commit: str | None
    source_url: str | None
    walk: WalkResult
    texts: dict[str, str]
    facts: dict[str, FileFacts]
    graph: RepoGraph
    repo_facts: RepoFacts
    plan: ChunkPlan
    index: RepoIndex
    context: str
    timings: dict[str, float] = field(default_factory=dict)


@dataclass
class Result:
    readme: str
    draft: ReadmeDraft
    report: dict


class PipelineError(Exception):
    pass


# ---------------------------------------------------------------- deterministic stages


def prepare_checkout(root: Path, name: str, commit: str | None, source_url: str | None,
                     settings: Settings, progress: ProgressFn = _noop) -> Prepared:
    t0 = time.perf_counter()
    progress(Progress("walk", 0, 1, "listing files"))
    walked = walk(root, settings)
    if not walked.files:
        raise PipelineError("No readable text files found in the repository")
    texts = {f.path: f.text for f in walked.files}
    progress(Progress("walk", 1, 1, f"{len(walked.files)} readable files"))
    t_walk = time.perf_counter()

    facts: dict[str, FileFacts] = {}
    total = len(walked.files)
    for i, record in enumerate(walked.files):
        facts[record.path] = extract(record)
        if i % 200 == 0 or i == total - 1:
            progress(Progress("parse", i + 1, total, record.path))
    t_parse = time.perf_counter()

    graph = build_graph(facts, texts)
    loc: dict[str, int] = defaultdict(int)
    for f in walked.files:
        if f.role == "source" and f.language:
            loc[f.language] += f.line_count
    repo_facts = extract_repo_facts(walked.files, walked.tree, dict(loc))
    plan = plan_chunks(
        walked.files, facts, graph.in_degree(),
        chunk_tokens=settings.map_chunk_tokens, max_chunks=settings.max_map_chunks,
    )
    index = RepoIndex(texts, facts, repo_facts, walked.tree)
    context = prompts.repo_context_block(name, prompts.render_tree(walked.tree), repo_facts.to_prompt())
    progress(Progress("plan", 1, 1, f"{len(plan.chunks)} chunks, {len(plan.skeleton_only)} files as skeletons"))
    return Prepared(
        name, commit, source_url, walked, texts, facts, graph, repo_facts, plan, index, context,
        timings={"walk_s": t_walk - t0, "parse_s": t_parse - t_walk, "plan_s": time.perf_counter() - t_parse},
    )


def prepare(target: str, settings: Settings, progress: ProgressFn = _noop, *, allow_local: bool = True) -> Prepared:
    progress(Progress("fetch", 0, 1, f"fetching {target}"))
    with checkout(target, settings, allow_local=allow_local) as co:
        progress(Progress("fetch", 1, 1, co.commit[:12] if co.commit else "local directory"))
        return prepare_checkout(co.root, co.display_name, co.commit, co.source_url, settings, progress)


def plan_summary(p: Prepared, settings: Settings) -> dict:
    context_tokens = estimate_tokens(p.context)
    map_in = sum(c.tokens for c in p.plan.chunks) + context_tokens * len(p.plan.chunks)
    return {
        "repository": p.name,
        "commit": p.commit,
        "files_discovered": len(p.walk.tree),
        "files_read": len(p.walk.files),
        "files_skipped": p.walk.skipped,
        "files_with_symbols": sum(1 for f in p.facts.values() if f.symbols),
        "parse_errors": sum(1 for f in p.facts.values() if f.parse_error),
        "languages_loc": p.repo_facts.languages,
        "internal_import_edges": sum(len(v) for v in p.graph.edges.values()),
        "map_chunks": len(p.plan.chunks),
        "files_sent_to_model": sum(len(c.paths) for c in p.plan.chunks),
        "files_skeleton_only": len(p.plan.skeleton_only),
        "skeleton_only_sample": p.plan.skeleton_only[:50],
        "estimated_map_input_tokens": map_in,
        "context_tokens": context_tokens,
        "timings": {k: round(v, 2) for k, v in p.timings.items()},
    }


# ---------------------------------------------------------------- map pass


def _map_user(chunk: Chunk, facts: dict[str, FileFacts]) -> str:
    lines = ["## Symbols per file (from the parser; key_symbols must come from these)"]
    ranges = _chunk_ranges(chunk)
    for path in chunk.paths:
        syms = [
            f"{s.kind} `{s.qualname}` (L{s.start_line}-{s.end_line})"
            for s in facts[path].symbols
            if any(a <= s.start_line <= b for a, b in ranges[path])
        ]
        lines.append(f"- `{path}`: " + ("; ".join(syms[:80]) if syms else "(no symbols extracted)"))
    lines += ["", "## Files", chunk.render(), "", f"Return one entry for each of these paths: {json.dumps(chunk.paths)}"]
    return "\n".join(lines)


def _chunk_ranges(chunk: Chunk) -> dict[str, list[tuple[int, int]]]:
    ranges: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for s in chunk.segments:
        ranges[s.path].append((s.start_line, s.end_line))
    return ranges


async def map_pass(p: Prepared, llm: StructuredLLM, settings: Settings, progress: ProgressFn,
                   stats: MapStats, failures: list[dict]) -> dict[str, FileSummary]:
    system = prompts.system_blocks(prompts.MAP_INSTRUCTIONS, p.context)
    sem = asyncio.Semaphore(settings.concurrency)
    parts: dict[str, list[FileSummary]] = defaultdict(list)
    done = 0
    total = len(p.plan.chunks)

    async def run(chunk: Chunk, depth: int = 0) -> None:
        nonlocal done
        try:
            async with sem:
                out = await llm.generate(
                    stage="map", system=system, user=_map_user(chunk, p.facts), schema=ChunkSummary,
                    effort=settings.map_effort, max_tokens=MAP_MAX_TOKENS,
                )
        except TruncatedError as e:
            if len(chunk.segments) > 1 and depth < 3:
                mid = len(chunk.segments) // 2
                await asyncio.gather(
                    run(Chunk(chunk.id, chunk.segments[:mid]), depth + 1),
                    run(Chunk(chunk.id, chunk.segments[mid:]), depth + 1),
                )
                return
            failures.append({"chunk": chunk.id, "paths": chunk.paths, "error": str(e)})
            return
        except FATAL_API_ERRORS:
            raise
        except (LLMError, anthropic.APIError) as e:
            failures.append({"chunk": chunk.id, "paths": chunk.paths, "error": f"{type(e).__name__}: {e}"[:300]})
            return
        finally:
            if depth == 0:
                done += 1
                progress(Progress("map", done, total, f"chunk {chunk.id + 1}"))

        allowed = set(chunk.paths)
        ranges = _chunk_ranges(chunk)
        for summary in out.files:
            verified = verify_file_summary(summary, allowed, p.index, ranges, stats)
            if verified:
                parts[verified.path].append(verified)

    chunks = p.plan.chunks
    if chunks:
        # First call alone so the shared repo context is written to the prompt cache before fan-out.
        await run(chunks[0])
        await asyncio.gather(*(run(c) for c in chunks[1:]))
    if chunks and not parts:
        detail = failures[0]["error"] if failures else "no summaries survived verification"
        raise PipelineError(f"Map pass produced nothing usable ({detail})")
    return {path: merge_parts(ps) for path, ps in parts.items()}


# ---------------------------------------------------------------- reduce pass


def module_of(path: str) -> str:
    dirs = PurePosixPath(path).parts[:-1]
    if not dirs:
        return "(root)"
    depth = 2 if dirs[0] in WRAPPER_DIRS and len(dirs) >= 2 else 1
    return "/".join(dirs[:depth])


def _render_file(s: FileSummary) -> str:
    head = f"- `{s.path}`{' [entrypoint]' if s.is_entrypoint else ''}: {s.purpose}"
    lines = [head]
    if s.key_symbols:
        lines.append("  - symbols: " + "; ".join(f"`{k.name}`: {k.role}" for k in s.key_symbols[:15]))
    for f in s.facts[:15]:
        lines.append(f"  - {f.statement} ({s.path}:{f.start_line}-{f.end_line})")
    return "\n".join(lines)


def _module_sections(p: Prepared, summaries: dict[str, FileSummary], failed_paths: set[str]) -> dict[str, str]:
    by_module: dict[str, list[str]] = defaultdict(list)
    skeletons: dict[str, list[str]] = defaultdict(list)
    for path in sorted(p.facts):
        f = p.facts[path]
        if path in summaries:
            by_module[module_of(path)].append(_render_file(summaries[path]))
        elif (path in failed_paths or path in p.plan.skeleton_only) and f.role in {"source", "manifest"}:
            names = ", ".join(f"`{s.qualname}`" for s in f.top_level[:8])
            skeletons[module_of(path)].append(f"- `{path}` (not read in full; defines: {names or 'no symbols'})")

    module_edges = p.graph.group_edges({path: module_of(path) for path in p.facts})
    sections: dict[str, str] = {}
    for mod in sorted(set(by_module) | set(skeletons)):
        lines = [f"### Module `{mod}`"]
        deps = sorted((d, n) for (s, d), n in module_edges.items() if s == mod)
        if deps:
            lines.append("Imports from: " + ", ".join(f"`{d}` ({n})" for d, n in deps))
        lines += by_module.get(mod, [])
        sk = skeletons.get(mod, [])
        lines += sk[:15]
        if len(sk) > 15:
            lines.append(f"- (+{len(sk) - 15} more files not read)")
        sections[mod] = "\n".join(lines)
    return sections


def _batches(sections: dict[str, str], budget: int) -> list[list[tuple[str, str]]]:
    batches: list[list[tuple[str, str]]] = [[]]
    used = 0
    for mod, text in sections.items():
        pieces = [text]
        if estimate_tokens(text) > budget:  # one enormous module: split its lines
            pieces, cur = [], []
            for line in text.splitlines():
                cur.append(line)
                if estimate_tokens("\n".join(cur)) > budget * 0.9:
                    pieces.append("\n".join(cur))
                    cur = [f"### Module `{mod}` (continued)"]
            pieces.append("\n".join(cur))
        for piece in pieces:
            t = estimate_tokens(piece)
            if batches[-1] and used + t > budget:
                batches.append([])
                used = 0
            batches[-1].append((mod, piece))
            used += t
    return [b for b in batches if b]


async def module_pass(p: Prepared, llm: StructuredLLM, settings: Settings, progress: ProgressFn,
                      sections: dict[str, str]) -> dict[str, ModuleSummary]:
    system = prompts.system_blocks(prompts.MODULE_INSTRUCTIONS, p.context)
    batches = _batches(sections, settings.reduce_input_tokens // 2)
    sem = asyncio.Semaphore(settings.concurrency)
    results: dict[str, list[ModuleSummary]] = defaultdict(list)
    done = 0

    async def run(batch: list[tuple[str, str]]) -> None:
        nonlocal done
        mods = list(dict.fromkeys(m for m, _ in batch))
        user = "\n\n".join(t for _, t in batch) + f"\n\nReturn one entry for each module: {json.dumps(mods)}"
        async with sem:
            out = await llm.generate(
                stage="reduce_modules", system=system, user=user, schema=ModuleBatch,
                effort=settings.reduce_effort, max_tokens=MODULE_MAX_TOKENS,
            )
        for m in out.modules:
            if m.path in mods:
                key_files = [k for k in m.key_files if p.index.path_exists(k)]
                results[m.path].append(m.model_copy(update={"key_files": key_files}))
        done += 1
        progress(Progress("reduce_modules", done, len(batches)))

    await asyncio.gather(*(run(b) for b in batches))
    merged = {}
    for mod, ms in results.items():
        merged[mod] = ModuleSummary(
            path=mod,
            summary=" ".join(m.summary for m in ms),
            responsibilities=[r for m in ms for r in m.responsibilities],
            key_files=list(dict.fromkeys(k for m in ms for k in m.key_files)),
        )
    return merged


def _synthesis_input(p: Prepared, summaries: dict[str, FileSummary], sections: dict[str, str],
                     modules: dict[str, ModuleSummary] | None, budget: int) -> str:
    edges = p.graph.group_edges({path: module_of(path) for path in p.facts})
    lines = ["## Module import graph (computed from source; `A -> B (n)` means n files in A import B)"]
    lines += [f"- `{s}` -> `{d}` ({n})" for (s, d), n in sorted(edges.items())] or ["- (no internal imports resolved)"]
    entry = [s for s in summaries.values() if s.is_entrypoint]
    if entry:
        lines += ["", "## Entrypoints", *(_render_file(s) for s in entry[:20])]

    if modules is None:
        lines += ["", "## Verified file summaries by module", *sections.values()]
    else:
        lines += ["", "## Module summaries"]
        for mod in sorted(modules):
            m = modules[mod]
            lines.append(f"### `{mod}`\n{m.summary}\n" + "\n".join(f"- {r}" for r in m.responsibilities))
            if m.key_files:
                lines.append("Key files: " + ", ".join(f"`{k}`" for k in m.key_files))
        # Spend remaining budget on concrete facts, entrypoint modules first.
        remaining = budget - estimate_tokens("\n".join(lines))
        extra = ["", "## Selected file facts"]
        for s in sorted(summaries.values(), key=lambda s: (not s.is_entrypoint, s.path)):
            block = _render_file(s)
            if estimate_tokens(block) > remaining:
                break
            extra.append(block)
            remaining -= estimate_tokens(block)
        lines += extra
    return "\n".join(lines)


async def synthesize(p: Prepared, llm: StructuredLLM, settings: Settings, progress: ProgressFn,
                     summaries: dict[str, FileSummary], failed_paths: set[str]) -> tuple[ReadmeDraft, list[Issue], dict]:
    sections = _module_sections(p, summaries, failed_paths)
    direct = estimate_tokens("\n".join(sections.values())) <= settings.reduce_input_tokens
    modules = None if direct else await module_pass(p, llm, settings, progress, sections)
    user = _synthesis_input(p, summaries, sections, modules, settings.reduce_input_tokens)
    system = prompts.system_blocks(prompts.SYNTHESIS_INSTRUCTIONS, p.context)

    progress(Progress("synthesize", 0, 1, "writing README draft"))
    draft = await llm.generate(stage="synthesize", system=system, user=user, schema=ReadmeDraft,
                               effort=settings.reduce_effort, max_tokens=SYNTH_MAX_TOKENS)
    draft, dropped, issues = verify_draft(draft, p.index)
    progress(Progress("synthesize", 1, 1, f"{len(dropped)} entries dropped, {len(issues)} mentions unverified"))

    rounds = 0
    for _ in range(settings.repair_rounds):
        if not issues:
            break
        rounds += 1
        progress(Progress("repair", 0, 1, f"fixing {len(issues)} mentions"))
        repair_user = (
            f"{user}\n\n## Your previous draft\n{draft.model_dump_json(indent=1)}\n\n"
            f"{prompts.REPAIR_INSTRUCTIONS}\n\n## Flagged items\n" + "\n".join(i.render() for i in issues)
        )
        try:
            candidate = await llm.generate(stage="repair", system=system, user=repair_user, schema=ReadmeDraft,
                                           effort=settings.reduce_effort, max_tokens=SYNTH_MAX_TOKENS)
        except FATAL_API_ERRORS:
            raise
        except (LLMError, anthropic.APIError) as e:
            log.warning("repair round failed: %s", e)
            break
        candidate, new_dropped, new_issues = verify_draft(candidate, p.index)
        progress(Progress("repair", 1, 1, f"{len(new_issues)} mentions remain"))
        if len(new_issues) <= len(issues):
            draft, issues = candidate, new_issues
            dropped = dropped + [d for d in new_dropped if vars(d) not in [vars(x) for x in dropped]]

    meta = {
        "reduce_mode": "direct" if direct else f"hierarchical ({len(modules or {})} module summaries)",
        "repair_rounds": rounds,
        "synthesis_input_tokens_est": estimate_tokens(user),
        "dropped_from_draft": [vars(d) for d in dropped],
    }
    return draft, issues, meta


# ---------------------------------------------------------------- entrypoint


async def generate_from_prepared(p: Prepared, llm: StructuredLLM, settings: Settings,
                                 progress: ProgressFn = _noop) -> Result:
    t0 = time.perf_counter()
    stats = MapStats()
    failures: list[dict] = []
    summaries = await map_pass(p, llm, settings, progress, stats, failures)
    failed_paths = {path for f in failures for path in f["paths"]}
    draft, issues, meta = await synthesize(p, llm, settings, progress, summaries, failed_paths)

    mermaid, edges = build_mermaid(draft.components, p.graph, list(p.facts))
    readme = render_readme(
        draft, mermaid, has_edges=bool(edges), license_name=p.repo_facts.license,
        commit=p.commit, source_url=p.source_url,
    )
    report = {
        **plan_summary(p, settings),
        "mode": "model",
        "map_verification": {k: v for k, v in vars(stats).items()},
        "map_failures": failures,
        **meta,
        "unverified_mentions": [vars(i) for i in issues],
        "diagram_edges": [{"from": s, "to": d, "import_edges": n} for s, d, n in edges],
        "usage": llm.usage.to_dict(),
        "llm_seconds": round(time.perf_counter() - t0, 1),
    }
    progress(Progress("done", 1, 1, "README ready"))
    return Result(readme, draft, report)


def generate_without_model(p: Prepared, settings: Settings, progress: ProgressFn = _noop) -> Result:
    """Facts-only README: no model calls, used when no credentials are configured."""
    from .facts_only import facts_only_draft

    progress(Progress("synthesize", 0, 1, "building README from extracted facts"))
    draft, dropped, issues = verify_draft(facts_only_draft(p), p.index)
    progress(Progress("synthesize", 1, 1, "built from extracted facts"))
    mermaid, edges = build_mermaid(draft.components, p.graph, list(p.facts))
    readme = render_readme(
        draft, mermaid, has_edges=bool(edges), license_name=p.repo_facts.license,
        commit=p.commit, source_url=p.source_url,
    )
    report = {
        **plan_summary(p, settings),
        "mode": "facts-only",
        "map_chunks": 0,
        "files_sent_to_model": 0,
        "map_verification": vars(MapStats()),
        "map_failures": [],
        "reduce_mode": "none (no model)",
        "repair_rounds": 0,
        "dropped_from_draft": [vars(d) for d in dropped],
        "unverified_mentions": [vars(i) for i in issues],
        "diagram_edges": [{"from": s, "to": d, "import_edges": n} for s, d, n in edges],
        "usage": {"model": None, "stages": {}, "estimated_cost_usd": 0.0},
    }
    progress(Progress("done", 1, 1, "README ready (facts only)"))
    return Result(readme, draft, report)


def has_model_credentials() -> bool:
    import os

    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


async def generate(target: str, llm: StructuredLLM | None, settings: Settings, progress: ProgressFn = _noop,
                   *, allow_local: bool = True) -> Result:
    """Full pipeline; with `llm=None`, a facts-only README without model calls."""
    prepared = await asyncio.to_thread(prepare, target, settings, progress, allow_local=allow_local)
    if llm is None:
        return generate_without_model(prepared, settings, progress)
    return await generate_from_prepared(prepared, llm, settings, progress)

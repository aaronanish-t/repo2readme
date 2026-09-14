"""Prompt text for each stage. Kept separate so it can be tuned without touching orchestration."""

from __future__ import annotations

from collections import defaultdict
from pathlib import PurePosixPath

MAP_INSTRUCTIONS = """\
You are reading one batch of files from a software repository so that an accurate README can be \
written later. Your summaries are the only view of this code that later stages will get, and every \
statement you make is machine-checked against the source.

For each <file> in the batch, return exactly one entry with its path copied verbatim.
- purpose: what the file is for, based on what the code does, not on names alone.
- key_symbols: the definitions that matter to someone learning the project, chosen only from that \
file's listed symbols, with names copied exactly.
- facts: concrete behaviours a README would mention: entrypoints, CLI commands and flags, HTTP routes, \
environment variables or config it reads, external services, storage, notable algorithms or \
invariants. Each fact cites the line range that shows it. Put identifiers, string literals, routes and \
paths in backticks, spelled exactly as they appear inside the cited lines; a fact whose backticked \
text is not in its cited lines is discarded.
- If a file is shown only in part (a line range), describe only that part.
- Skip facts you would have to guess at. An empty facts list is better than a speculative one.

Tests, fixtures and docs matter only for what they reveal about the project's behaviour or how it is \
run; keep their entries short."""

MODULE_INSTRUCTIONS = """\
You are condensing verified per-file summaries of a repository into one summary per module \
(directory). Use only the information provided. For each module given, return one entry with its \
path copied verbatim, a summary of its responsibility, its main responsibilities, and its most \
important files (paths copied exactly from the input). Preserve concrete details that a README would \
need: commands, routes, environment variables, entrypoints, public APIs."""

SYNTHESIS_INSTRUCTIONS = """\
You are writing the README for a software repository from verified facts extracted from its code. \
Readers will act on this README, so it must be accurate before it is anything else.

Grounding rules (enforced by an automated checker after you respond):
- Everything you state must be supported by the manifest facts, module summaries, or file summaries \
provided. If something is unknown (for example, how to deploy), leave it out rather than inventing \
a plausible default.
- Put identifiers, file paths, commands, env vars and config keys in backticks, spelled exactly as \
they appear in the input. Backticked text that can't be found in the repository is flagged.
- Installation and development commands must come from the manifest facts: declared scripts, make \
targets, installed commands, the package managers detected. Standard commands of a detected package \
manager (e.g. `pip install -e .` for a pyproject, `npm install` for a package.json) are fine.
- Component paths, feature evidence paths, and project structure paths must be exact repository \
paths from the input. Components should partition the important code, so each module or file \
belongs to at most one component; the architecture diagram's arrows are computed from real import \
edges between the paths you assign, so choose boundaries that make those edges meaningful.
- Configuration entries must be environment variables or config keys that appear in the facts.

Style: direct and specific, for a developer evaluating or onboarding onto the project. The overview \
says what the project does and how it works, in the project's own terms. Features name concrete \
capabilities, not adjectives. Do not write marketing language or badges, and do not mention that \
the README was generated."""

REPAIR_INSTRUCTIONS = """\
An automated checker compared your README draft against the repository and could not verify some \
of it. The problems are listed below. Return the complete corrected draft: fix each flagged item using \
the facts provided (correct the spelling or path, or remove the statement if the facts don't support \
it). Leave everything that was not flagged unchanged."""


def render_tree(paths: list[str], max_lines: int = 250) -> str:
    """A compact directory overview. Full listing for small repos; per-directory counts for large ones."""
    if len(paths) <= max_lines:
        return "\n".join(paths)
    counts: dict[str, int] = defaultdict(int)
    root_files = []
    for p in paths:
        parts = PurePosixPath(p).parts
        if len(parts) == 1:
            root_files.append(p)
        for depth in range(1, min(len(parts), 4)):
            counts["/".join(parts[:depth]) + "/"] += 1
    lines = [f"{d} ({n} files)" for d, n in sorted(counts.items())]
    if len(lines) > max_lines:
        lines = [l for l in lines if l.count("/") <= 2][:max_lines]
    return "\n".join(root_files + lines)


def repo_context_block(name: str, tree: str, manifest_facts: str) -> str:
    return f"# Repository: {name}\n\n## File tree\n{tree}\n\n{manifest_facts}"


def system_blocks(instructions: str, context: str) -> list[dict]:
    """Instructions + shared repo context, with the context cached across all calls in a run."""
    return [
        {"type": "text", "text": instructions},
        {"type": "text", "text": context, "cache_control": {"type": "ephemeral"}},
    ]

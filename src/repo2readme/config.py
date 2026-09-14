from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Knobs for one generation run. The hosted demo uses tighter limits than the CLI."""

    model: str = "claude-opus-5"
    map_effort: str = "medium"
    reduce_effort: str = "high"

    # Budgets are in *estimated* tokens (see chunk.estimate_tokens).
    map_chunk_tokens: int = 40_000
    reduce_input_tokens: int = 120_000
    max_map_chunks: int = 60
    concurrency: int = 8

    # Repository limits.
    max_files: int = 20_000
    max_file_bytes: int = 400_000  # bigger than this is almost always generated/vendored
    max_repo_bytes: int = 300_000_000
    clone_timeout_s: int = 120

    # One repair round for claims that fail grounding checks in the final draft.
    repair_rounds: int = 1


HOSTED = Settings(
    max_map_chunks=20,
    max_files=8_000,
    max_repo_bytes=150_000_000,
    clone_timeout_s=60,
    concurrency=6,
)

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import replace
from pathlib import Path

import anthropic

from . import __version__
from .config import Settings
from .fetch import FetchError
from .llm import ClaudeLLM
from .pipeline import PipelineError, Progress, generate_from_prepared, plan_summary, prepare


def _progress(verbose: bool):
    last = {"stage": None}

    def emit(p: Progress) -> None:
        if p.stage != last["stage"] or verbose or p.done == p.total:
            detail = f" {p.message}" if p.message else ""
            print(f"[{p.stage}] {p.done}/{p.total}{detail}", file=sys.stderr, flush=True)
            last["stage"] = p.stage

    return emit


def build_parser() -> argparse.ArgumentParser:
    d = Settings()
    ap = argparse.ArgumentParser(
        prog="repo2readme",
        description="Generate a grounded README with a Mermaid architecture diagram for a GitHub repo or local directory.",
    )
    ap.add_argument("target", help="GitHub URL (https://github.com/owner/repo), owner/repo, or a local directory")
    ap.add_argument("-o", "--output", default="-", help="where to write the README (default: stdout)")
    ap.add_argument("--report", help="write the grounding/usage report as JSON to this path")
    ap.add_argument("--model", default=d.model, help=f"Claude model (default: {d.model})")
    ap.add_argument("--map-effort", default=d.map_effort, choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--reduce-effort", default=d.reduce_effort, choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--max-chunks", type=int, default=d.max_map_chunks, help="cap on map-pass calls")
    ap.add_argument("--chunk-tokens", type=int, default=d.map_chunk_tokens, help="estimated tokens per map chunk")
    ap.add_argument("--concurrency", type=int, default=d.concurrency)
    ap.add_argument("--repair-rounds", type=int, default=d.repair_rounds)
    ap.add_argument("--dry-run", action="store_true", help="run the deterministic stages only and print the plan")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=f"repo2readme {__version__}")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    settings = replace(
        Settings(),
        model=args.model, map_effort=args.map_effort, reduce_effort=args.reduce_effort,
        max_map_chunks=args.max_chunks, map_chunk_tokens=args.chunk_tokens,
        concurrency=args.concurrency, repair_rounds=args.repair_rounds,
    )
    progress = _progress(args.verbose)

    try:
        prepared = prepare(args.target, settings, progress)
        if args.dry_run:
            print(json.dumps(plan_summary(prepared, settings), indent=2))
            return 0
        llm = ClaudeLLM(settings.model)
        result = asyncio.run(generate_from_prepared(prepared, llm, settings, progress))
    except (FetchError, PipelineError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except anthropic.AuthenticationError:
        print("error: Anthropic authentication failed. Set ANTHROPIC_API_KEY.", file=sys.stderr)
        return 3
    except anthropic.APIError as e:
        print(f"error: Claude API request failed: {e}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 130

    if args.output == "-":
        sys.stdout.write(result.readme)
    else:
        Path(args.output).write_text(result.readme, encoding="utf-8")
        print(f"wrote {args.output}", file=sys.stderr)
    if args.report:
        Path(args.report).write_text(json.dumps(result.report, indent=2), encoding="utf-8")

    r = result.report
    mv = r["map_verification"]
    cost = r["usage"].get("estimated_cost_usd")
    print(
        f"files read by model: {r['files_sent_to_model']}, skeleton-only: {r['files_skeleton_only']}, "
        f"map facts kept/dropped: {mv['facts_kept']}/{mv['facts_dropped']}, "
        f"unverified mentions: {len(r['unverified_mentions'])}"
        + (f", est. cost: ${cost:.2f}" if cost is not None else ""),
        file=sys.stderr,
    )
    for issue in r["unverified_mentions"][:10]:
        print(f"  unverified [{issue['field']}] {issue['item']}: {issue['reason']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

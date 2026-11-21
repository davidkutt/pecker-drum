"""Command line: ``drum run --target twin --set <set> --model <mode> --repeats <k>``."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from pecker_drum import __version__
from pecker_drum.campaign.graph import CampaignConfig, CampaignResult, run_campaign
from pecker_drum.campaign.selftest import self_test_check
from pecker_drum.grading.links import DEFAULT_ALLOWED_HOSTS
from pecker_drum.items.loader import ItemSetError, resolve_set_path
from pecker_drum.netguard import block_network
from pecker_drum.report.render import pct
from pecker_drum.targets.twin import TwinSettings, TwinTarget
from pecker_drum.twin.embeddings import EmbedderName
from pecker_drum.twin.fixtures import FixtureError
from pecker_drum.twin.models import MissingApiKeyError, ModelSpecError, parse_model_spec

EXIT_OK = 0
EXIT_SELF_TEST_FAILED = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="drum", description="Red-team harness for RAG agents.")
    parser.add_argument("--version", action="version", version=f"pecker-drum {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run an item set against a target")
    run.add_argument("--target", required=True, help="target to attack (M1: twin)")
    run.add_argument("--set", dest="item_set", required=True, help="item set name or .jsonl path")
    run.add_argument(
        "--model", required=True, help="fake:obedient, fake:safe or openrouter:<model id>"
    )
    run.add_argument("--repeats", type=int, default=1, help="attempts per item (k), default 1")
    run.add_argument("--concurrency", type=int, default=4, help="parallel attempts, default 4")
    run.add_argument(
        "--embedder",
        choices=["auto", "hash", "fastembed"],
        default="auto",
        help="knowledge-base embeddings; auto = hash for fake models, fastembed for live runs",
    )
    run.add_argument(
        "--allow-host",
        action="append",
        default=None,
        metavar="HOST",
        help="host that links/images may point to (repeatable; default: the twin's own hosts)",
    )
    run.add_argument("--fresh", action="store_true", help="ignore an unfinished checkpoint")
    run.add_argument("--items-dir", type=Path, default=Path("items"))
    run.add_argument("--fixtures-dir", type=Path, default=Path("fixtures"))
    run.add_argument("--reports-dir", type=Path, default=Path("reports"))
    return parser


def _print_summary(result: CampaignResult) -> None:
    report = result.report
    print(
        f"run {result.run_id}: {report.info.target} / {report.info.item_set} / {report.info.model}"
    )
    if result.resumed:
        print(f"resumed from checkpoint: {result.already_done} attempts were already done")
    print()
    print(f"{'item':<10} {'success':>7} {'contained':>9} {'blocked':>7} {'error':>5}")
    for stat in report.stats:
        print(
            f"{stat.item_id:<10} {stat.successes:>7} {stat.contained:>9} "
            f"{stat.blocked:>7} {stat.errors:>5}"
        )
    overall = report.overall
    print()
    asrk = f", ASR@{overall.k} {pct(overall.asrk)}" if overall.k > 1 else ""
    print(
        f"ASR@1 {pct(overall.asr1)}{asrk} "
        f"({overall.successes}/{overall.attempts} attempts, {overall.contained} contained, "
        f"{overall.errors} errors)"
    )
    if report.self_test:
        failed = [row for row in report.self_test if not row.ok]
        verdict = "passed" if not failed else f"FAILED for {len(failed)} items"
        print(f"self-test {verdict}")
        for row in failed:
            print(
                f"  {row.item_id}: expected {row.expected}, got {', '.join(row.outcomes)}; "
                f"missing {', '.join(row.missing) or '-'}"
            )
    print()
    print(f"report: {result.report_paths[0]}")
    print(f"        {result.report_paths[1]}")


def _run(args: argparse.Namespace) -> int:
    if args.target != "twin":
        print(f"drum: unknown target {args.target!r} (M1 supports: twin)", file=sys.stderr)
        return EXIT_USAGE
    if args.repeats < 1 or args.concurrency < 1:
        print("drum: --repeats and --concurrency must be at least 1", file=sys.stderr)
        return EXIT_USAGE
    try:
        spec = parse_model_spec(args.model)
        set_path = resolve_set_path(args.item_set, args.items_dir)
    except (ModelSpecError, ItemSetError) as exc:
        print(f"drum: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if spec.is_fake:
        block_network()  # the self-test must never reach the network
    embedder: EmbedderName
    if args.embedder == "auto":
        embedder = "hash" if spec.is_fake else "fastembed"
    else:
        embedder = args.embedder
    allowed = tuple(args.allow_host) if args.allow_host else DEFAULT_ALLOWED_HOSTS
    settings = TwinSettings(
        model=spec, fixtures_root=args.fixtures_dir, embedder=embedder, allowed_hosts=allowed
    )
    try:
        target = TwinTarget(settings)
    except (MissingApiKeyError, FixtureError) as exc:
        print(f"drum: {exc}", file=sys.stderr)
        return EXIT_USAGE
    config = CampaignConfig(
        set_path=set_path,
        repeats=args.repeats,
        concurrency=args.concurrency,
        reports_dir=args.reports_dir,
        fresh=args.fresh,
        self_test=self_test_check(spec, target.grading_context) if spec.is_fake else None,
    )
    try:
        result = asyncio.run(run_campaign(target, config))
    except KeyboardInterrupt:
        print(
            "\ninterrupted: run the same command again to resume from the checkpoint",
            file=sys.stderr,
        )
        return EXIT_INTERRUPTED
    _print_summary(result)
    if result.report.self_test and not result.report.self_test_passed:
        return EXIT_SELF_TEST_FAILED
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "run":
        return _run(args)
    return EXIT_USAGE  # pragma: no cover - argparse enforces the subcommand


if __name__ == "__main__":
    raise SystemExit(main())

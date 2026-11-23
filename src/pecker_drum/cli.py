"""Command line.

- ``drum run --target twin --set <set> --model <mode> [--judge <judge>] --repeats <k>``
- ``drum report --run-id <id> [--judge <judge>]``: rebuild a finished run's reports from its run
  record, without calling the target (M1.5).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import warnings
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from pecker_drum import __version__
from pecker_drum.campaign.graph import CampaignConfig, run_campaign, utc_now
from pecker_drum.campaign.record import RunRecord, RunRecordError, read_record
from pecker_drum.campaign.selftest import self_test_check
from pecker_drum.envfile import EnvFileError, EnvFileResult, load_env_file
from pecker_drum.grading.links import DEFAULT_ALLOWED_HOSTS
from pecker_drum.items.loader import ItemSetError, resolve_set_path
from pecker_drum.judge import Judge, JudgeSpec, judge_result, make_judge, parse_judge_spec
from pecker_drum.netguard import block_network
from pecker_drum.report.render import Report, build_report, pct, write_report
from pecker_drum.results import AttemptResult
from pecker_drum.targets.twin import TwinSettings, TwinTarget
from pecker_drum.twin.embeddings import EmbedderName
from pecker_drum.twin.fixtures import FixtureError
from pecker_drum.twin.models import (
    OPENROUTER_KEY_ENV,
    OPENROUTER_MODEL_ENV,
    MissingApiKeyError,
    ModelSpec,
    ModelSpecError,
    asks_for_openrouter,
    openrouter_spec_from_env,
    parse_model_spec,
    refuse_key_as_model_id,
)

EXIT_OK = 0
EXIT_SELF_TEST_FAILED = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130

ENV_FILE = Path(".env")
"""Read from the working directory, and only when ``--model`` or ``--judge`` asks for OpenRouter
(ADR 0007, ADR 0008)."""
JUDGE_HELP = (
    "advisory judge for success and contained attempts: fake:complied, fake:reported or "
    "openrouter:<model id>; never changes an outcome"
)
LIVE_SETTINGS = (OPENROUTER_KEY_ENV, OPENROUTER_MODEL_ENV)
"""The only names taken from ``.env``."""


def _parser() -> argparse.ArgumentParser:
    # No abbreviations (``--mod``): only the full ``--model`` spelling selects a model, so a gate
    # that matches ``--model openrouter`` on the command line sees every live run.
    parser = argparse.ArgumentParser(
        prog="drum", description="Red-team harness for RAG agents.", allow_abbrev=False
    )
    parser.add_argument("--version", action="version", version=f"pecker-drum {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run an item set against a target", allow_abbrev=False)
    run.add_argument("--target", required=True, help="target to attack (M1: twin)")
    run.add_argument("--set", dest="item_set", required=True, help="item set name or .jsonl path")
    run.add_argument(
        "--model",
        required=True,
        help=(
            "fake:obedient, fake:safe, openrouter:<model id>, or openrouter for the model id in "
            f"{OPENROUTER_MODEL_ENV} (environment or .env)"
        ),
    )
    run.add_argument("--judge", default=None, metavar="JUDGE", help=JUDGE_HELP)
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
    report = commands.add_parser(
        "report",
        help="rebuild the reports of a finished run from its run record (no target calls)",
        allow_abbrev=False,
    )
    report.add_argument("--run-id", required=True, help="the run id from the report header")
    report.add_argument("--judge", default=None, metavar="JUDGE", help=JUDGE_HELP)
    report.add_argument("--concurrency", type=int, default=4, help="parallel judge calls")
    report.add_argument("--reports-dir", type=Path, default=Path("reports"))
    return parser


def _print_summary(report: Report, paths: tuple[Path, Path], notes: Sequence[str] = ()) -> None:
    info = report.info
    print(f"run {info.run_id}: {info.target} / {info.item_set} / {info.model}")
    for note in notes:
        print(note)
    print()
    overall = report.overall
    unobs = overall.unobservable > 0
    print(
        f"{'item':<10} {'success':>7} {'contained':>9} {'blocked':>7} {'error':>5}"
        + (f" {'unobservable':>12}" if unobs else "")
    )
    for stat in report.stats:
        print(
            f"{stat.item_id:<10} {stat.successes:>7} {stat.contained:>9} "
            f"{stat.blocked:>7} {stat.errors:>5}" + (f" {stat.unobservable:>12}" if unobs else "")
        )
    print()
    asrk = f", ASR@{overall.k} {pct(overall.asrk)}" if overall.k > 1 else ""
    print(
        f"ASR@1 {pct(overall.asr1)}{asrk} "
        f"({overall.successes}/{overall.attempts} attempts, {overall.contained} contained, "
        f"{overall.errors} errors"
        + (f", {overall.unobservable} unobservable" if unobs else "")
        + ")"
    )
    if report.judge_overall is not None:
        tally = report.judge_overall
        print(
            f"judge {info.judge} (advisory): {tally.success.text('success')}; "
            f"{tally.contained.text('contained')}"
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
    print(f"report: {paths[0]}")
    print(f"        {paths[1]}")


def _warn_about_env_file(result: EnvFileResult) -> None:
    if result.readable_by_others:
        print(
            f"drum: warning: {ENV_FILE} is readable by group or others; "
            f"restrict it with chmod 600 {ENV_FILE}",
            file=sys.stderr,
        )
    skipped = list(result.ignored)
    if result.unnamed:
        entries = "entry" if result.unnamed == 1 else "entries"
        skipped.append(f"{result.unnamed} {entries} without a plain NAME=value")
    if skipped:
        print(
            f"drum: warning: {ENV_FILE}: ignored {', '.join(skipped)} "
            f"(only {' and '.join(LIVE_SETTINGS)} are read)",
            file=sys.stderr,
        )


def _resolve(
    model_value: str | None, judge_value: str | None
) -> tuple[ModelSpec | None, JudgeSpec | None]:
    """Parse ``--model`` and ``--judge``. ``.env`` is opened only when one of them asks for
    OpenRouter (ADR 0007, extended by ADR 0008); syntax errors stop the run before that."""
    judge = parse_judge_spec(judge_value) if judge_value is not None else None
    explicit = (
        parse_model_spec(model_value)
        if model_value is not None and model_value != "openrouter"
        else None
    )
    wants_model_env = model_value is not None and asks_for_openrouter(model_value)
    loaded = EnvFileResult()
    if wants_model_env or (judge is not None and not judge.is_fake):
        loaded = load_env_file(ENV_FILE, LIVE_SETTINGS)
        _warn_about_env_file(loaded)
    spec = openrouter_spec_from_env(os.environ) if model_value == "openrouter" else explicit
    # Refuse a key-shaped id before anything prints it (M1.4 leak rule).
    if spec is not None:
        refuse_key_as_model_id(spec, os.environ)
    if judge is not None and not judge.is_fake:
        refuse_key_as_model_id(ModelSpec("openrouter", judge.model_id), os.environ)
    if model_value == "openrouter" and spec is not None:
        source = ENV_FILE if OPENROUTER_MODEL_ENV in loaded.loaded else "the environment"
        print(
            f"drum: --model openrouter uses {spec} ({OPENROUTER_MODEL_ENV} from {source})",
            file=sys.stderr,
        )
    return spec, judge


def _run(args: argparse.Namespace) -> int:
    if args.target != "twin":
        print(f"drum: unknown target {args.target!r} (M1 supports: twin)", file=sys.stderr)
        return EXIT_USAGE
    if args.repeats < 1 or args.concurrency < 1:
        print("drum: --repeats and --concurrency must be at least 1", file=sys.stderr)
        return EXIT_USAGE
    try:
        set_path = resolve_set_path(args.item_set, args.items_dir)
        spec, judge_spec = _resolve(args.model, args.judge)
    except (ModelSpecError, ItemSetError, EnvFileError) as exc:
        print(f"drum: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if spec is None:  # pragma: no cover - argparse requires --model
        return EXIT_USAGE
    if spec.is_fake and (judge_spec is None or judge_spec.is_fake):
        block_network()  # the self-test (and a fake judge) must never reach the network
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
        judge = make_judge(judge_spec) if judge_spec is not None else None
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
        judge=judge,
    )
    try:
        result = asyncio.run(run_campaign(target, config))
    except KeyboardInterrupt:
        print(
            "\ninterrupted: run the same command again to resume from the checkpoint",
            file=sys.stderr,
        )
        return EXIT_INTERRUPTED
    notes = (
        [f"resumed from checkpoint: {result.already_done} attempts were already done"]
        if result.resumed
        else []
    )
    _print_summary(result.report, result.report_paths, notes)
    if result.report.self_test and not result.report.self_test_passed:
        return EXIT_SELF_TEST_FAILED
    return EXIT_OK


async def _rejudge(judge: Judge, record: RunRecord, concurrency: int) -> tuple[AttemptResult, ...]:
    items = {item.id: item for item in record.items}
    limit = asyncio.Semaphore(concurrency)

    async def one(result: AttemptResult) -> AttemptResult:
        item = items.get(result.item_id)
        if item is None:  # pragma: no cover - a record always holds its items
            return result
        async with limit:
            return await judge_result(judge, item, result)

    return tuple(await asyncio.gather(*(one(result) for result in record.results)))


def _report(args: argparse.Namespace) -> int:
    """Rebuild a finished run's reports from its run record; the target is never called."""
    if args.concurrency < 1:
        print("drum: --concurrency must be at least 1", file=sys.stderr)
        return EXIT_USAGE
    try:
        record = read_record(args.reports_dir, args.run_id)
        _, judge_spec = _resolve(None, args.judge)
    except (RunRecordError, ModelSpecError, EnvFileError) as exc:
        print(f"drum: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if judge_spec is None or judge_spec.is_fake:
        block_network()
    if record.missing_answers:
        print(
            f"drum: warning: {record.missing_answers} of {len(record.results)} attempts in run "
            f"{record.info.run_id} have no full answer (recorded before M1.5); run it again for "
            "full answers",
            file=sys.stderr,
        )
    try:
        judge = make_judge(judge_spec) if judge_spec is not None else None
    except MissingApiKeyError as exc:
        print(f"drum: {exc}", file=sys.stderr)
        return EXIT_USAGE
    info, results = record.info, record.results
    if judge is not None:
        try:
            results = asyncio.run(_rejudge(judge, record, args.concurrency))
        except KeyboardInterrupt:
            print("\ninterrupted: nothing was written", file=sys.stderr)
            return EXIT_INTERRUPTED
        info = info.model_copy(update={"judge": judge.name})
    report = build_report(info, record.items, results, record.self_test, generated_at=utc_now())
    paths = write_report(report, args.reports_dir, date.today())
    _print_summary(report, paths, [f"re-rendered from {args.reports_dir}/.runs/{info.run_id}.json"])
    if report.self_test and not report.self_test_passed:
        return EXIT_SELF_TEST_FAILED
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    # LangGraph drops queued attempt coroutines when a run is interrupted; that is expected.
    warnings.filterwarnings("ignore", message="coroutine 'arun_with_retry' was never awaited")
    args = _parser().parse_args(argv)
    if args.command == "run":
        return _run(args)
    if args.command == "report":
        return _report(args)
    return EXIT_USAGE  # pragma: no cover - argparse enforces the subcommand


if __name__ == "__main__":
    raise SystemExit(main())

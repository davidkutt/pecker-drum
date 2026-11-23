"""Command line.

- ``drum run --target twin --set <set> --model <mode> [--judge <judge>] --repeats <k>``
- ``drum run --target stub --set <set> --model fake:obedient|fake:safe``: the stand-in server
  (M2) with ``docs/examples/http-target.toml``. Its data is public: reports go to ``reports/``.
- ``drum run --target <name> --set <set> --model target``: a private HTTP target configured in
  ``private/targets/<name>.toml`` (M2). Reports go to ``private/reports/``.
- ``drum report --run-id <id> [--judge <judge>]``: rebuild a finished run's reports from its run
  record, without calling the target (M1.5). It reads ``reports/``; a private run needs
  ``--reports-dir private/reports``, which the agents' gate blocks.
- ``drum cleanup --target <name> --run-id <id>``: delete documents a run left on an HTTP target.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import secrets
import sys
import warnings
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from pecker_drum import __version__
from pecker_drum.campaign.graph import (
    CampaignConfig,
    CampaignResult,
    RunAborted,
    in_scope,
    run_campaign,
    utc_now,
)
from pecker_drum.campaign.record import (
    RunRecord,
    RunRecordError,
    RunRecordNotFoundError,
    read_record,
)
from pecker_drum.campaign.selftest import self_test_check
from pecker_drum.envfile import EnvFileError, EnvFileResult, load_env_file
from pecker_drum.grading.links import DEFAULT_ALLOWED_HOSTS
from pecker_drum.items.loader import ItemSetError, load_item_set, resolve_set_path
from pecker_drum.items.schema import Item, Vector
from pecker_drum.judge import Judge, JudgeSpec, judge_result, make_judge, parse_judge_spec
from pecker_drum.netguard import block_network
from pecker_drum.report.render import (
    Report,
    build_report,
    contradicted,
    model_slug,
    pct,
    write_report,
)
from pecker_drum.results import AttemptResult
from pecker_drum.targets.http import (
    ConfigError,
    HttpTarget,
    HttpTargetConfig,
    LeftoversError,
    MissingSecretError,
    RemoteDocument,
    clean_up_run,
    load_config,
)
from pecker_drum.targets.http.client import TargetError
from pecker_drum.targets.stub import StubMode, StubServer
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
EXIT_STOPPED = 3
"""A safety cap or the target stopped the run, or planted documents were left (M2)."""
EXIT_INTERRUPTED = 130

TARGET_MODEL = "target"
"""``--model target``: an HTTP target runs its own model (M2)."""
STUB_MODELS = ("fake:obedient", "fake:safe")
EXAMPLE_CONFIG = Path("docs/examples/http-target.toml")
PRIVATE_TARGETS = Path("private/targets")
PUBLIC_REPORTS = Path("reports")
PRIVATE_REPORTS = Path("private/reports")
"""Private HTTP targets write here by default: reports and run records hold the target's answers.
``drum report`` reads it only when ``--reports-dir`` names it, so the path is always on the
command line where a gate can see it."""
_TARGET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}")
_RUN_ID = re.compile(r"[0-9a-f]{16}")

ENV_FILE = Path(".env")
"""Read from the working directory, and only when ``--model`` or ``--judge`` asks for OpenRouter
(ADR 0007, ADR 0008)."""
JUDGE_HELP = (
    "advisory judge for success and contained attempts: fake:complied, fake:reported or "
    "openrouter:<model id>; never changes an outcome"
)
LIVE_SETTINGS = (OPENROUTER_KEY_ENV, OPENROUTER_MODEL_ENV)
"""The only names taken from ``.env``."""
TARGET_HELP = (
    "twin, stub (the stand-in server), or the name of an HTTP target "
    "(letters, digits, hyphens; config in private/targets/<name>.toml)"
)
CONFIG_HELP = (
    "the HTTP target's TOML config (default: private/targets/<name>.toml; for stub "
    "docs/examples/http-target.toml)"
)
REPORTS_HELP = (
    "where reports go (default: reports/ for the twin and the stand-in, private/reports/ for a "
    "private HTTP target)"
)


def _parser() -> argparse.ArgumentParser:
    # No abbreviations (``--mod``): only the full ``--model`` spelling selects a model, so a gate
    # that matches ``--model openrouter`` on the command line sees every live run.
    parser = argparse.ArgumentParser(
        prog="drum", description="Red-team harness for RAG agents.", allow_abbrev=False
    )
    parser.add_argument("--version", action="version", version=f"pecker-drum {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run an item set against a target", allow_abbrev=False)
    run.add_argument("--target", required=True, help=TARGET_HELP)
    run.add_argument("--target-config", type=Path, default=None, metavar="PATH", help=CONFIG_HELP)
    run.add_argument("--set", dest="item_set", required=True, help="item set name or .jsonl path")
    run.add_argument(
        "--model",
        required=True,
        help=(
            "twin: fake:obedient, fake:safe, openrouter:<model id>, or openrouter for the model id "
            f"in {OPENROUTER_MODEL_ENV} (environment or .env); stub: fake:obedient or fake:safe; "
            "an HTTP target: target (it runs its own model)"
        ),
    )
    run.add_argument(
        "--vectors",
        default=None,
        metavar="LIST",
        help="run only items with these vectors, comma-separated (e.g. direct,indirect_doc)",
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
        help=(
            "host that links/images may point to (repeatable; default: the twin's own hosts, or "
            "an HTTP target's grading.allowed_hosts)"
        ),
    )
    run.add_argument("--fresh", action="store_true", help="ignore an unfinished checkpoint")
    run.add_argument("--items-dir", type=Path, default=Path("items"))
    run.add_argument("--fixtures-dir", type=Path, default=Path("fixtures"))
    run.add_argument("--reports-dir", type=Path, default=None, help=REPORTS_HELP)
    report = commands.add_parser(
        "report",
        help="rebuild the reports of a finished run from its run record (no target calls)",
        allow_abbrev=False,
    )
    report.add_argument("--run-id", required=True, help="the run id from the report header")
    report.add_argument("--judge", default=None, metavar="JUDGE", help=JUDGE_HELP)
    report.add_argument("--concurrency", type=int, default=4, help="parallel judge calls")
    report.add_argument(
        "--reports-dir",
        type=Path,
        default=None,
        help="where the run record is (default: reports/; a private run: private/reports)",
    )
    cleanup = commands.add_parser(
        "cleanup",
        help="delete the documents a run planted on an HTTP target and left behind",
        allow_abbrev=False,
    )
    cleanup.add_argument("--target", required=True, help=TARGET_HELP)
    cleanup.add_argument(
        "--target-config", type=Path, default=None, metavar="PATH", help=CONFIG_HELP
    )
    cleanup.add_argument("--run-id", required=True, help="the run id from the report header")
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
    contradictions = contradicted(report)
    if contradictions:
        print(
            f"contradictions: {len(contradictions)} attempts: the target reported a call as "
            "executed that the harness denied (graded as executed)"
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


def _parse_vectors(value: str | None) -> frozenset[str] | None:
    if value is None:
        return None
    names = {part.strip() for part in value.split(",") if part.strip()}
    known = {vector.value for vector in Vector}
    unknown = sorted(names - known)
    if unknown or not names:
        msg = (
            f"--vectors: unknown vector {', '.join(map(repr, unknown)) or '(none given)'} "
            f"(known: {', '.join(sorted(known))})"
        )
        raise ValueError(msg)
    return frozenset(names)


def _target_kind(name: str) -> str | None:
    """``twin``, ``stub``, ``http`` (a private target) or ``None`` for a name that is not plain."""
    if name in ("twin", "stub"):
        return name
    return "http" if _TARGET_NAME.fullmatch(name) else None


def _config_path(args: argparse.Namespace, kind: str) -> Path:
    if args.target_config is not None:
        return args.target_config
    return EXAMPLE_CONFIG if kind == "stub" else PRIVATE_TARGETS / f"{args.target}.toml"


def _ca_file(config_path: Path, config: HttpTargetConfig) -> Path | None:
    return None if config.ca_file is None else config_path.parent / config.ca_file


def _resume_notes(result: CampaignResult) -> list[str]:
    if not result.resumed:
        return []
    return [f"resumed from checkpoint: {result.already_done} attempts were already done"]


def _run(args: argparse.Namespace) -> int:
    if args.repeats < 1 or args.concurrency < 1:
        print("drum: --repeats and --concurrency must be at least 1", file=sys.stderr)
        return EXIT_USAGE
    kind = _target_kind(args.target)
    if kind is None:
        print(
            f"drum: unknown target {args.target[:64]!r}: use twin, stub or a plain name "
            "(letters, digits, hyphens)",
            file=sys.stderr,
        )
        return EXIT_USAGE
    try:
        vectors = _parse_vectors(args.vectors)
    except ValueError as exc:
        print(f"drum: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if kind == "twin":
        return _run_twin(args, vectors)
    return _run_http(args, kind, vectors)


def _run_twin(args: argparse.Namespace, vectors: frozenset[str] | None) -> int:
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
        reports_dir=args.reports_dir or PUBLIC_REPORTS,
        fresh=args.fresh,
        self_test=self_test_check(spec, target.grading_context) if spec.is_fake else None,
        judge=judge,
        vectors=vectors,
    )
    try:
        result = asyncio.run(run_campaign(target, config))
    except KeyboardInterrupt:
        print(
            "\ninterrupted: run the same command again to resume from the checkpoint",
            file=sys.stderr,
        )
        return EXIT_INTERRUPTED
    _print_summary(result.report, result.report_paths, _resume_notes(result))
    if result.report.self_test and not result.report.self_test_passed:
        return EXIT_SELF_TEST_FAILED
    return EXIT_OK


def _cleanup_command(args: argparse.Namespace, run_id: str) -> str:
    config = f" --target-config {args.target_config}" if args.target_config is not None else ""
    return f"uv run drum cleanup --target {args.target}{config} --run-id {run_id}"


def _print_documents(documents: Sequence[RemoteDocument]) -> None:
    for document in documents[:20]:
        print(f"  {document.tag_value[:120]} (id {document.id[:60]})", file=sys.stderr)
    if len(documents) > 20:
        print(f"  ... and {len(documents) - 20} more", file=sys.stderr)


def _check_leftovers(args: argparse.Namespace, target: HttpTarget) -> bool:
    """After every HTTP run: True if planted documents of this run are still on the target."""
    for problem in target.cleanup_problems:
        print(f"drum: warning: cleanup: {problem}", file=sys.stderr)
    if not target.run_id:
        return False
    if target.end_problem is not None or target.remaining is None:
        print(
            f"drum: ERROR: could not check the target for leftover documents of run "
            f"{target.run_id} ({target.end_problem}); check and clean up with:\n  "
            f"{_cleanup_command(args, target.run_id)}",
            file=sys.stderr,
        )
        return True
    if target.remaining:
        print(
            f"drum: ERROR: {len(target.remaining)} planted documents of run {target.run_id} are "
            "still on the target:",
            file=sys.stderr,
        )
        _print_documents(target.remaining)
        print(f"remove them with:\n  {_cleanup_command(args, target.run_id)}", file=sys.stderr)
        return True
    return False


def _run_http(args: argparse.Namespace, kind: str, vectors: frozenset[str] | None) -> int:
    """``--target stub`` or a private HTTP target (M2, ADR 0009)."""
    config_path = _config_path(args, kind)
    if not config_path.is_file():
        what = "the stand-in's config" if kind == "stub" else f"unknown target {args.target!r}"
        print(f"drum: {what}: no config at {config_path}", file=sys.stderr)
        return EXIT_USAGE
    if kind == "stub" and args.model not in STUB_MODELS:
        print("drum: --target stub takes --model fake:obedient or fake:safe", file=sys.stderr)
        return EXIT_USAGE
    if kind == "http" and args.model != TARGET_MODEL:
        print(
            "drum: an HTTP target runs its own model: use --model target (the config's label "
            "names it in reports)",
            file=sys.stderr,
        )
        return EXIT_USAGE
    try:
        set_path = resolve_set_path(args.item_set, args.items_dir)
        _, judge_spec = _resolve(None, args.judge)
        config = load_config(config_path)
        item_set = load_item_set(set_path)
    except (ModelSpecError, ItemSetError, EnvFileError, ConfigError) as exc:
        print(f"drum: {exc}", file=sys.stderr)
        return EXIT_USAGE
    scope = in_scope(item_set.items, vectors)
    planned = len(scope) * args.repeats
    if planned > config.limits.max_attempts:
        print(
            f"drum: {planned} attempts ({len(scope)} items x {args.repeats} repeats) exceed "
            f"limits.max_attempts = {config.limits.max_attempts} in {config_path}",
            file=sys.stderr,
        )
        return EXIT_USAGE
    live_judge = judge_spec is not None and not judge_spec.is_fake
    if kind == "stub" and not live_judge:
        block_network(allow_loopback=True)  # the stand-in on 127.0.0.1, nothing else
    server: StubServer | None = None
    environ: dict[str, str] | None = None
    runtime = config
    if kind == "stub":
        # Fresh passwords per run, held in memory only; the stand-in accepts exactly these.
        environ = {
            login.password_env: secrets.token_urlsafe(24) for login in config.auth.users.values()
        }
        users = {
            login.username: environ[login.password_env] for login in config.auth.users.values()
        }
        server = StubServer(item_set.items, StubMode(args.model), users).start()
        runtime = config.model_copy(update={"base_url": server.url})
    try:
        return _run_http_target(
            args, kind, vectors, config_path, config, runtime, environ, judge_spec, set_path, scope
        )
    finally:
        if server is not None:
            server.stop()


def _run_http_target(
    args: argparse.Namespace,
    kind: str,
    vectors: frozenset[str] | None,
    config_path: Path,
    config: HttpTargetConfig,
    runtime: HttpTargetConfig,
    environ: dict[str, str] | None,
    judge_spec: JudgeSpec | None,
    set_path: Path,
    scope: Sequence[Item],
) -> int:
    stub = kind == "stub"
    try:
        target = HttpTarget(
            runtime,
            name=args.target,
            model=args.model if stub else TARGET_MODEL,
            label=f"{config.label}-{model_slug(args.model)}" if stub else config.label,
            fixtures_root=args.fixtures_dir,
            environ=environ,
            identity=config,
            allowed_hosts=tuple(args.allow_host) if args.allow_host else None,
            ca_file=_ca_file(config_path, config),
        )
        judge = make_judge(judge_spec) if judge_spec is not None else None
    except (MissingSecretError, MissingApiKeyError, FixtureError) as exc:
        print(f"drum: {exc}", file=sys.stderr)
        return EXIT_USAGE
    problems = target.preflight(scope)
    if problems:
        for problem in problems:
            print(f"drum: {problem}", file=sys.stderr)
        print("drum: nothing was run (narrow the set with --vectors?)", file=sys.stderr)
        return EXIT_USAGE
    where = "the stand-in" if stub else config.label
    notes = [f"target {args.target}: {where} at {runtime.base_url}"]
    concurrency = args.concurrency
    if config.fixtures is not None and concurrency > 1:
        concurrency = 1
        notes.append(
            "concurrency 1: this target plants documents (attempts must not see each other's)"
        )
    spec = parse_model_spec(args.model) if stub else None
    campaign = CampaignConfig(
        set_path=set_path,
        repeats=args.repeats,
        concurrency=concurrency,
        reports_dir=args.reports_dir or (PUBLIC_REPORTS if stub else PRIVATE_REPORTS),
        fresh=args.fresh,
        self_test=self_test_check(spec, target.grading_context) if spec is not None else None,
        judge=judge,
        vectors=vectors,
        max_wall_time_s=config.limits.max_wall_time,
        max_consecutive_errors=config.limits.max_consecutive_errors,
        lifecycle=target,
    )

    async def go() -> CampaignResult:
        async with target:
            return await run_campaign(target, campaign)

    result: CampaignResult | None = None
    code = EXIT_OK
    try:
        result = asyncio.run(go())
    except KeyboardInterrupt:
        print(
            "\ninterrupted: planted documents were cleaned up; run the same command again to "
            "resume from the checkpoint",
            file=sys.stderr,
        )
        code = EXIT_INTERRUPTED
    except RunAborted as exc:
        print(
            f"drum: stopped: {exc}\nrun the same command again to resume from the checkpoint",
            file=sys.stderr,
        )
        code = EXIT_STOPPED
    except LeftoversError as exc:
        print(f"drum: ERROR: {exc}", file=sys.stderr)
        print("nothing was run; remove them first:", file=sys.stderr)
        for run_id in exc.run_ids:
            print(f"  {_cleanup_command(args, run_id)}", file=sys.stderr)
        return EXIT_STOPPED
    except TargetError as exc:
        print(f"drum: {exc}", file=sys.stderr)
        code = EXIT_STOPPED
    if _check_leftovers(args, target):
        code = EXIT_STOPPED
    if result is None:
        return code
    _print_summary(result.report, result.report_paths, [*notes, *_resume_notes(result)])
    if code == EXIT_OK and result.report.self_test and not result.report.self_test_passed:
        return EXIT_SELF_TEST_FAILED
    return code


def _cleanup(args: argparse.Namespace) -> int:
    """``drum cleanup``: delete documents a run left on an HTTP target, then list again."""
    if not _RUN_ID.fullmatch(args.run_id):
        print("drum: a run id is 16 hex characters, as in the report header", file=sys.stderr)
        return EXIT_USAGE
    kind = _target_kind(args.target)
    if kind is None:
        print(f"drum: unknown target {args.target[:64]!r}", file=sys.stderr)
        return EXIT_USAGE
    if kind in ("twin", "stub"):
        print(f"the {args.target} keeps nothing between runs; nothing to clean up")
        return EXIT_OK
    config_path = _config_path(args, kind)
    try:
        config = load_config(config_path)
        target = HttpTarget(config, name=args.target, ca_file=_ca_file(config_path, config))
    except (ConfigError, MissingSecretError) as exc:
        print(f"drum: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if config.fixtures is None:
        print(f"{args.target} has no [fixtures]: the harness plants nothing there")
        return EXIT_OK

    async def go() -> tuple[tuple[RemoteDocument, ...], list[str], tuple[RemoteDocument, ...]]:
        async with target:
            return await clean_up_run(target, args.run_id)

    try:
        found, problems, remaining = asyncio.run(go())
    except TargetError as exc:
        print(f"drum: {exc}", file=sys.stderr)
        return EXIT_STOPPED
    print(
        f"run {args.run_id}: {len(found)} planted documents found, "
        f"{len(found) - len(remaining)} deleted, {len(remaining)} left"
    )
    for problem in problems:
        print(f"drum: warning: {problem}", file=sys.stderr)
    if remaining:
        print("drum: ERROR: still on the target:", file=sys.stderr)
        _print_documents(remaining)
        return EXIT_STOPPED
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
    # Only reports/ by default: a private run's record is read only when the command line names
    # private/reports, so a gate sees it and an agent session never prints private results.
    reports_dir: Path = args.reports_dir or PUBLIC_REPORTS
    try:
        record = read_record(reports_dir, args.run_id)
        _, judge_spec = _resolve(None, args.judge)
    except RunRecordNotFoundError as exc:
        print(f"drum: {exc}", file=sys.stderr)
        if args.reports_dir is None:
            print(
                f"drum: a private HTTP target keeps its records in {PRIVATE_REPORTS}/: add "
                f"--reports-dir {PRIVATE_REPORTS}",
                file=sys.stderr,
            )
        return EXIT_USAGE
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
    paths = write_report(report, reports_dir, date.today())
    _print_summary(report, paths, [f"re-rendered from {reports_dir}/.runs/{info.run_id}.json"])
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
    if args.command == "cleanup":
        return _cleanup(args)
    return EXIT_USAGE  # pragma: no cover - argparse enforces the subcommand


if __name__ == "__main__":
    raise SystemExit(main())

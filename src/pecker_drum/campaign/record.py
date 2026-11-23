"""The run record: the final state of a finished run, for ``drum report`` (M1.5, ADR 0008).

A finished run deletes its LangGraph checkpoint (the spec) and leaves
``reports/.runs/<run id>.json`` with the run info, the items, every attempt result (full answers
included) and the self-test rows.
``drum report --run-id`` rebuilds the reports from it without calling the target. The record is
validated with Pydantic on load, so it does not depend on LangGraph's checkpoint format. A later
run with the same run id replaces it.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from pecker_drum.items.schema import Item
from pecker_drum.report.render import SelfTestRow
from pecker_drum.results import AttemptResult, RunInfo

RUNS_DIR = ".runs"
CHECKPOINTS_DIR = ".checkpoints"
_RUN_ID = re.compile(r"[0-9a-f]{16}")


class RunRecordError(RuntimeError):
    """No usable record for a run id. The message says what to do next."""


class RunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    format: Literal[1] = 1
    info: RunInfo
    items: tuple[Item, ...]
    results: tuple[AttemptResult, ...]
    self_test: tuple[SelfTestRow, ...] = ()

    @property
    def missing_answers(self) -> int:
        """Attempts without a full answer (resumed from a checkpoint made before M1.5)."""
        return sum(1 for result in self.results if result.answer is None)


def record_path(reports_dir: Path, run_id: str) -> Path:
    if not _RUN_ID.fullmatch(run_id):
        msg = f"a run id is 16 hex characters, as in the report header (got {run_id[:40]!r})"
        raise RunRecordError(msg)
    return reports_dir / RUNS_DIR / f"{run_id}.json"


def write_record(record: RunRecord, reports_dir: Path) -> Path:
    path = record_path(reports_dir, record.info.run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    partial.write_text(record.model_dump_json(indent=2) + "\n", encoding="utf-8")
    os.replace(partial, path)
    return path


def read_record(reports_dir: Path, run_id: str) -> RunRecord:
    path = record_path(reports_dir, run_id)
    if not path.is_file():
        if (reports_dir / CHECKPOINTS_DIR / f"{run_id}.sqlite").is_file():
            msg = (
                f"run {run_id} has not finished: resume it with the same drum run command, "
                "then re-render it"
            )
        else:
            msg = (
                f"no finished run {run_id} in {reports_dir / RUNS_DIR}: runs that finished before "
                "M1.5 keep no record, so run it again"
            )
        raise RunRecordError(msg)
    try:
        return RunRecord.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValidationError):
        msg = f"the run record {path} cannot be read; run it again"
        raise RunRecordError(msg) from None


def find_record(reports_dirs: Sequence[Path], run_id: str) -> tuple[Path, RunRecord]:
    """The first of ``reports_dirs`` that holds a record for ``run_id`` (M2: twin runs in
    ``reports/``, HTTP runs in ``private/reports/``), and the record."""
    for reports_dir in reports_dirs:
        if record_path(reports_dir, run_id).is_file():
            return reports_dir, read_record(reports_dir, run_id)
    for reports_dir in reports_dirs:
        if (reports_dir / CHECKPOINTS_DIR / f"{run_id}.sqlite").is_file():
            return reports_dir, read_record(reports_dir, run_id)  # raises "has not finished"
    places = " or ".join(str(d / RUNS_DIR) for d in reports_dirs)
    msg = (
        f"no finished run {run_id} in {places}: runs that finished before M1.5 keep no record, "
        "so run it again"
    )
    raise RunRecordError(msg)

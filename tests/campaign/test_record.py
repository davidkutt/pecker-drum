from pathlib import Path

import pytest

from pecker_drum.campaign.record import (
    RunRecord,
    RunRecordError,
    read_record,
    record_path,
    write_record,
)
from pecker_drum.grading.outcome import Outcome
from pecker_drum.results import AttemptResult, RunInfo

RUN_ID = "0123456789abcdef"
INFO = RunInfo(
    run_id=RUN_ID,
    target="twin",
    item_set="demo_v1",
    set_digest="f" * 64,
    model="fake:obedient",
    repeats=1,
    settings={},
    started_at="2026-11-22T10:00:00+00:00",
    finished_at="2026-11-22T10:01:00+00:00",
    harness_version="0.1.0",
)


@pytest.mark.parametrize("bad", ["../../etc/passwd", "0123456789ABCDEF", "abc", "", RUN_ID + "0"])
def test_run_ids_are_16_hex_characters(tmp_path: Path, bad: str) -> None:
    with pytest.raises(RunRecordError, match="16 hex characters"):
        record_path(tmp_path, bad)


def test_round_trip_and_missing_answers(tmp_path: Path) -> None:
    results = (
        AttemptResult(item_id="t-1", attempt=1, outcome=Outcome.SUCCESS, answer="full"),
        AttemptResult(item_id="t-1", attempt=2, outcome=Outcome.SUCCESS),  # before M1.5
    )
    record = RunRecord(info=INFO, items=(), results=results)
    path = write_record(record, tmp_path)
    assert path == tmp_path / ".runs" / f"{RUN_ID}.json"
    loaded = read_record(tmp_path, RUN_ID)
    assert loaded == record
    assert loaded.missing_answers == 1
    assert not list((tmp_path / ".runs").glob("*.partial"))


def test_missing_record_says_what_to_do(tmp_path: Path) -> None:
    with pytest.raises(
        RunRecordError, match=r"finished before M1\.5 keep no record, so run it again"
    ):
        read_record(tmp_path, RUN_ID)
    checkpoints = tmp_path / ".checkpoints"
    checkpoints.mkdir()
    (checkpoints / f"{RUN_ID}.sqlite").write_bytes(b"")
    with pytest.raises(RunRecordError, match="has not finished: resume it"):
        read_record(tmp_path, RUN_ID)


def test_unreadable_record(tmp_path: Path) -> None:
    runs = tmp_path / ".runs"
    runs.mkdir()
    (runs / f"{RUN_ID}.json").write_text('{"format": 2}', encoding="utf-8")
    with pytest.raises(RunRecordError, match="cannot be read") as raised:
        read_record(tmp_path, RUN_ID)
    assert raised.value.__suppress_context__

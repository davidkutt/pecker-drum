import json
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
from pecker_drum.report.render import build_report, render_json, render_markdown
from pecker_drum.results import (
    AttemptResult,
    RetrievalSignal,
    RetrievalState,
    RunInfo,
)

RUN_ID = "0123456789abcdef"
PRE_M2_1 = Path(__file__).parent / "data" / "record-pre-m2.1.json"
"""A finished run written before M2.1: its attempts have no source fields."""

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


def test_contradictions_round_trip_and_old_records_still_load(tmp_path: Path) -> None:
    text = "repo_create_issue: denied by the harness, reported executed by the target"
    result = AttemptResult(item_id="t-1", attempt=1, outcome=Outcome.SUCCESS, answer="a")
    record = RunRecord(
        info=INFO, items=(), results=(result.model_copy(update={"contradictions": (text,)}),)
    )
    path = write_record(record, tmp_path)
    assert read_record(tmp_path, RUN_ID).results[0].contradictions == (text,)
    raw = json.loads(path.read_text(encoding="utf-8"))
    for attempt in raw["results"]:
        del attempt["contradictions"]  # a record written before the field existed
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert read_record(tmp_path, RUN_ID).results[0].contradictions == ()


def test_retrieval_fields_round_trip(tmp_path: Path) -> None:
    result = AttemptResult(
        item_id="t-1",
        attempt=1,
        outcome=Outcome.BLOCKED,
        source_ids=("kb_search(query=cache)", "doc-abc#3"),
        retrieved=RetrievalState.NOT_RETRIEVED,
        retrieval_signal=None,
    )
    record = RunRecord(info=INFO, items=(), results=(result,))
    path = write_record(record, tmp_path)
    assert read_record(tmp_path, RUN_ID).results[0] == result
    raw = json.loads(path.read_text(encoding="utf-8"))["results"][0]
    assert raw["source_ids"] == ["kb_search(query=cache)", "doc-abc#3"]
    assert raw["retrieved"] == "not_retrieved"
    assert raw["retrieval_signal"] is None
    both = result.model_copy(
        update={"retrieved": RetrievalState.RETRIEVED, "retrieval_signal": RetrievalSignal.BOTH}
    )
    write_record(RunRecord(info=INFO, items=(), results=(both,)), tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))["results"][0]
    assert raw["retrieved"] == "retrieved"
    assert raw["retrieval_signal"] == "both"


def test_pre_m2_1_record_loads_and_renders(tmp_path: Path) -> None:
    text = PRE_M2_1.read_text(encoding="utf-8")
    raw = json.loads(text)
    for attempt in raw["results"]:  # this is what makes it a pre-M2.1 record
        assert not {"source_ids", "retrieved", "retrieval_signal"} & attempt.keys()
    run_id = raw["info"]["run_id"]
    runs = tmp_path / ".runs"
    runs.mkdir()
    (runs / f"{run_id}.json").write_text(text, encoding="utf-8")
    record = read_record(tmp_path, run_id)
    assert record.format == 1
    assert [r.item_id for r in record.results] == ["demo-001", "demo-001"]
    assert [(r.source_ids, r.retrieved, r.retrieval_signal) for r in record.results] == [
        ((), None, None),
        ((), None, None),
    ]
    report = build_report(
        record.info, record.items, record.results, record.self_test, generated_at="2026-11-22"
    )
    markdown = render_markdown(report)
    assert "| demo-001 |" in markdown
    assert "## Self-test (fake:obedient): passed" in markdown
    attempts = json.loads(render_json(report))["attempts"]
    assert [(a["source_ids"], a["retrieved"], a["retrieval_signal"]) for a in attempts] == [
        ([], None, None),
        ([], None, None),
    ]


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

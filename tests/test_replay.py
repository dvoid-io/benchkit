"""`benchkit replay`: re-derive a recorded run's scores offline and diff them."""

from __future__ import annotations

import dataclasses
import json

import pytest
from click.testing import CliRunner

from benchkit.cli import main
from benchkit.errors import UsageError
from benchkit.replay import load_recorded, replay_run, write_jsonl

CORRECT = {"toy_001": "Paris", "toy_002": "4"}
# what a real export records: every evaluator the run emitted, including benchkit's own
FULL_PASS = {"answer_matches": True, "confident": True, "contract_valid": True,
             "protocol_pass": True, "diagnostic_score": 1.0}


def _row(item_id: str, answer: str, confidence: float = 0.9, scores: dict | None = None) -> dict:
    return {
        "id": item_id,
        "output": {"answer": answer, "confidence": confidence},
        "scores": scores if scores is not None else {},
    }


def _write(tmp_path, rows, name="items.jsonl"):
    p = tmp_path / name
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return p


# --------------------------------------------------------------------- load


def test_load_recorded_accepts_a_file_or_its_directory(tmp_path):
    _write(tmp_path, [_row("toy_001", "Paris")])
    from_file = load_recorded(tmp_path / "items.jsonl")
    from_dir = load_recorded(tmp_path)
    assert [r.id for r in from_file] == [r.id for r in from_dir] == ["toy_001"]


def test_load_recorded_rejects_junk(tmp_path):
    (tmp_path / "items.jsonl").write_text("{not json}\n")
    with pytest.raises(UsageError):
        load_recorded(tmp_path)
    (tmp_path / "noid.jsonl").write_text(json.dumps({"output": {}}) + "\n")
    with pytest.raises(UsageError):
        load_recorded(tmp_path / "noid.jsonl")
    with pytest.raises(UsageError):
        load_recorded(tmp_path / "nope.jsonl")


# ------------------------------------------------------------------- replay


def test_replay_reproduces_scores_it_agrees_with(toy, tmp_path):
    """The core guarantee: unchanged evaluators over recorded outputs change nothing."""
    rows = [_row(i, a, scores=dict(FULL_PASS)) for i, a in CORRECT.items()]
    report = replay_run(toy, _write(tmp_path, rows), dataset_logical="draft")
    assert len(report.rows) == 2
    assert report.changed_rows == []
    assert all(r.scores["answer_matches"] is True for r in report.rows)
    assert "no evaluator totals moved" in report.format()


def test_replay_surfaces_scores_that_moved(toy, tmp_path):
    """A stale recorded score is reported per item and in the totals, with was→now."""
    rows = [_row("toy_001", "Paris", scores={**FULL_PASS, "answer_matches": False, "confident": False})]
    report = replay_run(toy, _write(tmp_path, rows), dataset_logical="draft")
    (row,) = report.rows
    assert row.changed["answer_matches"] == {"was": False, "now": True}
    assert row.changed["confident"] == {"was": False, "now": True}
    totals = report.totals()
    assert totals["answer_matches"] == {"was": 0, "was_n": 1, "now": 1, "now_n": 1, "delta": 1}
    text = report.format()
    assert "1 changed" in text and "answer_matches" in text


def test_replay_scores_a_wrong_output_as_failing(toy, tmp_path):
    rows = [_row("toy_001", "Berlin", scores=dict(FULL_PASS))]
    report = replay_run(toy, _write(tmp_path, rows), dataset_logical="draft")
    (row,) = report.rows
    assert row.scores["answer_matches"] is False
    assert row.scores["protocol_pass"] is False  # rubric.critical = [answer_matches]
    assert row.changed["answer_matches"] == {"was": True, "now": False}


def test_replay_reports_items_with_no_case(toy, tmp_path):
    rows = [_row("toy_001", "Paris"), _row("gone_042", "Paris")]
    report = replay_run(toy, _write(tmp_path, rows), dataset_logical="draft")
    assert report.unmatched == ["gone_042"]
    missing = [r for r in report.rows if r.missing_case]
    assert [r.id for r in missing] == ["gone_042"]
    assert "no case for: gone_042" in report.format()


def test_replay_does_not_let_one_broken_evaluator_hide_the_run(toy, tmp_path):
    broken = dataclasses.replace(toy, evaluators_ref="benchmarks.toy.evaluators:BROKEN_EVALUATORS")
    rows = [_row(i, a, scores=dict(FULL_PASS)) for i, a in CORRECT.items()]
    report = replay_run(broken, _write(tmp_path, rows), dataset_logical="draft")
    assert len(report.rows) == 2
    assert all("raises: RuntimeError: evaluator is mid-edit" in r.errors for r in report.rows)
    # the surviving evaluators still produced their verdicts
    assert all(r.scores["answer_matches"] is True for r in report.rows)
    assert "evaluator errors on 2 item(s)" in report.format()


def test_replay_only_changed_keeps_the_movers(toy, tmp_path):
    rows = [
        _row("toy_001", "Paris", scores=dict(FULL_PASS)),
        _row("toy_002", "4", scores={**FULL_PASS, "answer_matches": False}),
    ]
    report = replay_run(toy, _write(tmp_path, rows), dataset_logical="draft", only_changed=True)
    assert [r.id for r in report.rows] == ["toy_002"]


def test_replay_numeric_scores_compare_by_tolerance(toy, tmp_path):
    rows = [_row("toy_001", "Paris", scores=dict(FULL_PASS))]
    report = replay_run(toy, _write(tmp_path, rows), dataset_logical="draft")
    (row,) = report.rows
    assert row.scores["diagnostic_score"] == 1.0
    assert "diagnostic_score" not in row.changed


def test_write_jsonl_round_trips(toy, tmp_path):
    rows = [_row("toy_001", "Paris", scores={**FULL_PASS, "answer_matches": False})]
    report = replay_run(toy, _write(tmp_path, rows), dataset_logical="draft")
    out = tmp_path / "replayed.jsonl"
    write_jsonl(report, out)
    written = [json.loads(x) for x in out.read_text().splitlines()]
    assert written[0]["id"] == "toy_001"
    assert written[0]["recorded_scores"]["answer_matches"] is False
    assert written[0]["changed"]["answer_matches"] == {"was": False, "now": True}


# ---------------------------------------------------------------------- cli


def test_cli_replay(in_toybench, tmp_path, monkeypatch):
    monkeypatch.setattr("benchkit.manifest.__version__", "0.1.0")
    src = tmp_path / "run"
    src.mkdir()
    _write(src, [_row(i, a, scores=dict(FULL_PASS)) for i, a in CORRECT.items()])
    out = tmp_path / "replayed.jsonl"
    res = CliRunner().invoke(main, ["replay", "-b", "toy", "-d", "draft", "--run", str(src), "-o", str(out)])
    assert res.exit_code == 0, res.output
    assert f"wrote 2 row(s) to {out}" in res.output
    assert "2 item(s), 0 changed" in res.output
    assert len(out.read_text().splitlines()) == 2


def test_cli_replay_needs_an_existing_run(in_toybench, monkeypatch):
    monkeypatch.setattr("benchkit.manifest.__version__", "0.1.0")
    res = CliRunner().invoke(main, ["replay", "-b", "toy", "--run", "nope"])
    assert res.exit_code == 2


def test_replay_of_an_export_without_scores_reports_no_phantom_changes(toy, tmp_path):
    """A bare export (outputs only, no recorded scores) is scored, not diffed: there is
    nothing to have moved, so nothing is reported as a change."""
    rows = [_row(i, a) for i, a in CORRECT.items()]
    report = replay_run(toy, _write(tmp_path, rows), dataset_logical="draft")
    assert report.changed_rows == []
    assert all(r.scores["protocol_pass"] is True for r in report.rows)
    assert all(r.recorded_scores == {} and r.added == [] for r in report.rows)

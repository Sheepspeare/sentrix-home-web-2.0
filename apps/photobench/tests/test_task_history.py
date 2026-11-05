"""Task history is a read projection; resume provenance never alters results."""

import copy
import json

from test_run_read import _read_runs, _state, _write

from photobench.domain.runs.analysis import run_list_entry
from photobench.domain.runs.task_history import task_families
from photobench.infrastructure.results.store import read_entry, write_committed_snapshot


def test_three_resumes_group_before_pagination_without_combining_same_album(tmp_path):
    root = tmp_path / "results"
    for name, parent, date, count in [
        ("a", "", "01", 0),
        ("b", "a", "02", 1),
        ("c", "b", "04", 2),
        ("independent", "", "03", 0),
    ]:
        _write(
            root,
            name,
            _state(
                name,
                started_at=f"2026-01-{date}T00:00:00+08:00",
                resume_context={"resumed_from": parent, "inherited_qa_ids": list(range(count))},
            ),
        )
    reader = _read_runs(root)
    response = reader.list_runs(page_size=1, group_tasks=True)
    assert response["total"] == 2
    task = response["runs"][0]
    assert task["run_id"] == "c" and task["task_id"] == "a"
    assert task["task_run_ids"] == ["c", "b", "a"]
    assert [v["run_id"] for v in task["execution_versions"]] == ["a", "b", "c"]
    assert task["execution_versions"][-1]["inherited_count"] == 2
    assert reader.list_runs(page=2, page_size=1, group_tasks=True)["runs"][0]["run_id"] == "independent"
    assert reader.get_run("b", include_history=False)["task_id"] == "a"
    assert reader.list_runs()["total"] == 4


def test_scoring_missing_timestamp_stays_after_answers_and_deduplicates_inherited_task():
    task = {
        "rejudge_id": "score1",
        "status": "completed",
        "total": 85,
        "completed": 85,
        "qa_count": 53,
        "turn_count": 61,
        "results": [{"secret": "large"}],
    }
    first = _state("a", rejudge=task)
    second = _state(
        "b", rejudge=task, resume_context={"resumed_from": "a", "inherited_qa_ids": ["q1", "q1", "q2"]}
    )
    rows = [run_list_entry(second), run_list_entry(first)]
    before = copy.deepcopy(rows)
    versions = task_families(rows)[0]["execution_versions"]
    assert [v["execution_id"] for v in versions] == ["a", "score1", "b"]
    assert versions[1]["qa_count"] == 53 and versions[1]["total"] == 85
    assert "results" not in versions[1]
    assert versions[-1]["inherited_count"] == 2
    assert rows == before


def test_missing_parent_and_cycle_terminate_without_guessing():
    rows = [
        {"run_id": "a", "resume_context": {"resumed_from": "b"}},
        {"run_id": "b", "resume_context": {"resumed_from": "a"}},
        {"run_id": "c", "resume_context": {"resumed_from": "missing"}},
        {"run_id": "d"},
    ]
    families = task_families(rows)
    assert [f["task_id"] for f in families] == ["a", "missing", "d"]
    assert len(families[0]["execution_versions"]) == 2


def test_old_committed_entry_reads_resume_from_same_overview_without_rewriting(tmp_path):
    state = _state(
        "b",
        resume_context={"resumed_from": "a", "inherited_qa_ids": ["q1"]},
        rejudge={"rejudge_id": "s", "status": "completed"},
    )
    write_committed_snapshot(tmp_path, state)
    pointer = json.loads((tmp_path / "snapshot-commit.json").read_text())
    metadata = tmp_path / ".snapshots" / pointer["generation"] / "metadata.json"
    payload = json.loads(metadata.read_text())
    payload["entry"].pop("resume_context", None)
    payload["entry"].pop("scoring_executions", None)
    metadata.write_text(json.dumps(payload))
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    entry = read_entry(tmp_path)
    assert entry["resume_context"]["inherited_count"] == 1
    assert entry["scoring_executions"][0]["rejudge_id"] == "s"
    assert all(p.read_bytes() == data for p, data in before.items())


def test_filtered_page_and_detail_include_both_standards_without_extra_requests(tmp_path):
    from test_scoring_profiles import setup, state

    from photobench.domain.evaluation.profiles import ATM, OPEN

    saved = state()
    saved["run_id"] = "r"
    _, batches, _, _ = setup(saved, [OPEN, ATM])
    saved["scoring_results"] = {
        batches[0]["batch_id"] + ":q:0": {
            "profile_id": OPEN,
            "score": 2,
            "max_score": 2,
            "status": "completed",
            "purpose": "answer_quality",
        },
        batches[1]["batch_id"] + ":q:0": {
            "profile_id": ATM,
            "score": 0,
            "max_score": 1,
            "status": "completed",
            "purpose": "open",
        },
    }
    for result in saved["scoring_results"].values():
        result["qa_id"] = "q"
    _write(tmp_path, "r", saved)
    reader = _read_runs(tmp_path)
    page = reader.get_run_items("r", scoring_profile=OPEN, score="2")
    assert page["total"] == 1
    scores = page["items"][0]["profile_scores"]
    assert scores[OPEN]["score"] == 2 and scores[ATM]["score"] == 0
    detail = reader.get_run_item("r", 0, scoring_profile=ATM)["item"]
    assert detail["profile_scores"] == scores

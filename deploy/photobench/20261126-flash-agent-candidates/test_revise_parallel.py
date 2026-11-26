"""The parallel repair must never select the running v6 instance."""

from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "stage_name,run_suffix,previous_revision,previous_release",
    [
        ("parallel-v8", "fcc132", "4c823eac", "release"),
        ("parallel-v9", "0e7e57", "c4bd7f72", "release-c4bd7f72"),
        ("parallel-v10", "aa3735", "5c45b791", "release-5c45b791"),
        ("parallel-v11", "d11fd6", "f1dd6070", "release-f1dd6070"),
    ],
)
def test_revision_targets_only_the_paused_parallel_run(
    monkeypatch, stage_name, run_suffix, previous_revision, previous_release
):
    monkeypatch.syspath_prepend(str(Path(__file__).parent))
    import revise_parallel

    op = revise_parallel.configure(stage_name)
    assert op.candidate.GROUPS == {"large": (8300, 8880, 100, 600)}
    assert op.candidate.unit_name("large-api") == "sentrix-flash-query-v7-large-api.service"
    stage = op.STAGES[stage_name]
    assert stage["runs"]["large"].endswith(run_suffix)
    assert stage["previous_revision"].startswith(previous_revision)
    assert stage["previous_release"] == previous_release
    assert stage["answer_count"] == 16
    assert stage.get("protection_file", "intent.json") == (
        "large-v10-protected.json" if stage_name in {"parallel-v10", "parallel-v11"} else "intent.json"
    )
    assert op.BASE.name == "flash-query-v7-parallel-20261126"

"""The parallel repair must never select the running v6 instance."""

from pathlib import Path


def test_revision_targets_only_the_paused_parallel_run(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parent))
    import revise_parallel

    op = revise_parallel.configure()
    assert op.candidate.GROUPS == {"large": (8300, 8880, 100, 600)}
    assert op.candidate.unit_name("large-api") == "sentrix-flash-query-v7-large-api.service"
    stage = op.STAGES[revise_parallel.STAGE]
    assert stage["runs"]["large"].endswith("fcc132")
    assert stage["previous_revision"].startswith("4c823eac")
    assert stage["answer_count"] == 16
    assert op.BASE.name == "flash-query-v7-parallel-20261126"

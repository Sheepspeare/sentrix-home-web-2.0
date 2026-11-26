"""The compact revision only changes the reviewed fusion instance."""

import json
from pathlib import Path

import revise_fusion


def test_fusion_revision_preserves_scope_and_sets_eight_workers(tmp_path, monkeypatch):
    op = revise_fusion.revise_limited
    candidate = op.candidate
    for module, names in [(op, ("BASE",)), (candidate, ("BASE", "PREFIX", "GROUPS", "protected_processes"))]:
        for name in names:
            monkeypatch.setattr(module, name, getattr(module, name))
    monkeypatch.setattr(op, "STAGES", dict(op.STAGES))
    monkeypatch.setattr(revise_fusion.prepare_fusion, "BASE", tmp_path)
    (tmp_path / "release-newsha12").mkdir()
    (tmp_path / "large").mkdir()
    (tmp_path / "large/run-payload.json").write_text(
        json.dumps({"qa_concurrency": 2, "qa_priority_ids": ["same"]})
    )
    calls = []

    def run(revision, **kwargs):
        calls.append((revision, kwargs))
        assert candidate.GROUPS == {"large": (8303, 8883, 100, 600)}
        assert candidate.unit_name("large-api") == "sentrix-flash-condition-fusion-large-api.service"
        assert op.STAGES[revise_fusion.STAGE]["runs"]["large"].endswith("7dc9ca")
        assert op.STAGES[revise_fusion.STAGE]["answer_count"] == 16

    monkeypatch.setattr(op, "run", run)
    revise_fusion.run("newsha12", Path("committed-release"))
    assert calls[0][1]["workers"] == 8
    assert json.loads((tmp_path / "large/run-payload.json").read_text())["qa_priority_ids"] == ["same"]
    assert json.loads((tmp_path / "large/dispatch-control.json").read_text()) == {
        "concurrency": 8,
        "pause_after_completed": 16,
    }

import prepare_fusion


def test_fusion_prepares_only_its_new_instance(monkeypatch):
    parallel = prepare_fusion.prepare_parallel
    # Restore module globals even though the real CLI gets a fresh process.
    for name in ("BASE", "PREFIX", "PORTS", "candidate_module"):
        monkeypatch.setattr(parallel, name, getattr(parallel, name))
    calls = []

    def prepare(release, payload):
        module = parallel.candidate_module()
        calls.append((release, payload, module.BASE, module.GROUPS, module.unit_name("large-api")))
        outgoing = parallel.payload_for_parallel({"qa_priority_ids": ["same"], "reasoning": True})
        assert outgoing["sentrix_url"] == "http://127.0.0.1:8303"
        assert outgoing["qa_concurrency_control_path"] == str(
            prepare_fusion.BASE / "large/dispatch-control.json"
        )
        assert outgoing["qa_priority_ids"] == ["same"]

    monkeypatch.setattr(parallel, "prepare", prepare)
    prepare_fusion.run("committed-release", "payload")
    assert calls == [
        (
            "committed-release",
            "payload",
            prepare_fusion.BASE,
            {"large": (8303, 8883, 100, 600)},
            "sentrix-flash-condition-fusion-large-api.service",
        )
    ]

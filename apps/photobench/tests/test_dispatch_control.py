import json

import pytest

from photobench.application.run_execution.dispatch_control import DispatchControl


def test_preflight_gate_admits_four_then_resumes_without_repeating(tmp_path):
    path = tmp_path / "control.json"
    path.write_text(json.dumps({"concurrency": 4, "pause_after_completed": 4}))
    control = DispatchControl(str(path), 4)
    assert control.admission(completed=0, inflight=3)
    assert not control.admission(completed=1, inflight=3)
    assert not control.admission(completed=4, inflight=0)
    assert control.paused
    path.write_text(json.dumps({"concurrency": 4}))
    assert control.admission(completed=4, inflight=0)
    assert not control.paused


def test_lower_concurrency_drains_inflight_and_refuses_increase(tmp_path):
    path = tmp_path / "control.json"
    path.write_text(json.dumps({"concurrency": 2}))
    control = DispatchControl(str(path), 4)
    assert not control.admission(completed=2, inflight=3)
    assert control.admission(completed=4, inflight=1)
    path.write_text(json.dumps({"concurrency": 3}))
    with pytest.raises(ValueError, match="decrease"):
        control.admission(completed=4, inflight=1)
    path.write_text(json.dumps({"concurrency": 1}))
    with pytest.raises(ValueError, match="minimum 2"):
        control.admission(completed=4, inflight=0)

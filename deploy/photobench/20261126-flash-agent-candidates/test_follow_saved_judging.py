import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "follow_saved_judging", Path(__file__).with_name("follow_saved_judging.py")
)
FOLLOW = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FOLLOW)


def test_only_new_available_answers_are_selected_in_frozen_order():
    assert FOLLOW.missing_ids(["a", "b", "c", "d"], {"a", "c", "d"}, {("a", 0): {}}) == ["c", "d"]


def test_success_is_reused_and_failed_score_remains_missing():
    good = {
        "qa_id": "a",
        "turn_index": 0,
        "profile_id": "atm_qs_v1",
        "status": "completed",
        "diagnostic_key": "valid",
        "normalized_score": 0,
    }
    state = {
        "scoring_results": {"a": good, "b": {"status": "failed"}},
        "diagnostic_results": {"valid": {"status": "completed"}},
    }
    scores = {}
    FOLLOW.merge_scores(scores, state)
    FOLLOW.merge_scores(scores, state)
    assert scores == {("a", 0): good}
    assert FOLLOW.missing_ids(["a", "b"], {"a", "b"}, scores) == ["b"]


def test_incomplete_diagnostic_is_not_a_valid_score():
    with pytest.raises(AssertionError):
        FOLLOW.merge_scores(
            {},
            {
                "scoring_results": {
                    "a": {
                        "qa_id": "a",
                        "turn_index": 0,
                        "profile_id": "atm_qs_v1",
                        "status": "completed",
                        "diagnostic_key": "invalid",
                    }
                },
                "diagnostic_results": {"invalid": {"status": "failed"}},
            },
        )

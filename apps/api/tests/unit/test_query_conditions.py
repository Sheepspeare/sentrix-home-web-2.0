"""条件理解、字段角色及执行边界；不读取基准答案。"""

import json
from datetime import datetime

import pytest

from sentrix.domain.assistant.intent import parse_declaration, planner_response_format
from sentrix.domain.retrieval.query_conditions import QueryCondition, executable_conditions, parse_conditions
from sentrix.domain.retrieval.time_expression import parse_time_expression

NOW = datetime(2026, 11, 26)


@pytest.mark.parametrize(
    "expression,start,end",
    [
        ("2024年2月下旬", "2024-02-21", "2024-03-01"),
        ("2022年3月中旬", "2022-03-11", "2022-03-21"),
        ("2022年3月21日至3月31日", "2022-03-21", "2022-04-01"),
        ("2022-03-21-2022-03-31", "2022-03-21", "2022-04-01"),
        ("2022-03-21/2022-04-01", "2022-03-21", "2022-04-01"),
        ("2022年12月31日至2023年1月2日", "2022-12-31", "2023-01-03"),
    ],
)
def test_calendar_bounds_preserve_the_complete_expression(expression, start, end):
    assert parse_time_expression(expression) == (datetime.fromisoformat(start), datetime.fromisoformat(end))


@pytest.mark.parametrize(
    "expression", ["2023-02-30", "2022年13月", "2022年3月某个周末", "3月21日", "2022-04-03/2022-04-01"]
)
def test_unresolved_or_invalid_time_never_becomes_a_broader_month(expression):
    assert parse_time_expression(expression) is None


def test_unsupported_place_roles_do_not_filter_by_capture_gps():
    for role in ("mentioned", "venue", "origin", "destination"):
        result = executable_conditions([QueryCondition("place", "Marseille", role)], now=NOW, exact=False)
        assert not result.filters
        assert result.pending[0][1] == "place_role_requires_content_evidence"


def test_capture_place_does_not_need_a_person_condition():
    result = executable_conditions([QueryCondition("place", "Porto", "capture")], now=NOW, exact=False)
    assert result.filters == {"place": "Porto"}


def test_pending_event_and_quantity_cannot_be_replaced_by_asset_statistics():
    result = executable_conditions(
        [
            QueryCondition("time", "第三天", "capture", state="pending", anchor="trip"),
            QueryCondition("quantity", "罐", "item"),
        ],
        now=NOW,
        exact=True,
    )
    assert not result.report()["answer_eligible"]
    assert len(result.pending) == 2


def test_photographer_is_preserved_as_a_required_unindexed_role():
    condition = QueryCondition("person", "我", "photographer")
    result = executable_conditions([condition], now=NOW, exact=False)
    assert result.pending == ((condition, "identity_role_not_indexed"),)
    assert not result.report()["answer_eligible"]
    narrator = QueryCondition("person", "我", "narrator", required=False)
    assert executable_conditions([narrator], now=NOW, exact=False).report()["answer_eligible"]


def test_relation_role_is_validated_against_dimension():
    with pytest.raises(ValueError, match="role"):
        parse_conditions([{"dimension": "place", "value": "我", "role": "photographer"}])


def test_planner_keeps_role_and_relation_without_turning_photographer_into_a_face_filter():
    conditions = [QueryCondition("person", "妈妈", "photographer", anchor="我", relation="母亲")]
    raw = {
        "action": "declare",
        "declaration": {
            "goal": "妈妈拍的照片",
            "requirements": [
                {
                    "id": "p",
                    "evidence_type": "photo_set",
                    "description": "妈妈拍的明明",
                    "predicate": "照片",
                    "value_type": "media_set",
                    "scope": {},
                    "required": True,
                    "conditions": [item.payload() for item in conditions],
                }
            ],
        },
    }
    requirements, error = parse_declaration(json.dumps(raw), scope_id="home", question="妈妈拍的明明")
    assert not error and requirements is not None
    assert requirements[0].conditions == tuple(conditions)
    assert "person" not in dict(requirements[0].scope)
    fields = planner_response_format()["json_schema"]["schema"]["properties"]["declaration"]["properties"][
        "requirements"
    ]["items"]
    assert "conditions" in fields["required"]


def test_time_intersection_keeps_timezone_and_rejects_incompatible_calendars():
    conditions = [
        QueryCondition("time", "2024年3月[Asia/Shanghai]", "capture"),
        QueryCondition("time", "2024年3月下旬[Asia/Shanghai]", "capture"),
    ]
    result = executable_conditions(conditions, now=NOW, exact=True)
    assert result.filters["time"] == "2024-03-21/2024-04-01[Asia/Shanghai]"
    assert not result.pending
    conditions[1] = QueryCondition("time", "2024年3月下旬", "capture")
    result = executable_conditions(conditions, now=NOW, exact=True)
    assert result.pending[0][1] == "incompatible_timezones"
    assert not result.report()["answer_eligible"]

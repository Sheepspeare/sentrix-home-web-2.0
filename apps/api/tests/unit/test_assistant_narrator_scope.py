"""第一人称活动检索与出镜人物筛选采用不同的条件。"""

import json

import pytest

from sentrix.application.assistant.goal_planner import GoalPlanner
from sentrix.domain.assistant.intent import parse_declaration
from sentrix.domain.assistant.models import ModelReply


def _declaration(person: str, evidence_type: str = "photo_set") -> str:
    return json.dumps(
        {
            "action": "declare",
            "declaration": {
                "goal": "查找相关记录",
                "requirements": [
                    {
                        "id": "req_1",
                        "evidence_type": evidence_type,
                        "description": "查找活动时间",
                        "predicate": "发生时间",
                        "value_type": "time",
                        "scope": {"person": person, "event": "制作酱油炖鸡翅"},
                        "required": True,
                    }
                ],
            },
        },
        ensure_ascii=False,
    )


@pytest.mark.parametrize(
    ("person", "question"),
    [
        (
            "我",
            "Today is Feb 9th, 2023. When did I most recently make myself some delicious "
            "soy-source braised chicken wings?",
        ),
        ("我", "我上次做酱油炖鸡翅是什么时候？"),
        ("本人", "我曾经制作的蛋糕有哪些？"),
        ("I", "When did I cook chicken?"),
        ("my", "Find the food pictures in my album."),
        ("me", "When was this cake made by me?"),
        ("myself", "When did I last cook myself a meal?"),
        ("我的", "我的相册里有哪些餐厅？"),
    ],
)
def test_narrator_activity_does_not_require_resolving_a_face_name(person, question):
    requirements, reason = parse_declaration(_declaration(person), scope_id="album", question=question)

    assert reason == ""
    assert requirements is not None
    assert dict(requirements[0].scope) == {"event": "制作酱油炖鸡翅"}


@pytest.mark.parametrize(
    ("person", "question"),
    [
        ("Alice", "When did I cook dinner with Alice?"),
        ("我和Alice", "找我和Alice的合照"),
        ("我", "我的相册里我出镜的照片有哪些？"),
        ("我", "我做饭时穿着什么衣服？"),
        ("me", "Find photos showing me in my album."),
        ("myself", "Show pictures of me from my album."),
        ("me", "Find photos with me and Alice in my album."),
        ("I", "What was I wearing when I cooked?"),
        ("我", "我是谁？"),
        ("me", "What can you tell me about me?"),
    ],
)
def test_names_self_appearance_and_ambiguous_self_constraints_are_preserved(person, question):
    requirements, reason = parse_declaration(_declaration(person), scope_id="album", question=question)

    assert reason == ""
    assert requirements is not None
    assert dict(requirements[0].scope)["person"] == person


@pytest.mark.parametrize("evidence_type", ["person_profile", "photo_identity"])
def test_identity_requirements_keep_self_constraint_even_in_activity_question(evidence_type):
    requirements, reason = parse_declaration(
        _declaration("我", evidence_type), scope_id="album", question="我做饭时是谁给我拍的照？"
    )

    assert reason == ""
    assert requirements is not None
    assert dict(requirements[0].scope)["person"] == "我"


def test_goal_planner_retains_raw_declaration_and_returns_normalized_requirements():
    raw = _declaration("我")

    class Model:
        def complete(self, messages, tools):
            return ModelReply(content=raw)

    result = GoalPlanner(Model()).declare("我上次做鸡翅是什么时候？", scope_id="album")

    assert result.ok
    assert result.raw == raw
    assert len(result.model_calls) == 1
    assert dict(result.requirements[0].scope) == {"event": "制作酱油炖鸡翅"}

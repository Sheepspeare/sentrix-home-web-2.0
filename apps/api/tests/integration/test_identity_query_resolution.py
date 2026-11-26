"""真实目录与有效关系边：昵称、我锚点和多段关系。"""

import pytest

from sentrix.application.identity.person_directory import EntityPersonDirectory
from sentrix.domain.assistant.models import AmbiguousPersonName
from sentrix.infrastructure.persistence.repositories.entities import SqliteEntityRepository
from sentrix.infrastructure.persistence.repositories.family import SqliteFamilyGraphRepository


@pytest.fixture
def directory(migrations_ready_db):
    db = migrations_ready_db
    stamp = "2026-11-26T00:00:00+00:00"
    with db.connect() as connection, connection:
        connection.execute(
            "INSERT INTO memory_spaces(id,name,include_in_people,created_at,updated_at) "
            "VALUES('home','家',1,?,?)",
            (stamp, stamp),
        )
        for identifier, name, role in (
            ("me", "小张", "自己"),
            ("mother", "张母", "母亲"),
            ("aunt", "阿姨", "姐姐"),
            ("ming", "张明", None),
        ):
            connection.execute(
                "INSERT INTO entities(id,scope_id,entity_type,canonical_name,status,family_role,"
                "created_at,updated_at) VALUES(?,'home','person',?,'confirmed',?,?,?)",
                (identifier, name, role, stamp, stamp),
            )
    entities = SqliteEntityRepository(db)
    entities.set_person_aliases("ming", ("明明",))
    family = SqliteFamilyGraphRepository(db, entities)
    family.set_relationship("home", "mother", "母亲", "me", "儿子", source="user_override")
    family.set_relationship("home", "aunt", "姐姐", "mother", "妹妹", source="user_override")
    return EntityPersonDirectory(entities=entities, family=family), entities, family


def test_alias_self_anchor_and_direction_of_relationships(directory):
    people, _, _ = directory
    assert people.resolve_identity("明明", "home").entity_id == "ming"
    assert people.resolve_identity("我", "home").entity_id == "me"
    assert people.resolve_identity("妈妈", "home", anchor="我", relation="母亲").entity_id == "mother"
    assert people.resolve_identity("我妈妈", "home").entity_id == "mother"
    assert (
        people.resolve_identity("我妈妈的姐姐", "home", anchor="我", relation="母亲/姐姐").entity_id == "aunt"
    )
    assert people.resolve_identity("张明的妈妈", "home", anchor="明明", relation="母亲") is None


def test_missing_self_anchor_and_no_identity_data_are_distinct(directory):
    people, _, _ = directory
    assert people.identity_capabilities("absent")["named_people"] is False
    assert people.resolve_identity("我妈妈", "absent") is None
    capabilities = people.identity_capabilities("home")
    assert capabilities["self_anchor"] is True
    assert capabilities["photographer"] == "not_indexed"
    assert capabilities["anonymous_faces"] == "available_independently"


def test_ambiguous_relation_is_not_resolved_to_the_first_candidate(directory):
    people, _, family = directory
    family.set_relationship("home", "aunt", "母亲", "me", "儿子", source="user_override")
    with pytest.raises(AmbiguousPersonName):
        people.resolve_identity("我妈妈", "home")

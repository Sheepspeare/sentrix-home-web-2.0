"""已确认人物的目录：把实体仓储适配成助手工具要的档案。

S3 装配的是 ``NoIdentityDirectory``——那时身份层还没建起来，「查不到」与「没有这个人」
是同一件事，如实回 ``None`` 是唯一诚实的结果。S5 起库里有真的已确认人物，所以这条查询
有了真答案。**工具契约、账本与门禁一行都没动**（S3 那个占位实现的模块注释当初就是这么
约定的）：换掉的只是容器里那一个字段的实参。

**只认已确认的人。** ``confirmed_people_named`` 先查本名，再查生效的别名。全相册只看
对人物视图开放的空间，查到多位则要求澄清。未确认的候选人物簇
不进这个目录：那还只是一次聚类猜测，把它当档案答出去，等于让猜测变成检索事实
（``link_confirmed_mention`` 拒绝未确认实体，是同一条理由）。

**状态码与错误码不在这里。** 这个端口只有一个答案形状（``PersonProfile | None``），
「没有这个人」不是错误，是 ``None``——工具据此输出 gap 而不是抛异常。

**S7c 起它同时回答「这个人在家庭图谱里的归属与关系」。** 1.0 的同一件事在
``db.py:2840`` 的 ``person_profile_digest`` 里做，读的正是
``family_person_memberships`` / ``family_relationships`` / ``family_person_portraits``
三张表——**不是**通用 ``relationships`` 表。这里照同一条口径。
富化放在目录里而不是助手用例里：application 层只能依赖 domain 端口
（``application-purity`` 契约禁止 import ``sentrix.infrastructure``），
而「我们知道这个人的什么」本来就该由目录答。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime

from sentrix.domain.assistant.models import AmbiguousPersonName, PersonProfile, PersonRelationship
from sentrix.domain.assistant.ports import PersonDirectory
from sentrix.domain.identity.family import SOURCE_USER_OVERRIDE
from sentrix.domain.identity.models import ENTITY_STATUS_CONFIRMED, PENDING_CLUSTER_NAME, entity_display_name
from sentrix.domain.identity.ports import EntityRepository, FamilyGraphRepository, PortraitRepository

# 对方实体被删、没名字或还没确认时用它，**不编造**，也不暴露内部 cluster id。
UNNAMED_MEMBER = PENDING_CLUSTER_NAME


def assets_for_names(
    directory: PersonDirectory, names: Sequence[str], scope_id: str
) -> tuple[str, ...] | None:
    assets: tuple[str, ...] | None = None
    for name in dict.fromkeys(name.strip() for name in names if name.strip()):
        person = directory.lookup(name, scope_id)
        if person is None or not person.confirmed or not person.entity_id:
            return ()
        matching = directory.asset_ids(person)
        if assets is None:
            assets = matching
        else:
            ids = set(matching)
            assets = tuple(asset_id for asset_id in assets if asset_id in ids)
    return assets


class EntityPersonDirectory:
    def __init__(
        self,
        *,
        entities: EntityRepository,
        family: FamilyGraphRepository,
        portraits: PortraitRepository | None = None,
    ) -> None:
        self._entities = entities
        self._family = family
        self._portraits = portraits

    def lookup(self, name: str, scope_id: str, *, include_portrait: bool = False) -> PersonProfile | None:
        clean_name = str(name or "").strip()
        if not clean_name:
            return None
        candidates = self._entities.confirmed_people_named(scope_id, clean_name)
        if not candidates:
            return None
        if len(candidates) > 1:
            raise AmbiguousPersonName(clean_name)
        (entity,) = candidates
        scope_id = entity.scope_id
        membership = self._family.get_effective_membership(scope_id, entity.id)
        summary, photo_portrait = (
            self._current_portraits(scope_id, entity.id, entity.updated_at) if include_portrait else ("", "")
        )
        return PersonProfile(
            # 报**实体的本名**而不是查询串：按别名查到时，用户问的是别名、答案里的名字
            # 得是本名，否则模型会把这个人的两个名字当成两个人。
            name=entity.canonical_name,
            confirmed=True,
            # 1.0 ``db.py:2875-2876`` 的 ``or "unknown"`` / ``or ""``：没有归属行与
            # 「归属不明」是同一件事，不区分。
            membership=(membership.membership if membership is not None else "") or "unknown",
            membership_source=(membership.source if membership is not None else "") or "",
            summary=summary,
            summary_source=SOURCE_USER_OVERRIDE if summary else "",
            photo_portrait=photo_portrait,
            relationships=self._relationships(scope_id, entity.id),
            entity_id=entity.id,
            scope_id=scope_id,
            aliases=self._entities.person_aliases(entity.id),
            family_role_label=entity.family_role or "",
        )

    def identity_capabilities(self, scope_id: str) -> dict[str, object]:
        people = [
            entity
            for entity in self._entities.list_entities(
                status=ENTITY_STATUS_CONFIRMED, scope_id=scope_id or None
            )
            if entity.entity_type == "person"
        ]
        return {
            "named_people": bool(people),
            "self_anchor": any(entity.family_role == "自己" for entity in people),
            "relationships": any(
                self._family.list_effective_relationships(entity.scope_id, entity.id) for entity in people
            ),
            "photographer": "not_indexed",
            "anonymous_faces": "available_independently",
        }

    def resolve_identity(
        self, value: str, scope_id: str, *, anchor: str = "", relation: str = "is"
    ) -> PersonProfile | None:
        # 名称/昵称优先，只有明确关系表达才沿图走，不能从查无姓名猜关系。
        if relation == "is" and not anchor:
            if value.casefold() not in {"我", "本人", "自己", "me", "myself", "self"}:
                direct = self.lookup(value, scope_id)
                if direct is not None:
                    return direct
                relative = re.fullmatch(
                    r"(?:我的?|my\s+)(妈妈|母亲|爸爸|父亲|mother|mom|father|dad)", value, re.I
                )
                if relative is None:
                    return None
                anchor, relation = "我", relative[1]
            else:
                anchor = "我"
        if anchor.casefold() in {"我", "本人", "自己", "me", "myself", "self"}:
            candidates = [
                entity
                for entity in self._entities.list_entities(
                    status=ENTITY_STATUS_CONFIRMED, scope_id=scope_id or None
                )
                if entity.entity_type == "person" and entity.family_role == "自己"
            ]
            if len(candidates) > 1:
                raise AmbiguousPersonName(anchor)
            current = candidates[0] if candidates else None
        else:
            profile = self.lookup(anchor, scope_id)
            current = self._entities.get(profile.entity_id) if profile else None
        if current is None:
            return None
        aliases = {
            "妈妈": "母亲",
            "mother": "母亲",
            "mom": "母亲",
            "爸爸": "父亲",
            "father": "父亲",
            "dad": "父亲",
        }
        path = [] if relation == "is" else relation.split("/")
        visited = {current.id}
        for step in path:
            predicate = aliases.get(step.strip().casefold(), step.strip())
            # 图边的方向是subject为object的predicate，因此“我的妈妈”读指向我的母亲边。
            ids = {
                row.subject_entity_id
                for row in self._family.list_effective_relationships(current.scope_id, current.id)
                if row.object_entity_id == current.id and row.predicate == predicate
            }
            matches = [
                entity
                for identifier in ids
                if (entity := self._entities.get(identifier)) is not None
                and entity.status == ENTITY_STATUS_CONFIRMED
                and entity.entity_type == "person"
                and entity.scope_id == current.scope_id
            ]
            if len(matches) > 1:
                raise AmbiguousPersonName(value)
            if not matches or matches[0].id in visited:
                return None
            current = matches[0]
            visited.add(current.id)
        # 按稳定ID构建档案，避免同名实体再次通过名字查询产生歧义。
        membership = self._family.get_effective_membership(current.scope_id, current.id)
        return PersonProfile(
            name=current.canonical_name,
            confirmed=True,
            entity_id=current.id,
            scope_id=current.scope_id,
            aliases=self._entities.person_aliases(current.id),
            family_role_label=current.family_role or "",
            membership=membership.membership if membership else "unknown",
            relationships=self._relationships(current.scope_id, current.id),
        )

    def _current_portraits(
        self, scope_id: str, entity_id: str, entity_updated_at: datetime | None
    ) -> tuple[str, str]:
        def aware(value: datetime) -> datetime:
            return value if value.tzinfo else value.replace(tzinfo=UTC)

        latest_edit = max(
            (
                aware(stamp)
                for stamp in (entity_updated_at, self._family.latest_user_edit_at(scope_id, entity_id))
                if stamp is not None
            ),
            default=None,
        )

        def current(created_at: datetime | None) -> bool:
            return created_at is not None and (latest_edit is None or aware(created_at) >= latest_edit)

        photo = self._portraits.get_active(entity_id) if self._portraits is not None else None
        if photo and photo.scope_id == scope_id and current(photo.created_at) and photo.portrait_text.strip():
            return "", photo.portrait_text.strip()
        family = self._family.get_active_portrait(scope_id, entity_id)
        # Legacy generated family text contains whole-image observations and cached relationships.
        if family and family.source == SOURCE_USER_OVERRIDE and current(family.created_at):
            return family.portrait_text.strip(), ""
        return "", ""

    def asset_ids(self, person: PersonProfile) -> tuple[str, ...]:
        return self._entities.asset_ids_for_person(person.entity_id, person.scope_id)

    def _relationships(self, scope_id: str, entity_id: str) -> tuple[PersonRelationship, ...]:
        """只收**此人是 subject** 的边——``predicate`` 才是「此人是对方的什么」。

        2.0 的 ``set_relationship`` **一次写正向 + 反向两行**（F7 实测确认），不过滤的
        话同一段关系会出现两次（一次 ``父亲``、一次 ``儿子``），模型会把一个人说成两个身份。

        **1.0 的同一处过滤是残的**（``db.py:2852-2858``）：那边一段关系只按一个方向存，
        于是查「女儿」的档案拿不到父亲——而 1.0 的测试恰好只测了父为 subject 的方向，
        把它盖住了。2.0 因为双向存储，**同一个过滤拿到的反而是完整的关系集**。

        仓储那层是双向匹配（``subject_entity_id = ? OR object_entity_id = ?``），
        过滤放在这里、不放进仓储：仓储的那个方法别的消费方（图谱页）要的是全部边。
        """
        found: list[PersonRelationship] = []
        for row in self._family.list_effective_relationships(scope_id, entity_id):
            if row.subject_entity_id != entity_id:
                continue
            other = self._entities.get(row.object_entity_id)
            found.append(
                PersonRelationship(
                    predicate=row.predicate,
                    other_name=(UNNAMED_MEMBER if other is None else entity_display_name(other)),
                    source=row.source,
                    other_aliases=(
                        self._entities.person_aliases(other.id)
                        if other is not None and other.status == ENTITY_STATUS_CONFIRMED
                        else ()
                    ),
                )
            )
        return tuple(found)

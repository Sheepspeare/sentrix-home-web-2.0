"""四个工具执行器的行为：真 SQLite 向量库 + 真结果集仓储 + 脚本化嵌入。

这里钉的是每个工具的**信息边界**——``observation``（内部）与 ``model_visible``（发回模型的）
两份投影差在哪。1.0 靠 ``ToolPolicy._sanitize`` 与 ``_model_visible_observation`` 两层过滤
做同一件事，但没有任何一处解释为什么需要两层；这里把它写成可断言的事实。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sentrix.application.assistant.query_memory_facts import QueryMemoryFacts
from sentrix.application.assistant.tools import AssistantToolbox, is_empty_result
from sentrix.application.identity.person_directory import EntityPersonDirectory
from sentrix.application.retrieval.evaluate_conditions import EvaluateConditions
from sentrix.application.retrieval.retrieve_entity import RetrieveEntity
from sentrix.application.retrieval.retrieve_evidence import RetrieveEvidence
from sentrix.application.retrieval.retrieve_lexical import RetrieveLexical
from sentrix.application.retrieval.retrieve_metadata import RetrieveMetadata
from sentrix.application.retrieval.retrieve_text_ann import RetrieveTextAnn
from sentrix.application.retrieval.retrieve_visual import RetrieveVisual
from sentrix.domain.assistant.claim_verifier import ClaimReview, ClaimVerdict
from sentrix.domain.assistant.evidence_ledger import EvidenceRequirement, EvidenceType
from sentrix.domain.assistant.models import PersonProfile, PersonRelationship
from sentrix.domain.assistant.result_set import PAGE_SIZE, ResultSet
from sentrix.domain.assistant.task_state import ToolCallRecord
from sentrix.domain.identity.models import ENTITY_STATUS_CONFIRMED, FACE_VALIDITY_VERIFIED
from sentrix.domain.media.models import Asset, AssetStatus, GpsPoint, MediaType, Observation
from sentrix.domain.places.aliases import EMPTY_ALIASES
from sentrix.domain.places.models import (
    AdminArea,
    NearestPlace,
    Place,
    PlacePrecision,
    PlaceSource,
)
from sentrix.domain.retrieval import scheme
from sentrix.domain.retrieval.ports import VectorRecord
from sentrix.domain.retrieval.query_conditions import QueryCondition
from sentrix.infrastructure.persistence.repositories.asset_places import SqliteAssetPlaceRepository
from sentrix.infrastructure.persistence.repositories.assets import (
    SqliteAssetRepository,
    SqliteObservationRepository,
)
from sentrix.infrastructure.persistence.repositories.entities import SqliteEntityRepository
from sentrix.infrastructure.persistence.repositories.faces import SqliteFaceRepository
from sentrix.infrastructure.persistence.repositories.family import SqliteFamilyGraphRepository
from sentrix.infrastructure.persistence.repositories.result_sets import SqliteResultSetRepository
from sentrix.infrastructure.persistence.repositories.search_terms import SqliteLexicalIndex
from sentrix.infrastructure.persistence.repositories.structured import (
    SqliteStructuredQueryRepository,
)
from sentrix.infrastructure.persistence.repositories.text_vectors import (
    SqliteTextVectorRepository,
)
from sentrix.infrastructure.persistence.repositories.vectors import SqliteVectorRepository
from sentrix.infrastructure.persistence.repositories.video import SqliteVideoRepository

NOW = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)
SCOPE = "album1"


class ScriptedEmbedder:
    """文本与图片都映射到指定向量——让「哪张图排第一」是构造出来的。"""

    model_id = scheme.IMAGE_MODEL
    dimension = 4

    def embed_query(self, text: str) -> Sequence[float]:
        digest = hashlib.sha256(text.encode()).digest()
        return [digest[0] / 255, digest[1] / 255, 0.0, 0.0]

    def embed_image(self, path: Path) -> Sequence[float]:
        digest = hashlib.sha256(Path(path).read_bytes()).digest()
        return [digest[0] / 255, digest[1] / 255, 0.0, 0.0]


class ScriptedTextEmbedder:
    """文本通道的假嵌入器。**维度与 ScriptedEmbedder 故意不同**（3 vs 4）——
    两个通道共用一个假件时，把维度写成一样会让「查错空间」这件事测不出来。
    """

    model_id = scheme.TEXT_MODEL
    dimension = 3

    def embed_query(self, text: str) -> Sequence[float]:
        digest = hashlib.sha256(text.encode()).digest()
        return [digest[0] / 255, digest[1] / 255, digest[2] / 255]


class StubInspector:
    """`inspect_photo` 的模型入口。这个夹具不调它，所以只要求它叫得应。"""

    def inspect(self, *args: object, **kwargs: object) -> None:  # pragma: no cover
        raise AssertionError("这个夹具不该调 inspect_photo")


class StubCrops:
    """目标人物的裁剪件。同上。"""

    def render(self, *args: object, **kwargs: object) -> None:  # pragma: no cover
        raise AssertionError("这个夹具不该裁图")


class StubPeople:
    def __init__(self, profiles: dict[str, PersonProfile] | None = None) -> None:
        self._profiles = profiles or {}

    def lookup(self, name: str, scope_id: str, *, include_portrait: bool = False) -> PersonProfile | None:
        return self._profiles.get(name)


@pytest.fixture
def toolbox(migrations_ready_db, tmp_path):
    assets = SqliteAssetRepository(migrations_ready_db)
    vectors = SqliteVectorRepository(migrations_ready_db)
    text_vectors = SqliteTextVectorRepository(migrations_ready_db)
    result_sets = SqliteResultSetRepository(migrations_ready_db)
    media = tmp_path / "media"
    media.mkdir()
    for index, word in enumerate(("海边", "雪山")):
        path = media / f"{word}.jpg"
        path.write_bytes(f"图片字节 {word}".encode())
        asset = Asset(
            id=f"asset_{index}",
            scope_id=SCOPE,
            file_name=f"{word}.jpg",
            media_type=MediaType.IMAGE,
            path=str(path),
            mime_type="image/jpeg",
            size_bytes=path.stat().st_size,
            status=AssetStatus.PROCESSED,
            content_sha256=f"hash{index}",
            captured_at=datetime(2019, 8, index + 1, tzinfo=UTC),
            captured_location=GpsPoint(30.0, 120.0),
            created_at=NOW,
            updated_at=NOW,
        )
        assets.add(asset)
        vectors.upsert(
            VectorRecord(
                scope_id=SCOPE,
                space=scheme.VISUAL_SPACE,
                source_type="asset",
                source_id=asset.id,
                model_name=scheme.IMAGE_MODEL,
                vector=tuple(ScriptedEmbedder().embed_query(word)),
            )
        )

    observations = SqliteObservationRepository(migrations_ready_db)
    places = SqliteAssetPlaceRepository(migrations_ready_db)
    # 三条召回通道与 HTTP 端点用的是同一套编排（1.0 里两个入口共用同一个 Kernel）。
    # 这个夹具不落 Observation，所以词法与元数据两条通道恒空——被测的是工具的信息
    # 边界，不是召回；通道自己的行为在 test_retrieve_evidence.py。
    entities = SqliteEntityRepository(migrations_ready_db)
    build = AssistantToolbox(
        retrieval=RetrieveEvidence(
            entity=RetrieveEntity(
                index=SqliteLexicalIndex(migrations_ready_db, observations, assets=assets, places=places),
                observations=observations,
            ),
            visual=RetrieveVisual(embedder=ScriptedEmbedder(), vectors=vectors, index=None),
            lexical=RetrieveLexical(
                index=SqliteLexicalIndex(migrations_ready_db, observations, assets=assets, places=places)
            ),
            metadata=RetrieveMetadata(assets=assets, places=places, aliases=EMPTY_ALIASES),
            text=RetrieveTextAnn(embedder=ScriptedTextEmbedder(), vectors=text_vectors),
        ),
        assets=assets,
        result_sets=result_sets,
        people=StubPeople(
            {
                "妈妈": PersonProfile(
                    name="妈妈",
                    confirmed=True,
                    membership="family",
                    membership_source="user_override",
                    summary="家庭归属：family；近期记忆：一起在公园野餐。",
                    relationships=(PersonRelationship(predicate="母亲", other_name="小明", source="model"),),
                ),
                # **一条关系都没有的人**：实测那份 53 张照片的相册就是 8 个人 0 条关系，
                # 所以这不是边角情形，是常态。他查得到，只是没关系数据。
                "成员一": PersonProfile(name="成员一", confirmed=True),
            }
        ),
        memory_facts=QueryMemoryFacts(
            structured=SqliteStructuredQueryRepository(
                migrations_ready_db,
                # 与 metadata 通道用同一份空别名表：这个夹具测的是工具的信息边界，
                # 不是地点匹配；别名桥接自己的行为在 test_structured_query_repository.py。
                EMPTY_ALIASES,
            ),
            people=EntityPersonDirectory(
                entities=entities, family=SqliteFamilyGraphRepository(migrations_ready_db, entities)
            ),
        ),
        # `query_photo_people` 的两条读路径（T122）。这个夹具不落脸也不落观察，
        # 所以它们恒空——被测的是别的工具的信息边界。它们自己的行为在
        # test_query_photo_people.py。
        faces=SqliteFaceRepository(migrations_ready_db),
        observations=observations,
        # `inspect_photo` 的两件（T123）。这个夹具不调它——被测的是别的工具的信息边界。
        photo_inspector=StubInspector(),
        person_crops=StubCrops(),
        # preview 的 ``place`` 取自 ``asset_places``（GPS 反解的行政地名）。
        asset_places=places,
    )
    return build, result_sets


def run(toolbox, name: str, **arguments: object):
    build, _result_sets = toolbox
    return build.execute(name, arguments, scope_id=SCOPE, now=NOW)


@pytest.mark.parametrize("subject", ["研究机构", "论文作者", "正文里的人名"])
@pytest.mark.parametrize(
    "evidence_type",
    [
        EvidenceType.SOURCE_TEXT,
        EvidenceType.VISIBLE_TEXT,
        EvidenceType.STRUCTURED_FACT,
        EvidenceType.SPOKEN_CONTENT,
    ],
)
def test_text_subject_does_not_require_a_named_face(toolbox, monkeypatch, subject, evidence_type):
    build, _ = toolbox
    requests = []
    original = build._retrieval.execute

    def retrieve(request):
        requests.append(request)
        return original(request)

    monkeypatch.setattr(build._retrieval, "execute", retrieve)
    outcome = build.execute(
        "search_memories",
        {"query": "海边"},
        scope_id=SCOPE,
        now=NOW,
        requirements=(EvidenceRequirement("text", evidence_type, "读文字", scope=(("person", subject),)),),
    )
    assert outcome.ok
    assert len(requests) == 1 and requests[0].linked_person_assets is None
    assert requests[0].text == "海边"


@pytest.mark.parametrize("source_type", ["email", "document"])
def test_text_source_search_keeps_subject_out_of_face_filters(toolbox, source_type, monkeypatch):
    build, _ = toolbox
    monkeypatch.setattr(build._retrieval, "_assets", build._assets)
    outcome = build.execute(
        "search_memories",
        {"query": "研究进展", "source_type": source_type},
        scope_id=SCOPE,
        now=NOW,
        requirements=(
            EvidenceRequirement("photo", EvidenceType.PHOTO_SET, "记录", scope=(("person", "某作者"),)),
        ),
    )
    assert outcome.ok


@pytest.mark.parametrize("with_text", [False, True])
@pytest.mark.parametrize(
    "text_type",
    [
        EvidenceType.SOURCE_TEXT,
        EvidenceType.VISIBLE_TEXT,
        EvidenceType.STRUCTURED_FACT,
        EvidenceType.SPOKEN_CONTENT,
    ],
)
def test_content_candidates_preserve_unconfirmed_photo_subject(toolbox, with_text, text_type):
    build, _ = toolbox
    requirements = [
        EvidenceRequirement("photo", EvidenceType.PHOTO_SET, "某人的照片", scope=(("person", "未确认人物"),))
    ]
    if with_text:
        requirements.append(EvidenceRequirement("text", text_type, "文字", scope=(("person", "研究机构"),)))
    outcome = build.execute(
        "search_memories",
        {"query": "海边", "requirement_id": "photo"},
        scope_id=SCOPE,
        now=NOW,
        requirements=requirements,
    )
    assert outcome.ok and not outcome.model_visible["answer_eligible"]
    assert outcome.model_visible["conditions_not_applied"][0]["value"] == "未确认人物"
    assert outcome.model_visible["conditions_not_applied"][0]["required"] is True
    assert "person" not in outcome.observation["filters_applied"]


def test_read_source_text_requires_a_text_handle_in_the_current_scope(toolbox):
    build, result_sets = toolbox
    result_sets.add(ResultSet("rs_source", SCOPE, "查询", ("asset_0",), NOW))
    result_sets.add(ResultSet("rs_other", "other-scope", "查询", ("asset_0",), NOW))

    assert (
        run(toolbox, "read_source_text", result_set_id="rs_source", asset_handle="photo_1").failure_reason
        == "unsupported_media_type"
    )
    assert (
        run(toolbox, "read_source_text", result_set_id="rs_source", asset_handle="photo_2").failure_reason
        == "bad_handle"
    )
    assert (
        run(toolbox, "read_source_text", result_set_id="rs_other", asset_handle="photo_1").failure_reason
        == "scope_mismatch"
    )
    assert (
        build.execute(
            "read_source_text",
            {"result_set_id": "rs_other", "asset_handle": "photo_1"},
            scope_id="other-scope",
            now=NOW,
        ).failure_reason
        == "asset_unavailable"
    )


def _seed_plain_assets(db, tmp_path, count: int) -> list[str]:
    """补一批只用来“凑满两页”的普通照片。preview 只读资产表，不需要向量。"""
    assets = SqliteAssetRepository(db)
    seeded: list[str] = []
    for offset in range(count):
        index = 100 + offset
        path = tmp_path / f"filler_{index}.jpg"
        path.write_bytes(f"filler {index}".encode())
        assets.add(
            Asset(
                id=f"asset_f{index}",
                scope_id=SCOPE,
                file_name=path.name,
                media_type=MediaType.IMAGE,
                path=str(path),
                mime_type="image/jpeg",
                size_bytes=path.stat().st_size,
                status=AssetStatus.PROCESSED,
                content_sha256=f"hashf{index}",
                captured_at=datetime(2019, 9, 1, tzinfo=UTC),
                captured_location=GpsPoint(30.0, 120.0),
                created_at=NOW,
                updated_at=NOW,
            )
        )
        seeded.append(f"asset_f{index}")
    return seeded


# ---- search_memories --------------------------------------------------------


def test_search_returns_a_result_set_with_a_preview(toolbox):
    """S2 没有相关性阈值，所以两行全在结果集里——顺序才是被测的东西。"""
    outcome = run(toolbox, "search_memories", query="海边")

    assert outcome.ok
    assert outcome.observation["total"] == 2
    assert outcome.observation["asset_ids"][0] == "asset_0"
    preview = outcome.model_visible["preview"]
    assert outcome.observation["preview"] == preview
    assert [item["handle"] for item in preview] == ["photo_1", "photo_2"]
    assert preview[0]["file_name"] == "海边.jpg"


def test_the_first_page_keeps_candidate_controls_internal(toolbox):
    """首批候选只给模型预览；候选总数与资产 ID 留给内部控制和取证。"""
    outcome = run(toolbox, "search_memories", query="海边")

    assert outcome.ok
    assert outcome.observation["query"] == "海边"
    assert outcome.observation["page"] == 1
    assert outcome.observation["total"] == len(outcome.observation["asset_ids"]) == 2
    assert outcome.observation["has_more"] is False
    assert outcome.model_visible["result_set_id"] == outcome.observation["result_set_id"]
    assert outcome.model_visible["preview"] == outcome.observation["preview"]
    for key in ("query", "page", "page_size", "total", "has_more", "can_inspect"):
        assert key not in outcome.model_visible
    assert "asset_ids" not in outcome.model_visible
    for asset_id in outcome.observation["asset_ids"]:
        assert asset_id not in json.dumps(outcome.model_visible)


def test_search_records_the_retrieval_backend_for_the_internal_projection(toolbox):
    outcome = run(toolbox, "search_memories", query="海边")

    assert outcome.observation["backend"] == "sqlite_exact"
    assert outcome.observation["degraded"] is True
    assert "degraded" not in outcome.model_visible


def test_a_blank_query_fails_without_touching_retrieval(toolbox):
    outcome = run(toolbox, "search_memories", query="   ")

    assert not outcome.ok
    assert outcome.failure_reason == "empty_query"
    assert outcome.model_visible["error"] == "empty_query"


def test_an_empty_scope_is_reported_as_zero(toolbox):
    """空库与「没搜到」是两件事。S2 没有相关性阈值，所以只会因为**一条向量都没有**
    而返回空——这正是门禁要认的那个事实（``fabrication_from_empty`` 的依据）。"""
    build, _result_sets = toolbox

    outcome = build.execute("search_memories", {"query": "海边"}, scope_id="空相册", now=NOW)

    assert outcome.observation["total"] == 0
    assert is_empty_result(outcome) is True
    assert outcome.model_visible["preview"] == []


# ---- get_result_page --------------------------------------------------------


def test_paging_requires_a_known_result_set(toolbox):
    outcome = run(toolbox, "get_result_page", result_set_id="rs_nope", page=1)

    assert not outcome.ok
    assert outcome.failure_reason == "unknown_result_set"


def test_paging_returns_the_rest_of_the_result_set(toolbox, migrations_ready_db, tmp_path):
    build, result_sets = toolbox
    filler = _seed_plain_assets(migrations_ready_db, tmp_path, 2 * PAGE_SIZE - 2)
    result_sets.add(
        ResultSet(
            id="rs_big",
            scope_id=SCOPE,
            query="海边",
            asset_ids=tuple(filler) + ("asset_0", "asset_1"),
            created_at=NOW,
        )
    )

    outcome = build.execute(
        "get_result_page", {"result_set_id": "rs_big", "page": 2}, scope_id=SCOPE, now=NOW
    )

    assert outcome.ok
    assert outcome.model_visible["page"] == 2
    assert outcome.model_visible["result_set_id"] == "rs_big"
    handles = [item["handle"] for item in outcome.model_visible["preview"]]
    # 两页候选：第 2 页从第 PAGE_SIZE+1 张开始，到结尾。
    assert handles == [f"photo_{i}" for i in range(PAGE_SIZE + 1, 2 * PAGE_SIZE + 1)]


def test_paging_a_single_page_set_is_refused(toolbox):
    """候选不超过 12 张时没有第二页可翻——直接拒绝，并说清“已经全部给过”。"""
    search = run(toolbox, "search_memories", query="海边")
    result_set_id = search.model_visible["result_set_id"]

    outcome = run(toolbox, "get_result_page", result_set_id=result_set_id, page=2)

    assert not outcome.ok
    assert outcome.failure_reason == "single_page_only"


def test_a_page_past_the_end_is_empty_not_an_error(toolbox, migrations_ready_db, tmp_path):
    build, result_sets = toolbox
    filler = _seed_plain_assets(migrations_ready_db, tmp_path, 2 * PAGE_SIZE - 2)
    result_sets.add(
        ResultSet(
            id="rs_big",
            scope_id=SCOPE,
            query="海边",
            asset_ids=tuple(filler) + ("asset_0", "asset_1"),
            created_at=NOW,
        )
    )

    outcome = build.execute(
        "get_result_page", {"result_set_id": "rs_big", "page": 99}, scope_id=SCOPE, now=NOW
    )

    assert outcome.ok
    assert outcome.model_visible["preview"] == []


@pytest.mark.parametrize("caption", ["海边两个人合影", "海边两个人合影" + "很长的画面描述" * 80])
def test_the_preview_carries_where_who_and_what(toolbox, migrations_ready_db, caption):
    """每条 preview 必须带上**地点、人物、画面描述**——回答「这张照片是什么」的全靠它们。

    1.0 的 ``_preview_entry`` 给了模型 14 个字段，其中这三个是核心；它的
    ``search_memories`` 描述里还专门写了一句「单张照片的地点、时间、人物直接在
    preview 里读，**不要为此再调用其他工具**」。

    2.0 的 ``_preview`` 是 S3 写的，那时这三样在 2.0 还不存在；S4 加了地点、S5 加了
    人物与语义，**没有一个切片回头把它加宽**。后果是模型无法从检索结果判断"这张是不是
    我要找的"，只能逐张 ``inspect_photo``——地点类题平均调 6.63 次工具而 1.0 是 1.83，
    错率 38.7% → 75.3%。

    反向断言（``people`` 为空、``place`` 为空的另一条）见本文件另一条用例：
    键必须**恒在**，空值是「这张图没记录到」，缺键是「字段没给我们」。
    """
    observations = SqliteObservationRepository(migrations_ready_db)
    observations.add(
        Observation(
            id="obs_1",
            scope_id=SCOPE,
            asset_id="asset_0",
            source_type="image",
            captured_at=datetime(2019, 8, 1, tzinfo=UTC),
            confidence=0.9,
            created_at=NOW,
            updated_at=NOW,
            caption=caption,
            # **``observations.place`` 是 VLM 写的语义场景**（提示词原文：「place 是
            # 语义地点，例如家中客厅、餐厅、公园」），不是行政地名。
            place="婚礼或庆典活动的室内背景区",
            people=("妈妈",),
        )
    )
    # **地点另有来源**：``asset_places``——导入期由 EXIF GPS 反解出来的行政地名。
    # preview 的 ``place`` 取这一份（1.0 的 ``_short_place_label`` 就是反解口径）。
    SqliteAssetPlaceRepository(migrations_ready_db).save(
        asset_id="asset_0",
        scope_id=SCOPE,
        gps=None,
        place=Place(
            admin=AdminArea(province="浙江省", city="杭州市", district="萧山区"),
            nearest=NearestPlace(),
            precision=PlacePrecision.DISTRICT,
            source=PlaceSource.PYGEOCN,
        ),
    )

    outcome = run(toolbox, "search_memories", query="海边")

    entry = outcome.model_visible["preview"][0]
    assert entry["place"] == "浙江省杭州市萧山区"
    # 摘要已含场景时不重复发送；摘要截断了场景时，独立字段必须保留。
    scene = "婚礼或庆典活动的室内背景区"
    summary = entry["evidence_summary"]
    if scene in summary:
        assert "scene_hint" not in entry
    else:
        assert entry["scene_hint"] == scene
    assert (summary + entry.get("scene_hint", "")).count(scene) == 1
    assert entry["people"] == ["妈妈"]
    assert "海边两个人合影" in entry["evidence_summary"]


def test_the_summary_is_composed_like_1_0(toolbox, migrations_ready_db):
    """摘要不是 caption 一句话，而是**按 1.0 的分段顺序拼出来的复合句**。

    1.0 ``_observation_summary``（``agent_runtime/tools.py:371``）的顺序是
    ``caption → activity → place → objects[:8] → clothing[:8] → 文字：…``。

    这条断言的是「那四样**同时**在」——只测 caption 的话，把 ``_observation_summary``
    换回 ``latest.caption`` 也照样绿，而那正是被修掉的那个 bug。
    """
    observations = SqliteObservationRepository(migrations_ready_db)
    observations.add(
        Observation(
            id="obs_rich",
            scope_id=SCOPE,
            asset_id="asset_0",
            source_type="image",
            captured_at=datetime(2019, 8, 1, tzinfo=UTC),
            confidence=0.9,
            created_at=NOW,
            updated_at=NOW,
            caption="婚礼现场，新郎新娘站在紫色地毯上",
            activity="婚礼仪式进行中",
            place="婚礼宴会厅",
            objects=("紫色地毯", "白色婚纱", "麦克风"),
            clothing=("婚纱", "西装"),
            ocr_text="双喜",
        )
    )

    outcome = run(toolbox, "search_memories", query="海边")

    summary = outcome.model_visible["preview"][0]["evidence_summary"]
    # 这四样原先一样都进不了 preview——它们正是模型会去调 inspect_photo /
    # read_photo_text 才拿得到的东西。
    assert "婚礼仪式进行中" in summary  # activity
    assert "紫色地毯、白色婚纱、麦克风" in summary  # objects
    assert "婚纱、西装" in summary  # clothing
    assert "文字：双喜" in summary  # ocr_text
    # 分段之间用「；」连，段内用「、」连——1.0 的两种分隔符不是随手选的。
    assert summary.startswith("婚礼现场，新郎新娘站在紫色地毯上；婚礼仪式进行中；")


def test_the_preview_carries_the_media_kind(toolbox, migrations_ready_db, tmp_path):
    """照片与视频都要能认出来：``media_kind`` 取资产自己的 ``media_type``。

    没有它，模型分不出「这是视频」还是「这是一张图」——而视频在 2.0 是**一个资产**，
    它的关键帧才是图。
    """
    video = tmp_path / "婚礼.mp4"
    video.write_bytes("视频字节".encode())
    SqliteAssetRepository(migrations_ready_db).add(
        Asset(
            id="asset_video",
            scope_id=SCOPE,
            file_name="婚礼.mp4",
            media_type=MediaType.VIDEO,
            path=str(video),
            mime_type="video/mp4",
            size_bytes=video.stat().st_size,
            status=AssetStatus.PROCESSED,
            content_sha256="hashvideo",
            captured_at=datetime(2019, 8, 3, tzinfo=UTC),
            captured_location=GpsPoint(30.0, 120.0),
            created_at=NOW,
            updated_at=NOW,
        )
    )

    build, result_sets = toolbox
    filler = _seed_plain_assets(migrations_ready_db, tmp_path, 2 * PAGE_SIZE - 2)
    result_sets.add(
        ResultSet(
            id="rs_kind",
            scope_id=SCOPE,
            query="婚礼",
            asset_ids=tuple(filler) + ("asset_0", "asset_video"),
            created_at=NOW,
        )
    )
    outcome = build.execute(
        "get_result_page", {"result_set_id": "rs_kind", "page": 2}, scope_id=SCOPE, now=NOW
    )

    kinds = [item["media_kind"] for item in outcome.model_visible["preview"]]
    assert kinds[-2:] == ["image", "video"]


def test_the_preview_marks_a_video_keyframe_as_such(toolbox, migrations_ready_db, tmp_path):
    """视频帧在 ``media_type`` 上就是普通图片，``source_kind`` 才能把它和真照片分开。

    2026-10-19 那轮 48 道视频题里有 9 道答「相册里没有视频，只有静态照片」——
    模型其实找到了帧，但 preview 只说 ``media_kind: image``，它无从知道那是帧。
    """
    video = tmp_path / "婚礼.mp4"
    video.write_bytes(b"video")
    SqliteAssetRepository(migrations_ready_db).add(
        Asset(
            id="asset_video",
            scope_id=SCOPE,
            file_name="婚礼.mp4",
            media_type=MediaType.VIDEO,
            path=str(video),
            mime_type="video/mp4",
            size_bytes=video.stat().st_size,
            status=AssetStatus.PROCESSED,
            content_sha256="hashvideo",
            captured_at=datetime(2019, 8, 3, tzinfo=UTC),
            captured_location=GpsPoint(30.0, 120.0),
            created_at=NOW,
            updated_at=NOW,
        )
    )
    frame = tmp_path / "kf_0001.jpg"
    frame.write_bytes(b"frame")
    # **必须走 ``add_derived``**：``SqliteAssetRepository.add`` 的 INSERT 列清单里
    # 没有血缘三列（那条路进来的从来不是派生物），用它造出来的帧会被读回成普通照片。
    SqliteVideoRepository(migrations_ready_db).add_derived(
        parent_asset_id="asset_video",
        asset=Asset(
            id="asset_frame",
            scope_id=SCOPE,
            file_name="kf_0001.jpg",
            media_type=MediaType.IMAGE,
            path=str(frame),
            mime_type="image/jpeg",
            size_bytes=frame.stat().st_size,
            status=AssetStatus.PROCESSED,
            content_sha256="hashframe",
            captured_at=datetime(2019, 8, 3, tzinfo=UTC),
            captured_location=GpsPoint(30.0, 120.0),
            created_at=NOW,
            updated_at=NOW,
        ),
        derived_kind="video_keyframe",
        source_timestamp_sec=3.021,
        source_frame_index=0,
        source_scene_index=0,
    )

    build, result_sets = toolbox
    filler = _seed_plain_assets(migrations_ready_db, tmp_path, 2 * PAGE_SIZE - 2)
    result_sets.add(
        ResultSet(
            id="rs_frame",
            scope_id=SCOPE,
            query="婚礼",
            asset_ids=tuple(filler) + ("asset_0", "asset_frame"),
            created_at=NOW,
        )
    )
    outcome = build.execute(
        "get_result_page", {"result_set_id": "rs_frame", "page": 2}, scope_id=SCOPE, now=NOW
    )

    kinds = [item["source_kind"] for item in outcome.model_visible["preview"]]
    assert kinds[-2:] == ["photo", "video_keyframe"]


def test_the_preview_carries_the_relevance_level(toolbox, migrations_ready_db, tmp_path):
    """``level`` 取**这条候选被哪一类通道召回**，不是名次。

    ``anchor``（metadata / entity）是确定性锚点，``semantic`` 是相似度命中。
    1.0 用 ``exact`` / ``strong`` / ``approximate`` 三个词表达它，但它的判据是
    「是不是事件成员」；2.0 的判据在 ``fusion.evidence_class_for``。
    """
    build, result_sets = toolbox
    filler = _seed_plain_assets(migrations_ready_db, tmp_path, 2 * PAGE_SIZE - 2)
    result_sets.add(
        ResultSet(
            id="rs_level",
            scope_id=SCOPE,
            query="海边",
            asset_ids=tuple(filler) + ("asset_0", "asset_1"),
            created_at=NOW,
            evidence_classes={"asset_0": "anchor", "asset_1": "semantic"},
        )
    )

    outcome = build.execute(
        "get_result_page", {"result_set_id": "rs_level", "page": 2}, scope_id=SCOPE, now=NOW
    )

    levels = [item["level"] for item in outcome.model_visible["preview"]]
    assert levels[-2:] == ["exact", "strong"]


def test_the_fused_evidence_class_survives_retrieval(toolbox):
    """融合层算出来的分级必须**一路带到结果集**，不能在 ``EvidenceHit`` 那里被丢掉。

    这是 ``level`` 的上游：``RetrieveEvidence`` 把 ``FusedCandidate`` 转成
    ``EvidenceHit`` 时，原先只搬了 ``asset_id`` / ``score`` / ``channels`` /
    ``raw_scores``——``evidence_class`` 就此消失。
    """
    build, result_sets = toolbox

    outcome = build.execute("search_memories", {"query": "海边"}, scope_id=SCOPE, now=NOW)

    stored = result_sets.get(outcome.model_visible["result_set_id"])
    assert stored is not None
    assert set(stored.evidence_classes) == set(stored.asset_ids)
    # 这个夹具的候选全部来自视觉通道（semantic），所以一条 anchor 都不该有。
    assert set(stored.evidence_classes.values()) == {"semantic"}


def test_the_preview_keys_are_always_present(toolbox):
    """没有观察行、也没有分级时这几个键**仍然在**，值取空。

    空串的意思与「字段没给我们」不是一回事：前者是「这张图确实没记录到地点」，
    后者会让模型以为这条链路坏了、转头去调 ``inspect_photo`` 试探。
    """
    outcome = run(toolbox, "search_memories", query="海边")

    entry = outcome.model_visible["preview"][0]
    assert entry["place"] == ""
    assert entry["people"] == []
    assert entry["evidence_summary"] == ""
    # 这两格的键也恒在：资产在、所以认得出是照片；检索给分级、所以这一格有值。
    # （「结果集压根没带分级」那一支在 test_assistant_repositories.py，
    #  那里读回来的是空串而不是 ``strong``。）
    assert entry["media_kind"] == "image"
    assert entry["level"] == "strong"


# ---- 人数与合影：数据驱动的重设计（替代 1.0 的 group_photo_*）------------------
#
# 1.0 那三个字段（``group_photo_count`` / ``sizes`` / ``rows``）只在 **query 或 goal 里
# 出现「合影」两个字**时才计算（``tools.py:1085`` 的 ``if re.search(r"合影", ...)``），
# 人数则靠正则从 ``caption`` / ``activity`` 的中文散文里抠「N名 / N人」。
#
# **判据换成数据。** 人数据本来就在库里（人脸绑定 + 观察快照），按它判既不用关键词、
# 也不依赖模型把「合影」写进描述里。字段名与形状照 1.0 保留。


def add_person(db, *, asset_id: str, name: str | None, people: int) -> None:
    """给一张照片落一份「谁在这上面」。``people`` = **这张照片上一共几个人**。

    ``name`` 给出时，其中一位是**已确认并绑定人脸**的（这正是 ``people_count`` 里
    「已确认」那一半的来源——它数的是**绑定的脸**，不是 ``people_json`` 里带 id 的条目）。

    **形状照真实写入路径**：VLM 先把 N 个原始人写成不带 ``entity_id`` 的条目，命名时
    ``_append_person_snapshot`` **再追加**一条带 id 的（``entities.py:1000``）。所以
    ``people_json`` 里「没有 id 的条目数」== N，而绑定的脸数是 1——``_people_count``
    的 ``described - len(identities)`` 减的就是这个重复。

    与 ``query_photo_people`` 的夹具同一套路：**人数据必须真落库**，否则测的是构造
    而不是算法。
    """
    faces = SqliteFaceRepository(db)
    observations = SqliteObservationRepository(db)
    observation_id = f"obs_{asset_id}"
    observations.add(
        Observation(
            id=observation_id,
            scope_id=SCOPE,
            asset_id=asset_id,
            source_type="image",
            captured_at=datetime(2019, 8, 1, tzinfo=UTC),
            confidence=0.9,
            created_at=NOW,
            updated_at=NOW,
            caption="婚礼合影",
            people=(name,) if name else (),
        )
    )
    # 原始条目**没有名字**：``_json_people`` 会跳过空名，所以 ``Observation.people``
    # （预览里那一格）留下的正是「已确认的那几位」——与 ``people_count`` 分工明确。
    snapshot: list[dict[str, object]] = [{"name": "", "entity_id": None} for _ in range(people)]
    if name:
        entity = SqliteEntityRepository(db).create_person(
            name, status=ENTITY_STATUS_CONFIRMED, scope_id=SCOPE
        )
        cluster = faces.create_cluster([0.0] * 4, confidence=0.9, scope_id=SCOPE)
        faces.link_entity(cluster.id, entity.id)
        faces.add_instance(
            asset_id=asset_id,
            observation_id=observation_id,
            cluster_id=cluster.id,
            bbox=(0.1, 0.2, 0.3, 0.4),
            embedding=[0.0] * 4,
            detection_confidence=0.95,
            quality=0.9,
            pose=[0.0] * 3,
            pose_bucket_name="front",
            embedding_model="buffalo_l",
            embedding_version="1",
            embedding_quality_signal=0.9,
            validity=FACE_VALIDITY_VERIFIED,
            area_ratio=0.05,
            sharpness=100.0,
        )
        # **命名会把这个人追加进快照**，所以原始那批未确认条目仍然在里面——这正是
        # ``_people_count`` 要减掉 ``len(identities)`` 的原因。
        snapshot.append({"name": name, "entity_id": entity.id})
    with db.connect() as connection, connection:
        connection.execute(
            "UPDATE observations SET people_json = ? WHERE id = ?",
            (json.dumps(snapshot, ensure_ascii=False), observation_id),
        )


def test_the_preview_carries_how_many_people_are_on_the_photo(toolbox, migrations_ready_db):
    """每条 preview 要能直接读出「这张上有几个人」。

    ``people`` 只有**已确认**的人名，所以只看它会把「四个人合影、认出来一个」读成
    「一个人」。``people_count`` 把未确认的同行者也算进去。
    """
    add_person(migrations_ready_db, asset_id="asset_0", name="妈妈", people=4)

    entry = run(toolbox, "search_memories", query="海边").model_visible["preview"][0]

    assert entry["people"] == ["妈妈"]
    assert entry["people_count"] == 4


def test_the_group_photo_rollup_is_not_keyword_gated(toolbox, migrations_ready_db):
    """**这一条就是这次重设计的全部理由。**

    1.0 只在 query 含「合影」时才算那三个字段。这里同一个检索**不带那个词**，聚合照样
    在——判据是数据（这张上有几个人），不是问题措辞。
    """
    add_person(migrations_ready_db, asset_id="asset_0", name="妈妈", people=3)
    add_person(migrations_ready_db, asset_id="asset_1", name="爸爸", people=1)

    outcome = run(toolbox, "search_memories", query="海边")
    body = outcome.model_visible

    assert outcome.ok
    assert outcome.observation["query"] == "海边"
    assert "query" not in body
    assert body["group_photo_count"] == 1
    assert body["group_photo_sizes"] == [3]
    assert body["group_photo_rows"] == [{"handle": "photo_1", "group_size": 3}]


def test_the_rollup_is_derived_from_the_per_photo_counts(toolbox, migrations_ready_db):
    """断言的是**关系**：聚合等于 preview 里那些人数的投影，不是抄一组常量。

    改了 ``GROUP_PHOTO_MIN_PEOPLE`` 或改了每张的人数，两边必须一起动——它们本来就是
    同一个数的两种读法。
    """
    add_person(migrations_ready_db, asset_id="asset_0", name="妈妈", people=2)
    add_person(migrations_ready_db, asset_id="asset_1", name="爸爸", people=4)

    body = run(toolbox, "search_memories", query="海边").model_visible

    counts = {item["handle"]: item["people_count"] for item in body["preview"]}
    assert counts == {"photo_1": 2, "photo_2": 4}
    assert body["group_photo_count"] == 2
    assert body["group_photo_sizes"] == [2, 4]
    assert {row["handle"] for row in body["group_photo_rows"]} == {"photo_1", "photo_2"}


def test_a_page_without_multi_person_photos_rolls_up_empty(toolbox, migrations_ready_db):
    """反向：一张多人照片都没有时是**空**，不是漏算。

    ``group_photo_count`` 与 ``group_photo_sizes`` 分开取 0 与 ``[]``——前者是
    「几张」，后者是「都是几人」。
    """
    add_person(migrations_ready_db, asset_id="asset_0", name="妈妈", people=1)

    body = run(toolbox, "search_memories", query="海边").model_visible

    assert body["group_photo_count"] == 0
    assert body["group_photo_sizes"] == []
    assert body["group_photo_rows"] == []
    # 但每张自己的计数照给：0 张合影不等于「没人」。
    assert body["preview"][0]["people_count"] == 1


@pytest.mark.parametrize("scope_id, expected_count", [(SCOPE, 2), ("空相册", 0)])
def test_the_first_preview_distinguishes_inspectable_candidates_from_empty_results(
    toolbox, scope_id, expected_count
):
    """首批预览直接表明有无可读候选，内部保留后续页判定。"""
    build, _result_sets = toolbox

    outcome = build.execute("search_memories", {"query": "海边"}, scope_id=scope_id, now=NOW)

    assert outcome.ok
    assert len(outcome.model_visible["preview"]) == expected_count
    assert is_empty_result(outcome) is (expected_count == 0)
    assert outcome.observation["total"] == expected_count
    assert outcome.observation["has_more"] is False
    assert "can_inspect" not in outcome.model_visible
    assert "has_more" not in outcome.model_visible


# ---- 交付：终止动作上的一个字段，不是一次工具调用 ---------------------------


def deliver(toolbox, result_set_id: str, handles: list[str]):
    build, _result_sets = toolbox
    return build.deliver(result_set_id, handles, scope_id=SCOPE)


def test_the_terminal_action_is_not_reachable_as_a_tool(toolbox):
    """``final_answer`` 不进 ``execute``——它是终止动作，不是一次工具调用。

    走工具那条路会让两件事同时发生：它受工具预算管辖（预算用完就拒收尾），并且它重新
    变成「作答之外的一步」——1.0 的实测正是后者：``get_original_photos`` 这个独立工具在
    生产上被调用 0 次，239 道图片题全部走 final 字段。
    """
    outcome = run(toolbox, "final_answer", answer="在海边拍的。", selected_image_handles=[])

    assert not outcome.ok
    assert outcome.failure_reason == "unknown_tool"


def test_delivery_turns_handles_into_openable_attachments(toolbox):
    search = run(toolbox, "search_memories", query="海边")
    result_set_id = search.model_visible["result_set_id"]

    outcome = deliver(toolbox, result_set_id, ["photo_1"])

    assert outcome.ok
    assert [item.asset_id for item in outcome.attachments] == ["asset_0"]
    assert outcome.attachments[0].file_name == "海边.jpg"
    assert outcome.model_visible["delivered"] == 1


def test_delivery_rejects_a_handle_outside_the_result_set(toolbox):
    """模型只能交付它真的看过的图——这是结构性事实，不是提示词约束。"""
    search = run(toolbox, "search_memories", query="海边")
    result_set_id = search.model_visible["result_set_id"]

    outcome = deliver(toolbox, result_set_id, ["photo_7"])

    assert not outcome.ok
    assert outcome.failure_reason == "bad_handle"
    assert outcome.attachments == ()


def test_delivery_needs_an_explicit_handle_list(toolbox):
    """不做回填：模型没选图就是没交付，**交付为空是合法状态**。

    空数组 ``[]`` 不是错误。``domain/assistant/tools.py`` 的 ``FINAL_ANSWER`` 注释写着
    「允许空数组是这条收紧能成立的前提」，``tool_policy`` 判「缺」的判据也是
    「键不在 / null / 空串」而不是 falsy。**2026-10-15 之前这里收 ``[]`` 会回
    ``missing_handle``**——同一件事在策略层合法、在交付层是错误码，于是「模型明确
    回答这轮不交图」在轨迹里被记成一次失败。
    """
    search = run(toolbox, "search_memories", query="海边")
    result_set_id = search.model_visible["result_set_id"]

    outcome = deliver(toolbox, result_set_id, [])

    assert outcome.ok
    assert outcome.attachments == ()
    assert outcome.model_visible["delivered"] == 0


def test_delivery_fails_only_when_no_handle_resolves(toolbox):
    """**给了非空 handles、一条都解不出来**才是错误——空数组不是。"""
    search = run(toolbox, "search_memories", query="海边")
    result_set_id = search.model_visible["result_set_id"]

    outcome = deliver(toolbox, result_set_id, ["photo_99", "nope"])

    assert not outcome.ok
    assert outcome.failure_reason == "bad_handle"


def test_delivery_drops_repeats_and_caps_at_the_limit(toolbox):
    """去重与上限照 1.0 ``_normalize_selected_image_handles``（``runtime.py:239-251``）。

    2.0 此前两者都没有：上限只在 JSON Schema 的 ``maxItems`` 里声明（那不保证端点会守），
    重复的 handle 会产出重复附件。
    """
    search = run(toolbox, "search_memories", query="海边")
    result_set_id = search.model_visible["result_set_id"]

    # 重复同一个 handle，再要一轮超出上限的量。
    outcome = deliver(toolbox, result_set_id, ["photo_1", "photo_1", "photo_2", "photo_2"])

    assert outcome.ok
    handles = [item.handle for item in outcome.attachments]
    assert handles == ["photo_1", "photo_2"]
    assert len(handles) == len(set(handles))


def test_delivery_requires_a_known_result_set(toolbox):
    outcome = deliver(toolbox, "rs_nope", ["photo_1"])

    assert not outcome.ok
    assert outcome.failure_reason == "unknown_result_set"


# ---- get_person_profile -----------------------------------------------------


def test_a_known_person_returns_membership_and_relationships(toolbox):
    """S7c 之前这里只回 ``{"person", "family_role"}``——``family_role`` 是 S3 的占位。"""
    outcome = run(toolbox, "get_person_profile", person="妈妈")

    assert outcome.ok
    assert outcome.model_visible == {
        "person": "妈妈",
        "aliases": [],
        "membership": "family",
        "membership_source": "user_override",
        "relationships": [{"predicate": "母亲", "other_name": "小明", "source": "model"}],
    }
    assert outcome.blocks is None
    # 「有几条关系」是**内部**投影里的量，不给模型看——给了它会拿数量当答案。
    assert outcome.observation["relationship_count"] == 1
    assert "relationship_count" not in outcome.model_visible


def test_a_person_with_no_relationships_is_still_found(toolbox):
    """**没有关系 ≠ 查不到这个人。** 实测那份相册就是 8 个人 0 条关系，如果这里回
    ``found: False``，助手会对着一个明明存在的人说「查不到」。

    定点变异：把 ``relationships`` 为空当成查不到（走 gap 分支），这条红。
    """
    outcome = run(toolbox, "get_person_profile", person="成员一")

    assert outcome.ok
    assert "found" not in outcome.model_visible
    assert outcome.model_visible["person"] == "成员一"
    assert outcome.model_visible["relationships"] == []
    assert outcome.model_visible["membership"] == "unknown"
    assert outcome.blocks is None


def test_the_person_tool_description_no_longer_denies_relationships(toolbox):
    """工具描述是**给模型看的说明书**，写错就是让模型去问拿不到的东西、或者
    明明有却说没有。S7c 之前那句「人物关系…尚未建立」现在是假的。

    定点变异：把描述改回旧的，这条红。
    """
    from sentrix.domain.assistant.tools import TOOLS_BY_NAME

    description = TOOLS_BY_NAME["get_person_profile"].description

    assert "尚未建立" not in description.split("常去地点")[0]
    assert "relationships" in description
    assert "家庭归属" in description


def test_an_unknown_person_returns_a_gap_and_blocks_the_requirement(toolbox):
    """S3 还没有身份层（S5 落地）。查不到就如实封口，而不是让门禁一遍遍逼模型去查。"""
    outcome = run(toolbox, "get_person_profile", person="隔壁老王")

    assert outcome.model_visible["found"] is False
    assert outcome.blocks is EvidenceType.PERSON_PROFILE
    assert "不要编造" in outcome.model_visible["note"]


def test_a_blank_person_name_fails(toolbox):
    outcome = run(toolbox, "get_person_profile", person="")

    assert not outcome.ok
    assert outcome.failure_reason == "empty_person"


# ---- 未知工具 ---------------------------------------------------------------


def test_an_unknown_tool_is_reported_not_raised(toolbox):
    """**名字要挑一个永远不会存在的。** 这条用例原来借的是 ``inspect_photo``——
    那时它确实不存在（S6 的 T123 才做）。它落地之后，同一次调用变成了
    ``unknown_result_set``（参数齐了、只是结果集对不上），用例当场红。
    """
    outcome = run(toolbox, "no_such_tool", asset_handle="photo_1")

    assert not outcome.ok
    assert outcome.failure_reason == "unknown_tool"


# ---- query_memory_facts -----------------------------------------------------


def test_memory_facts_counts_the_album(toolbox):
    """统计工具走的是 SQL，不是检索——所以它数出来的就是资产数本身。

    夹具里有 2 张照片（``asset_0`` / ``asset_1``），搜索那条用例也断言了同一个数。
    """
    outcome = run(toolbox, "query_memory_facts", operation="count", subject="asset")

    assert outcome.ok
    assert outcome.model_visible["value"] == 2
    assert outcome.model_visible["fact_type"] == "asset_count"
    assert outcome.model_visible["unit"] == "original_asset"
    assert outcome.model_visible["coverage"]["complete"] is True


def test_memory_facts_never_leaks_an_asset_id_to_the_model(toolbox):
    """``samples`` 里只有 ``captured_at`` / ``media_type`` / ``caption``。

    1.0 ``_fact_samples`` 的键白名单，也是这个工具与模型之间唯一的边界。
    """
    outcome = run(toolbox, "query_memory_facts", operation="first", subject="asset")

    assert outcome.ok
    samples = outcome.model_visible["samples"]
    assert samples
    for sample in samples:
        assert set(sample) == {"captured_at", "media_type", "caption"}
    assert "asset_id" not in str(outcome.model_visible)


def test_memory_facts_reports_a_rejection_as_a_normal_answer(toolbox):
    """**被拒是一次正常返回，不是异常。** 模型看到 ``reason`` 才知道下一步怎么改参数。"""
    outcome = run(toolbox, "query_memory_facts", operation="count", subject="妈妈")

    assert not outcome.ok
    assert outcome.failure_reason == "unsupported_subject"
    assert outcome.model_visible["status"] == "unsupported_subject"


def test_memory_facts_groups_by_month(toolbox):
    """两张照片分别在 2019-08-01 与 2019-08-02，所以是同一个分组、计数为 2。"""
    outcome = run(toolbox, "query_memory_facts", operation="group", subject="asset", group_by="month")

    assert outcome.ok
    assert outcome.model_visible["value"] == [{"group": "2019-08", "count": 2}]
    assert outcome.model_visible["unit"] == "original_asset_breakdown"
    # 分组结果本身已经把内容摊开了，不再附样本——1.0 也是只在非分组时附。
    assert outcome.model_visible["samples"] == []


def test_memory_facts_exists_and_last(toolbox):
    """夹具里两张照片是 2019-08-01 与 2019-08-02。"""
    exists = run(toolbox, "query_memory_facts", operation="exists", subject="asset")
    assert exists.ok
    assert exists.model_visible["value"] is True

    last = run(toolbox, "query_memory_facts", operation="last", subject="asset")
    assert last.ok
    assert last.model_visible["value"] == "2019-08-02T00:00:00+00:00"
    assert last.model_visible["unit"] == "captured_at"


def test_memory_facts_unit_follows_the_media_filter(toolbox):
    """``unit`` 是按媒体过滤定的，不是按结果定的——问「多少张视频」得到 0，
    但单位仍然是 ``original_video``。写成「结果为空就用默认单位」会让
    「0 个视频」和「0 张照片」看起来是同一句话。"""
    image = run(toolbox, "query_memory_facts", operation="count", subject="asset", filters={"media": "image"})
    assert (image.model_visible["unit"], image.model_visible["value"]) == ("original_image", 2)

    video = run(toolbox, "query_memory_facts", operation="count", subject="asset", filters={"media": "video"})
    assert (video.model_visible["unit"], video.model_visible["value"]) == ("original_video", 0)


def test_memory_facts_without_a_scope_is_unavailable(toolbox):
    """相册范围由系统注入。走到这里说明**装配错了，不是模型错了**——
    所以是 ``unavailable`` 而不是某个 ``unsupported_*``：那两类要模型改参数，
    这一类改参数没用。"""
    build, _result_sets = toolbox

    outcome = build.execute(
        "query_memory_facts", {"operation": "count", "subject": "asset"}, scope_id="", now=NOW
    )

    assert outcome.failure_reason == "missing_runtime_scope"


def test_memory_facts_rejects_a_person_that_was_never_confirmed(toolbox):
    """**与「这个人不存在」不同，所以不是一个 0 条的统计结果。** 返回 0 会让模型
    理直气壮地说「没有他/她的照片」，而事实是这个名字在本相册里根本没被确认过。"""
    outcome = run(
        toolbox,
        "query_memory_facts",
        operation="count",
        subject="person_appearance",
        filters={"person": "查无此人"},
    )

    assert outcome.failure_reason == "person_not_named_in_current_album"
    assert outcome.model_visible["status"] == "unresolved_person"


def test_batch_bound_photo_delivery_and_ocr_do_not_follow_the_latest_search(toolbox, migrations_ready_db):
    build, sets = toolbox
    observations = SqliteObservationRepository(migrations_ready_db)
    for i in range(2):
        observations.add(
            Observation(
                id=f"ocr-{i}",
                scope_id=SCOPE,
                asset_id=f"asset_{i}",
                source_type="image",
                captured_at=NOW,
                confidence=1.0,
                created_at=NOW,
                updated_at=NOW,
                ocr_text=f"菜单{i}",
            )
        )
    sets.add(ResultSet("rs_table", SCOPE, "餐桌", ("asset_1", "asset_0"), NOW))
    sets.add(ResultSet("rs_landscape", SCOPE, "风景", ("asset_0", "asset_1"), NOW))
    # photo_2 deliberately names different assets in the two batches.
    first = run(toolbox, "read_photo_text", result_set_id="rs_table", asset_handle="photo_2")
    second = run(toolbox, "read_photo_text", result_set_id="rs_landscape", asset_handle="photo_2")
    assert first.observation["asset_id"] == "asset_0"
    assert second.observation["asset_id"] == "asset_1"
    outcome = build.deliver(
        "rs_landscape",
        [
            {"result_set_id": "rs_table", "asset_handle": "photo_2"},
            {"result_set_id": "rs_landscape", "asset_handle": "photo_2"},
        ],
        scope_id=SCOPE,
        fallback_result_set_ids=("rs_table",),
    )
    assert outcome.ok
    assert [item.asset_id for item in outcome.attachments] == ["asset_0", "asset_1"]
    assert [row["result_set_id"] for row in outcome.observation["references"]] == ["rs_table", "rs_landscape"]
    legacy = build.deliver("rs_landscape", ["photo_2"], scope_id=SCOPE, fallback_result_set_ids=("rs_table",))
    assert [item.asset_id for item in legacy.attachments] == ["asset_1"]


def test_delivery_rejects_another_turn_or_scope_batch(toolbox):
    build, sets = toolbox
    sets.add(ResultSet("rs_current", SCOPE, "餐桌", ("asset_0",), NOW))
    sets.add(ResultSet("rs_previous_turn", SCOPE, "餐桌", ("asset_0",), NOW))
    sets.add(ResultSet("rs_other_scope", "other", "餐桌", ("asset_0",), NOW))
    for batch, allowed, reason in [
        ("rs_previous_turn", (), "unknown_result_set"),
        ("rs_missing", ("rs_missing",), "unknown_result_set"),
        ("rs_other_scope", ("rs_other_scope",), "scope_mismatch"),
    ]:
        result = build.deliver(
            "rs_current",
            [{"result_set_id": batch, "asset_handle": "photo_1"}],
            scope_id=SCOPE,
            fallback_result_set_ids=allowed,
        )
        assert result.failure_reason == reason
        assert result.attachments == ()


def test_first_page_keeps_the_batch_identity(toolbox):
    result = run(toolbox, "search_memories", query="海边")
    assert result.model_visible["result_set_id"] == result.observation["result_set_id"]


@pytest.mark.parametrize(
    "time,expected",
    [
        ("2019年8月1日", {"asset_0"}),
        ("截至2019-08-01", {"asset_0"}),
        ("截至2019-07-31", set()),
    ],
)
def test_activity_time_is_applied_without_a_face_scope(toolbox, time, expected):
    build, _ = toolbox
    for n in range(2):
        build._observations.add(
            Observation(
                id=f"obs_{n}",
                scope_id=SCOPE,
                asset_id=f"asset_{n}",
                source_type="import",
                captured_at=None,
                confidence=0.8,
                created_at=NOW,
                updated_at=NOW,
            )
        )
    build._retrieval._conditions = EvaluateConditions(assets=build._assets, observations=build._observations)
    outcome = build.execute(
        "search_memories",
        {"query": "海边"},
        scope_id=SCOPE,
        now=NOW,
        requirements=(
            EvidenceRequirement("photo", EvidenceType.PHOTO_SET, "活动照片", scope=(("time", time),)),
        ),
    )
    assert outcome.ok
    assert set(outcome.observation["asset_ids"]) == expected
    assert outcome.observation["filters_applied"] == {"time": time}


def test_distinct_requirement_times_do_not_become_a_single_filter(toolbox):
    build, _ = toolbox
    outcome = build.execute(
        "search_memories",
        {"query": "海边"},
        scope_id=SCOPE,
        now=NOW,
        requirements=tuple(
            EvidenceRequirement(
                f"p{day}", EvidenceType.PHOTO_SET, "比较两日", scope=(("time", f"2019年8月{day}日"),)
            )
            for day in (1, 2)
        ),
    )
    assert not outcome.ok
    assert outcome.failure_reason == "requirement_required"
    selected = build.execute(
        "search_memories",
        {"query": "海边", "requirement_id": "p1"},
        scope_id=SCOPE,
        now=NOW,
        requirements=(
            EvidenceRequirement("p1", EvidenceType.PHOTO_SET, "第一日", scope=(("time", "2019年8月1日"),)),
        ),
    )
    assert selected.ok
    assert selected.observation["filters_applied"]["time"] == "2019年8月1日"


def test_fact_cutoff_includes_earlier_days_and_excludes_next_day(toolbox):
    count = run(
        toolbox, "query_memory_facts", operation="count", subject="asset", filters={"time": "截至2019-08-01"}
    )
    assert count.model_visible["value"] == 1
    assert count.observation["filters_not_applied"] == []


def test_explicit_question_reference_date_bounds_search_even_when_planner_omits_time(toolbox):
    build, _ = toolbox
    for n in range(2):
        build._observations.add(
            Observation(
                id=f"obs_{n}",
                scope_id=SCOPE,
                asset_id=f"asset_{n}",
                source_type="import",
                captured_at=None,
                confidence=0.8,
                created_at=NOW,
                updated_at=NOW,
            )
        )
    build._retrieval._conditions = EvaluateConditions(assets=build._assets, observations=build._observations)
    result = build.execute(
        "search_memories",
        {"query": "海边"},
        scope_id=SCOPE,
        now=NOW,
        question="Today is Aug 1st, 2019. When did I most recently visit the beach?",
    )
    assert result.ok
    assert result.observation["asset_ids"] == ["asset_0"]
    assert result.observation["filters_applied"] == {"reference_time": "截至2019-08-01"}


@pytest.mark.parametrize("time,expected", [(None, 1), ("2019年7月", 0), ("2019年8月2日", 0)])
def test_fact_question_reference_date_intersects_model_time_filters(toolbox, time, expected):
    build, _ = toolbox
    result = build.execute(
        "query_memory_facts",
        {"operation": "count", "subject": "asset", "filters": {"time": time} if time else {}},
        scope_id=SCOPE,
        now=NOW,
        question="今天是2019年8月1日，最近一次去海边是哪天？",
    )
    assert result.model_visible["value"] == expected
    assert "reference_time" in result.observation["filters_applied"]


# 查询条件使用独立条件契约，样例只依赖夹具中的两天媒体。
def test_bound_capture_range_is_shared_by_search_and_statistics(toolbox):
    from sentrix.domain.retrieval.query_conditions import QueryCondition

    build, _ = toolbox
    req = EvidenceRequirement(
        "day",
        EvidenceType.PHOTO_SET,
        "第一天",
        conditions=(QueryCondition("time", "2019年8月1日至8月1日", "capture"),),
    )
    results = [
        build.execute(name, args, scope_id=SCOPE, now=NOW, requirements=(req,))
        for name, args in (
            ("search_memories", {"query": "海边", "requirement_id": "day"}),
            ("query_memory_facts", {"operation": "count", "subject": "asset", "requirement_id": "day"}),
        )
    ]
    assert all(result.ok for result in results)
    assert results[0].observation["filters_applied"]["time"] == "2019年8月1日至8月1日"
    assert results[1].observation["total"] == 1
    assert results[1].observation["filters_applied"]["time"] == ("2019-08-01T00:00:00", "2019-08-02T00:00:00")


def test_unparsed_time_rejects_statistics_before_any_sql(toolbox, monkeypatch):
    build, _ = toolbox
    monkeypatch.setattr(
        build._memory_facts, "_dispatch", lambda *args: pytest.fail("unresolved time reached SQL")
    )
    outcome = build.execute(
        "query_memory_facts",
        {"operation": "count", "subject": "asset", "filters": {"time": "某次旅行的第三天"}},
        scope_id=SCOPE,
        now=NOW,
    )
    assert not outcome.ok
    assert outcome.model_visible["answer_eligible"] is False
    direct = build._memory_facts.execute(
        {"operation": "count", "subject": "asset", "filters": {"time": "2022年3月某个周末"}},
        scope_id=SCOPE,
        viewer_id="owner",
        conversation_id="test",
        now=NOW,
    )
    assert direct.reason == "unparsed_time"


def test_event_range_update_requires_evidence_and_is_reused_for_the_turn(toolbox):
    from sentrix.domain.retrieval.query_conditions import QueryCondition

    build, _ = toolbox
    condition = QueryCondition("time", "第三天", "capture", anchor="trip", state="pending")
    req = EvidenceRequirement("trip_day", EvidenceType.PHOTO_SET, "行程第三天", conditions=(condition,))
    state = {}
    original = {"query": "海边", "requirement_id": "trip_day"}
    initial = build.execute(
        "search_memories", original, scope_id=SCOPE, now=NOW, requirements=(req,), query_state=state
    )
    assert initial.ok and not initial.model_visible["answer_eligible"]
    exploration = build.execute(
        "search_memories",
        {**original, "exploration": True},
        scope_id=SCOPE,
        now=NOW,
        requirements=(req,),
        query_state=state,
    )
    assert exploration.ok and not exploration.model_visible["answer_eligible"]
    update = QueryCondition("time", "2019-08-01", "capture", anchor="trip", required=False).payload()
    args = {
        "operation": "count",
        "subject": "asset",
        "requirement_id": "trip_day",
        "condition_updates": [update],
    }
    missing = build.execute(
        "query_memory_facts", args, scope_id=SCOPE, now=NOW, requirements=(req,), query_state=state
    )
    assert missing.failure_reason == "condition_evidence_required"
    args["condition_evidence"] = {
        "result_set_id": exploration.observation["result_set_id"],
        "asset_handle": "photo_1",
    }
    evidence, records = _read_condition_evidence(build, exploration)
    args["condition_evidence"] = evidence
    answered = build.execute(
        "query_memory_facts",
        args,
        scope_id=SCOPE,
        now=NOW,
        requirements=(req,),
        query_state=state,
        records=records,
        condition_reviewer=lambda *args: ClaimReview(ClaimVerdict.SUPPORTED),
    )
    assert answered.ok and answered.observation["total"] == 1
    assert answered.model_visible["conditions_applied"][0]["required"] is True
    reused = build.execute(
        "query_memory_facts",
        {"operation": "count", "subject": "asset", "requirement_id": "trip_day"},
        scope_id=SCOPE,
        now=NOW,
        requirements=(req,),
        query_state=state,
    )
    assert reused.ok and reused.observation["total"] == 1
    assert reused.model_visible["resolved_anchors"] == answered.model_visible["resolved_anchors"]


def _read_condition_evidence(build, result, text="行程从2019-07-30开始，第三天2019-08-01去海边。"):
    set_id = result.observation["result_set_id"]
    asset_id = result.observation["asset_ids"][0]
    build._observations.add(
        Observation(
            id="condition-ocr",
            scope_id=SCOPE,
            asset_id=asset_id,
            source_type="import",
            captured_at=None,
            confidence=1.0,
            ocr_text=text,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    reading = build.execute(
        "read_photo_text",
        {"result_set_id": set_id, "asset_handle": "photo_1"},
        scope_id=SCOPE,
        now=NOW,
    )
    assert reading.ok
    evidence = {"result_set_id": set_id, "asset_handle": "photo_1", "call_id": "read-1", "quote": text}
    record = ToolCallRecord(
        "read-1",
        "read_photo_text",
        "sig-read",
        "ok",
        observation=reading.observation,
        model_visible=reading.model_visible,
    )
    return evidence, (record,)


@pytest.mark.parametrize("planner_state", ["resolved", "pending"])
def test_event_confirmation_is_bound_to_the_read_asset_and_never_widens_statistics(toolbox, planner_state):
    build, _ = toolbox
    condition = QueryCondition("event", "海边旅行", "anchor", state=planner_state)
    req = EvidenceRequirement("trip", EvidenceType.PHOTO_SET, "海边旅行照片", conditions=(condition,))
    state = {}
    args = {"query": "海边", "requirement_id": "trip"}
    common = dict(scope_id=SCOPE, now=NOW, requirements=(req,), query_state=state)
    initial = build.execute("search_memories", args, **common)
    assert initial.ok and not initial.model_visible["answer_eligible"]
    evidence, records = _read_condition_evidence(build, initial, "海边旅行的纪念照片。")
    checked = []

    def review(updates, facts):
        checked.append((updates, facts))
        return ClaimReview(ClaimVerdict.SUPPORTED, "原文明确对应这张海边旅行照片。")

    updated = build.execute(
        "search_memories",
        {
            **args,
            "condition_updates": [
                QueryCondition("event", condition.value, "anchor", required=False).payload()
            ],
            "condition_evidence": evidence,
        },
        records=records,
        condition_reviewer=review,
        **common,
    )
    assert updated.ok and updated.model_visible["answer_eligible"]
    assert updated.observation["asset_ids"] == [records[0].observation["asset_id"]]
    assert checked[0][0][0][0] == condition
    assert checked[0][0][0][1].required is True
    assert state["trip"]["conditions"] == (condition,)
    reused = build.execute("search_memories", args, **common)
    assert reused.observation["asset_ids"] == updated.observation["asset_ids"]
    assert reused.model_visible["answer_eligible"]
    count = build.execute(
        "query_memory_facts", {"operation": "count", "subject": "asset", "requirement_id": "trip"}, **common
    )
    assert count.failure_reason == "unresolved_conditions"
    assert not count.model_visible["answer_eligible"]


@pytest.mark.parametrize(
    "defect",
    ["unread", "wrong_asset", "quote", "uncertain", "wrong_scope", "role", "anchor", "unrelated", "fault"],
)
def test_condition_evidence_errors_keep_original_conditions_and_return_specific_feedback(toolbox, defect):
    import dataclasses

    build, _ = toolbox
    condition = QueryCondition("event", "购买海边明信片", "anchor")
    req = EvidenceRequirement("buy", EvidenceType.VISIBLE_TEXT, "购买明信片", conditions=(condition,))
    state = {}
    common = dict(scope_id=SCOPE, now=NOW, requirements=(req,), query_state=state)
    args = {"query": "海边", "requirement_id": "buy"}
    initial = build.execute("search_memories", args, **common)
    evidence, records = _read_condition_evidence(build, initial, "海边明信片售价£5。")
    update = condition.payload()
    expected = "condition_evidence_not_read"
    if defect == "unread":
        records = ()
    elif defect == "wrong_asset":
        evidence["asset_handle"] = "photo_2"
    elif defect == "quote":
        evidence["quote"] = "用户已购买£5明信片"
    elif defect == "uncertain":
        records = (
            dataclasses.replace(
                records[0], model_visible={**records[0].model_visible, "certainty": "uncertain"}
            ),
        )
    elif defect == "wrong_scope":
        result_set = build._result_sets.get(evidence["result_set_id"])
        build._result_sets.add(dataclasses.replace(result_set, scope_id="another"))
        expected = "invalid_condition_evidence"
    elif defect == "role":
        update["role"] = "venue"
        expected = "invalid_conditions"
    elif defect == "anchor":
        update["anchor"] = evidence["result_set_id"] + "/photo_1"
        expected = "invalid_condition_update"
    else:
        expected = "condition_evidence_unconfirmed"
    calls = []

    def review(*values):
        calls.append(values)
        return ClaimReview(
            ClaimVerdict.AMBIGUOUS,
            "只有售价，无购买者或交易依据。" if defect == "unrelated" else "核验服务不可用。",
        )

    result = build.execute(
        "search_memories",
        {**args, "condition_updates": [update], "condition_evidence": evidence},
        records=records,
        condition_reviewer=review,
        **common,
    )
    assert result.ok and not result.model_visible["answer_eligible"]
    error = result.model_visible["condition_update_error"]
    assert error["error"] == expected
    assert "buy" not in state
    if defect == "role":
        assert "anchor" in error["message"] and "venue" in error["message"]
    if defect == "anchor":
        assert "待补条件" in error["message"] and "anchor" in error["message"]
    assert len(calls) == (1 if defect in {"unrelated", "fault"} else 0)


def test_condition_batch_is_atomic_before_semantic_review(toolbox):
    build, _ = toolbox
    condition = QueryCondition("event", "海边旅行", "anchor")
    req = EvidenceRequirement("trip", EvidenceType.PHOTO_SET, "旅行", conditions=(condition,))
    state = {}
    common = dict(scope_id=SCOPE, now=NOW, requirements=(req,), query_state=state)
    initial = build.execute("search_memories", {"query": "海边", "requirement_id": "trip"}, **common)
    evidence, records = _read_condition_evidence(build, initial)
    result = build.execute(
        "search_memories",
        {
            "query": "海边",
            "requirement_id": "trip",
            "condition_evidence": evidence,
            "condition_updates": [condition.payload(), QueryCondition("place", "Porto", "capture").payload()],
        },
        records=records,
        condition_reviewer=lambda *args: pytest.fail("invalid batch reached model"),
        **common,
    )
    assert not result.model_visible["answer_eligible"] and "trip" not in state


def test_hotel_mentioned_location_is_not_applied_to_capture_gps(toolbox):
    from sentrix.domain.retrieval.query_conditions import QueryCondition

    build, _ = toolbox
    req = EvidenceRequirement(
        "hotel",
        EvidenceType.VISIBLE_TEXT,
        "酒店截图",
        conditions=(QueryCondition("place", "Marseille", "mentioned"),),
    )
    result = build.execute(
        "search_memories",
        {"query": "海边", "exploration": True},
        scope_id=SCOPE,
        now=NOW,
        requirements=(req,),
    )
    assert result.ok and "place" not in result.observation["filters_applied"]
    assert result.model_visible["conditions_not_applied"][0]["role"] == "mentioned"


def test_mixed_typed_and_legacy_scopes_require_a_selected_requirement(toolbox):
    from sentrix.domain.retrieval.query_conditions import QueryCondition

    build, _ = toolbox
    typed = EvidenceRequirement(
        "typed",
        EvidenceType.PHOTO_SET,
        "第一天",
        conditions=(QueryCondition("time", "2019-08-01", "capture"),),
    )
    legacy = EvidenceRequirement(
        "legacy",
        EvidenceType.PHOTO_SET,
        "另一天",
        scope=(("time", "2019-08-02"),),
    )
    result = build.execute(
        "query_memory_facts",
        {"operation": "count", "subject": "asset"},
        scope_id=SCOPE,
        now=NOW,
        requirements=(typed, legacy),
    )
    assert result.failure_reason == "requirement_required"
    result = build.execute(
        "query_memory_facts",
        {"operation": "count", "subject": "asset", "requirement_id": "legacy"},
        scope_id=SCOPE,
        now=NOW,
        requirements=(typed, legacy),
    )
    assert result.ok
    assert result.observation["filters_applied"]["time"] == ("2019-08-02T00:00:00", "2019-08-03T00:00:00")


def test_failed_query_does_not_report_resolved_conditions_as_executed(toolbox):
    build, _ = toolbox
    result = build.execute(
        "query_memory_facts",
        {"operation": "invalid", "subject": "asset", "filters": {"time": "2019-08-01"}},
        scope_id=SCOPE,
        now=NOW,
    )
    assert not result.ok
    assert result.model_visible["conditions_applied"] == []
    assert result.model_visible["conditions_resolved"]
    assert not result.model_visible["answer_eligible"]


def test_exploratory_result_pages_keep_the_unresolved_conditions(toolbox, migrations_ready_db, tmp_path):
    from sentrix.domain.retrieval.query_conditions import QueryCondition

    build, result_sets = toolbox
    state = {}
    req = EvidenceRequirement(
        "trip",
        EvidenceType.PHOTO_SET,
        "旅行第三天",
        conditions=(QueryCondition("time", "第三天", "capture", state="pending", anchor="trip"),),
    )
    result = build.execute(
        "search_memories",
        {"query": "海边", "requirement_id": "trip", "exploration": True},
        scope_id=SCOPE,
        now=NOW,
        requirements=(req,),
        query_state=state,
    )
    set_id = "exploratory_multi_page"
    filler = _seed_plain_assets(migrations_ready_db, tmp_path, 2 * PAGE_SIZE - 2)
    result_sets.add(
        ResultSet(
            id=set_id,
            scope_id=SCOPE,
            query="海边",
            asset_ids=tuple(filler) + ("asset_0", "asset_1"),
            created_at=NOW,
        )
    )
    state["result_conditions"][set_id] = state["result_conditions"][result.observation["result_set_id"]]
    page = build.execute(
        "get_result_page",
        {"result_set_id": set_id, "page": 2},
        scope_id=SCOPE,
        now=NOW,
        requirements=(req,),
        query_state=state,
    )
    assert page.ok
    assert not page.model_visible["answer_eligible"]
    assert page.model_visible["conditions_not_applied"] == result.model_visible["conditions_not_applied"]


def test_oversized_group_results_do_not_expose_pages_or_satisfy_the_answer(toolbox, monkeypatch):
    from sentrix.domain.retrieval.structured_query import GroupCount

    build, _ = toolbox
    monkeypatch.setattr(
        build._memory_facts._structured,
        "group_counts",
        lambda *args: [GroupCount(group=str(n), count=1) for n in range(1660)],
    )
    outcome = build.execute(
        "query_memory_facts",
        {"operation": "group", "subject": "asset", "group_by": "place"},
        scope_id=SCOPE,
        now=NOW,
    )
    assert not outcome.ok and not outcome.model_visible["answer_eligible"]
    assert outcome.model_visible["status"] == "query_too_broad"
    assert outcome.model_visible["total"] == 1660
    assert not {"value", "rows", "next_offset", "offset"} & outcome.model_visible.keys()
    assert len(outcome.observation["complete_group_rows"]) == 1660


@pytest.mark.parametrize("state", ["pending", "resolved"])
def test_content_candidate_search_runs_before_event_or_nearby_place_is_verified(toolbox, state):
    from sentrix.domain.retrieval.query_conditions import QueryCondition

    build, _ = toolbox
    req = EvidenceRequirement(
        "activity",
        EvidenceType.PHOTO_SET,
        "活动与附近地点",
        conditions=(
            QueryCondition("event", "在河边吃饭", "anchor", state=state),
            QueryCondition("place", "海边", "mentioned", relation="near"),
        ),
    )
    result = build.execute(
        "search_memories",
        {"query": "海边", "requirement_id": "activity"},
        scope_id=SCOPE,
        now=NOW,
        requirements=(req,),
    )
    assert result.ok and result.observation["returned_asset_ids"]
    assert not result.model_visible["answer_eligible"]
    assert len(result.model_visible["conditions_not_applied"]) == 2
    assert "place" not in result.observation["filters_applied"]
    count = build.execute(
        "query_memory_facts",
        {"operation": "count", "subject": "asset", "requirement_id": "activity"},
        scope_id=SCOPE,
        now=NOW,
        requirements=(req,),
    )
    assert not count.ok and count.failure_reason == "unresolved_conditions"


def test_missing_person_data_preserves_identity_requirement_without_blocking_content_search(toolbox):
    from sentrix.domain.retrieval.query_conditions import QueryCondition

    build, _ = toolbox
    req = EvidenceRequirement(
        "person",
        EvidenceType.PHOTO_SET,
        "我在海边",
        conditions=(QueryCondition("person", "未登记的我", "depicted"),),
    )
    result = build.execute(
        "search_memories",
        {"query": "海边", "requirement_id": "person"},
        scope_id=SCOPE,
        now=NOW,
        requirements=(req,),
    )
    assert result.ok and result.observation["returned_asset_ids"]
    assert not result.model_visible["answer_eligible"]
    assert result.model_visible["conditions_not_applied"][0]["dimension"] == "person"
    assert result.model_visible["conditions_not_applied"][0]["required"] is True
    assert not any(c["dimension"] == "person" for c in result.model_visible["conditions_applied"])


def test_unfounded_update_leaves_conditions_intact_and_still_returns_search_candidates(toolbox):
    from sentrix.domain.retrieval.query_conditions import QueryCondition

    build, _ = toolbox
    pending = QueryCondition("time", "第三天", "capture", state="pending", anchor="trip")
    req = EvidenceRequirement("trip", EvidenceType.PHOTO_SET, "旅行第三天", conditions=(pending,))
    state = {}
    args = {
        "query": "海边",
        "requirement_id": "trip",
        "condition_updates": [QueryCondition("time", "2019-08-01", "capture", anchor="trip").payload()],
    }
    result = build.execute(
        "search_memories", args, scope_id=SCOPE, now=NOW, requirements=(req,), query_state=state
    )
    assert result.ok and result.observation["returned_asset_ids"]
    assert result.model_visible["condition_update_error"]["error"] == "condition_evidence_required"
    assert result.model_visible["conditions_not_applied"][0]["value"] == "第三天"
    assert not result.model_visible["answer_eligible"] and "trip" not in state
    count = build.execute(
        "query_memory_facts",
        {"operation": "count", "subject": "asset", **args},
        scope_id=SCOPE,
        now=NOW,
        requirements=(req,),
        query_state=state,
    )
    assert not count.ok and count.failure_reason == "condition_evidence_required"

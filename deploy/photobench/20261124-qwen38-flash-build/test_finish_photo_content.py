"""Caption-only writes retain successful fields and resume indexing without rewriting."""

import sqlite3
from types import SimpleNamespace

import finish_photo_content as finish
import pytest

from sentrix.application.retrieval.index_observation_text import IndexObservationText, TextIndexOutcome
from sentrix.domain.media.models import ObservationFact


def original():
    return SimpleNamespace(
        id="obs1",
        caption="",
        revision=3,
        activity="原活动",
        event_type="原分类",
        place="原地点",
        objects=("树木",),
        clothing=("原服装",),
        ocr_text="原OCR",
        source_text="原正文",
        confidence=0.81,
        facts=(ObservationFact("树木", "位于", "庭院", 0.8),),
    )


def test_caption_write_preserves_all_other_successful_fields():
    source = original()
    semantics = finish.caption_semantics(source, "模型中已核实的描述")
    assert semantics.caption == "模型中已核实的描述" and source.caption == ""
    for name in (
        "activity",
        "event_type",
        "place",
        "objects",
        "clothing",
        "facts",
        "ocr_text",
        "source_text",
        "confidence",
    ):
        assert getattr(semantics, name) == getattr(source, name)


def test_index_failure_reuses_applied_caption_without_incrementing_revision_again(tmp_path, monkeypatch):
    source = original()
    state = {"id": source.id, "caption": "", "revision": 3, "activity": source.activity}
    before = dict(state)
    applied, indexed, lexical = [], [], []

    def enrich(identifier, *, semantics):
        applied.append(identifier)
        source.caption, source.revision = semantics.caption, source.revision + 1
        state.update(caption=source.caption, revision=source.revision)
        return source

    outcomes = [TextIndexOutcome(False), TextIndexOutcome(True, rows=3)]

    def index(self, observation, event_id, *, force):
        indexed.append((observation.id, event_id, force))
        return outcomes.pop(0)

    repos = SimpleNamespace(
        observations=SimpleNamespace(get=lambda _: source, enrich=enrich),
        embedder=None,
        vectors=None,
        lexical=SimpleNamespace(refresh_from_observation=lambda o: lexical.append(o.id) or 1),
    )
    monkeypatch.setattr(finish, "OUTPUT", tmp_path)
    monkeypatch.setattr(finish.repair, "snapshot", lambda *args: dict(state))
    monkeypatch.setattr(IndexObservationText, "index", index)
    with pytest.raises(AssertionError):
        finish.apply_caption("asset1", before, "已核实描述", ["evt1"], repos)
    assert not (tmp_path / "captions/asset1/success.json").exists()
    assert finish.apply_caption("asset1", before, "已核实描述", ["evt1"], repos)["status"] == "completed"
    assert finish.apply_caption("asset1", before, "已核实描述", ["evt1"], repos)["status"] == "reused"
    assert applied == ["obs1"] and len(indexed) == 2 and lexical == ["obs1"]
    assert state["revision"] == 4 and state["activity"] == before["activity"]


def test_protection_digest_excludes_only_frozen_target_records(monkeypatch):
    monkeypatch.setattr(
        finish, "TABLES", ("observations", "events", "text_vectors", "observation_search_fts")
    )
    connection = sqlite3.connect(":memory:")
    connection.executescript("""
        CREATE TABLE observations(id TEXT, caption TEXT);
        CREATE TABLE events(id TEXT, summary TEXT);
        CREATE TABLE text_vectors(id TEXT, source_type TEXT, source_id TEXT, space TEXT, vector_json TEXT);
        CREATE TABLE observation_search_fts(observation_id TEXT, tokens TEXT);
        INSERT INTO observations VALUES('obs1',''),('protected','saved');
        INSERT INTO events VALUES('evt1','old'),('evt2','old'),('protected','saved');
        INSERT INTO text_vectors VALUES('v1','observation','obs1','semantic','old'),
            ('v2','event','evt2','episodic','old'),('v3','event','protected','episodic','saved');
        INSERT INTO observation_search_fts VALUES('obs1','old'),('protected','saved');
    """)
    plan = {
        "caption_before": {"asset1": {"id": "obs1"}},
        "caption_event_ids": {"asset1": ["evt1"]},
        "stale_events": [{"event": {"id": "evt2"}}],
    }
    before = finish.protected_fingerprints(connection, plan)
    connection.execute("UPDATE observations SET caption='new' WHERE id='obs1'")
    connection.execute("UPDATE events SET summary='new' WHERE id='evt2'")
    connection.execute("UPDATE text_vectors SET vector_json='new' WHERE id IN ('v1','v2')")
    connection.execute("UPDATE observation_search_fts SET tokens='new' WHERE observation_id='obs1'")
    assert finish.protected_fingerprints(connection, plan) == before
    connection.execute("UPDATE observations SET caption='unexpected' WHERE id='protected'")
    assert finish.protected_fingerprints(connection, plan)["observations"] != before["observations"]

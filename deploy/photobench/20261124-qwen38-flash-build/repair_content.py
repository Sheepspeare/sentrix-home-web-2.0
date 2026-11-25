"""Complete only frozen missing observations and recovered scene summaries.

Uses existing observers, prompts and repositories without opening embedded Qdrant.
Successful model responses survive write/index failures and are reused on resume.
"""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import httpx
from operate import ENDPOINT, MODEL, PRODUCT_UNIT, assert_idle, call, process_env
from recover_videos import DATA, ROOT, SCOPE, connect, save, sha

CONTENT = ROOT / "content"
OCR_REVIEW = "asset_042fdf27e328"
EMPTY_SMOKE = "asset_ad16dcecda08"


class ModelOutputError(AssertionError):
    """A response cannot supply content; no observation or summary may be applied."""


def missing_semantics(row):
    return not any(
        str(row.get(field) or "").strip() for field in ("caption", "activity", "ocr_text")
    ) and not any(json.loads(row.get(field) or "[]") for field in ("objects_json", "facts_json"))


def assert_snapshot(current, before, expected=None):
    if current == before:
        return "before"
    if expected is not None and current == expected:
        return "applied"
    raise RuntimeError("Target changed outside this repair; inspect before resuming")


def plan():
    path = CONTENT / "plan.json"
    assert not path.exists(), "Frozen plan already exists"
    accepted = json.loads((ROOT / "full-recovery-acceptance.json").read_text())
    assert accepted["structural_status"] == "accepted"
    events = accepted["new_scene_events"]
    assert len(events) == len({r["id"] for r in events}) == 955
    with connect() as connection:
        targets = []
        for gap in accepted["image_content_gaps"]:
            observation = dict(
                connection.execute(
                    "SELECT * FROM observations WHERE id=?", (gap["observation_id"],)
                ).fetchone()
            )
            if missing_semantics(observation):
                targets.append(
                    {
                        "asset_id": gap["id"],
                        "mode": "missing",
                        "before": observation,
                        "sha256": sha(gap["path"]),
                    }
                )
        reviewed = dict(
            connection.execute("SELECT o.* FROM observations o WHERE asset_id=?", (OCR_REVIEW,)).fetchone()
        )
        asset = connection.execute("SELECT path FROM assets WHERE id=?", (OCR_REVIEW,)).fetchone()
        targets.append(
            {"asset_id": OCR_REVIEW, "mode": "ocr_only", "before": reviewed, "sha256": sha(asset["path"])}
        )
        for target in targets:
            target["event_ids"] = [
                r[0]
                for r in connection.execute(
                    "SELECT event_id FROM event_observations WHERE observation_id=?",
                    (target["before"]["id"],),
                )
            ]
        assert any(t["asset_id"] == EMPTY_SMOKE for t in targets)
        with (CONTENT / "before-content.db").open("xb"):
            pass
        import sqlite3

        with sqlite3.connect(CONTENT / "before-content.db") as backup:
            connection.backup(backup)
    save(
        path,
        {
            "scope_id": SCOPE,
            "created_at": time.time(),
            "visual_targets": targets,
            "scene_events": events,
            "partial_caption_gaps_deferred": [
                r["id"]
                for r in accepted["image_content_gaps"]
                if r["id"] not in {t["asset_id"] for t in targets}
            ],
            "ocr_review": "Original frame inspected: long increasing price sequence is unsupported",
        },
    )
    print(json.dumps({"planned_visual": len(targets), "scene_events": len(events)}))


def completion(directory, messages, *, json_mode=False, response_json=False):
    from sentrix.application.memories.summarize_event import PLACEHOLDER_TITLES
    from sentrix.infrastructure.media.vlm_observer import parse_json_object

    directory.mkdir(parents=True, exist_ok=True)
    accepted = directory / "accepted-response.json"
    if accepted.exists():
        return json.loads(accepted.read_text())
    attempt = len(list(directory.glob("attempt-*"))) + 1
    attempt_dir = directory / f"attempt-{attempt:03d}"
    body = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": 4096,
        "temperature": 0,
        "stream": False,
        "enable_thinking": False,
    }
    if json_mode or response_json:
        body["response_format"] = {"type": "json_object"}
    save(attempt_dir / "request.json", body)
    started = time.time()
    try:
        response = httpx.post(ENDPOINT + "/chat/completions", json=body, timeout=180)
        save(attempt_dir / "response.json", {"status_code": response.status_code, "body": response.text})
        response.raise_for_status()
        payload = response.json()
        assert payload["model"] == MODEL
        choice = payload["choices"][0]
        if choice["finish_reason"] != "stop":
            raise ModelOutputError("Model output incomplete")
        assert not choice["message"].get("reasoning_content"), "Unexpected thinking output"
        content = choice["message"].get("content")
        if not isinstance(content, str) or not content.strip():
            raise ModelOutputError("Empty model output")
        parsed = parse_json_object(content)
        if parsed is None:
            raise ModelOutputError("Model output is not JSON")
        if not json_mode:
            if not all(
                isinstance(parsed.get(field), str) and parsed[field].strip() for field in ("summary", "title")
            ):
                raise ModelOutputError("Missing summary or title in model output")
            if parsed["title"] in PLACEHOLDER_TITLES:
                raise ModelOutputError("Model returned placeholder summary title")
        save(accepted, payload)
        return payload
    finally:
        save(attempt_dir / "timing.json", {"started_at": started, "finished_at": time.time()})


def repositories():
    from sentrix.infrastructure.models.wemm import WeMMSidecarEmbedder
    from sentrix.infrastructure.persistence.database import Database
    from sentrix.infrastructure.persistence.repositories.asset_places import SqliteAssetPlaceRepository
    from sentrix.infrastructure.persistence.repositories.assets import (
        SqliteAssetRepository,
        SqliteObservationRepository,
    )
    from sentrix.infrastructure.persistence.repositories.entities import SqliteEntityRepository
    from sentrix.infrastructure.persistence.repositories.events import SqliteEventRepository
    from sentrix.infrastructure.persistence.repositories.facts import SqliteFactRepository
    from sentrix.infrastructure.persistence.repositories.participants import SqliteEventParticipantRepository
    from sentrix.infrastructure.persistence.repositories.relationships import SqliteRelationshipRepository
    from sentrix.infrastructure.persistence.repositories.search_terms import SqliteLexicalIndex
    from sentrix.infrastructure.persistence.repositories.text_vectors import SqliteTextVectorRepository

    env = process_env(PRODUCT_UNIT)
    assert Path(env["SENTRIX_DATA_DIR"]).resolve() == DATA.resolve()
    db = Database(DATA / "sentrix.db", Path("/unused-migrations"))
    assets, observations = SqliteAssetRepository(db), SqliteObservationRepository(db)
    return SimpleNamespace(
        assets=assets,
        observations=observations,
        events=SqliteEventRepository(db),
        participants=SqliteEventParticipantRepository(db),
        entities=SqliteEntityRepository(db),
        relationships=SqliteRelationshipRepository(db),
        facts=SqliteFactRepository(db),
        vectors=SqliteTextVectorRepository(db),
        embedder=WeMMSidecarEmbedder(env.get("SENTRIX_WEMM_URL", "http://127.0.0.1:8102")),
        lexical=SqliteLexicalIndex(db, observations, assets=assets, places=SqliteAssetPlaceRepository(db)),
    )


def snapshot(table, identifier):
    assert table in {"observations", "events"}
    with connect() as connection:
        return dict(connection.execute(f"SELECT * FROM {table} WHERE id=?", (identifier,)).fetchone())


def repair_visual(target, repos):
    from sentrix.application.media.maintain_entities import MaintainObservationEntities
    from sentrix.application.memories.maintain_facts import MaintainFacts
    from sentrix.application.retrieval.index_observation_text import IndexObservationText
    from sentrix.domain.media.ports import ObservationSemantics
    from sentrix.infrastructure.assistant.model_endpoint import FixedModelName
    from sentrix.infrastructure.media.vlm_observer import OpenAIVlmObserver, parse_json_object

    directory = CONTENT / "visual" / target["asset_id"]
    before = target["before"]
    if (directory / "success.json").exists():
        success = json.loads((directory / "success.json").read_text())
        assert snapshot("observations", before["id"]) == success["after"]
        return "reused"
    asset = repos.assets.get(target["asset_id"])
    assert asset is not None and asset.scope_id == SCOPE and sha(asset.path) == target["sha256"]

    class RecordedObserver(OpenAIVlmObserver):
        calls = 0

        def _chat(self, prompt, image):
            self.calls += 1
            content = [{"type": "text", "text": prompt}]
            if image:
                content.append({"type": "image_url", "image_url": {"url": image}})
            payload = completion(
                directory / f"model-{self.calls}", [{"role": "user", "content": content}], json_mode=True
            )
            parsed = parse_json_object(payload["choices"][0]["message"]["content"])
            if parsed is None:
                raise ModelOutputError("Unparseable visual output")
            return parsed

    observer = RecordedObserver(ENDPOINT, FixedModelName(ENDPOINT, MODEL), max_tokens=4096)
    semantics = observer.observe(Path(asset.path))
    if semantics is None or not semantics.caption.strip():
        raise ModelOutputError("Missing visual caption after observation")
    original = repos.observations.get(before["id"])
    assert original is not None
    if target["mode"] == "ocr_only":
        semantics = ObservationSemantics(
            **{
                field: getattr(original, field)
                for field in (
                    "caption",
                    "place",
                    "activity",
                    "event_type",
                    "objects",
                    "clothing",
                    "facts",
                    "source_text",
                )
            },
            ocr_text=semantics.ocr_text,
            confidence=original.confidence,
        )
    elif asset.source_metadata.precise_address:
        semantics = replace(semantics, place=asset.source_metadata.precise_address)
    prepared = directory / "prepared.json"
    if not prepared.exists():
        save(prepared, {"semantics": asdict(semantics)})
    # A partial run can have committed the observation but not its indexes.
    current = snapshot("observations", before["id"])
    if current == before:
        observation = repos.observations.enrich(before["id"], semantics=semantics)
    else:
        assert current["revision"] == before["revision"] + 1
        assert current["caption"] == semantics.caption and current["ocr_text"] == semantics.ocr_text
        observation = repos.observations.get(before["id"])
    event_ids = target["event_ids"]
    event_id = event_ids[0] if event_ids else None
    outcome = IndexObservationText(embedder=repos.embedder, vectors=repos.vectors).index(
        observation, event_id, force=True
    )
    assert outcome.indexed and outcome.rows > 0, outcome
    if target["mode"] == "missing":
        MaintainObservationEntities(
            entities=repos.entities,
            events=repos.participants,
            assets=repos.assets,
            relationships=repos.relationships,
        ).execute(observation, event_id=event_id or "")
        MaintainFacts(
            facts=repos.facts,
            candidates=lambda item: item.facts,
            confirmed_people=repos.entities.confirmed_person_names,
        ).maintain(observation)
    lexical_rows = repos.lexical.refresh_from_observation(observation)
    assert lexical_rows > 0
    save(
        directory / "success.json",
        {
            "after": snapshot("observations", before["id"]),
            "text_index": asdict(outcome),
            "lexical_rows": lexical_rows,
            "event_ids": event_ids,
            "source_sha256": sha(asset.path),
        },
    )
    return "completed"


def repair_summary(before, repos, *, source_type="video_scene"):
    from sentrix.application.memories.summarize_event import (
        build_evidence,
        build_system_prompt,
        projection_from_reply,
    )
    from sentrix.domain.retrieval import scheme
    from sentrix.domain.retrieval.ports import VectorRecord
    from sentrix.infrastructure.persistence.repositories.events import _with_speech

    identifier = before["id"]
    directory = CONTENT / "summary" / identifier
    if (directory / "success.json").exists():
        success = json.loads((directory / "success.json").read_text())
        assert snapshot("events", identifier) == success["after"]
        return "reused"
    event = repos.events.get(identifier)
    assert event is not None and event.scope_id == SCOPE and event.source_type == (source_type or "")
    observations = [repos.observations.get(oid) for oid in repos.events.observation_ids(identifier)]
    assert observations and all(o is not None and o.caption.strip() for o in observations)
    evidence = build_evidence(event, observations, repos.participants.list_participants(identifier))
    payload = completion(
        directory / "model",
        [{"role": "system", "content": build_system_prompt()}, {"role": "user", "content": evidence}],
    )
    projection = projection_from_reply(payload["choices"][0]["message"]["content"], event=event)
    if projection is None or not projection.summary.strip():
        raise ModelOutputError("Missing model summary")
    summary = _with_speech(projection.summary, event.source_metadata)
    current = snapshot("events", identifier)
    if current == before:
        repos.events.apply_summary(
            identifier,
            title=projection.title,
            event_type=projection.event_type,
            activity=projection.activity,
            summary=projection.summary,
        )
    else:
        assert current["revision"] == before["revision"] + 1
        assert current["summary"] == summary and current["title"] == projection.title
    updated = repos.events.get(identifier)
    text = " ".join((updated.title, updated.summary, updated.place, updated.activity)).strip()
    vector = repos.embedder.embed_query(text)
    assert len(vector) == repos.embedder.dimension, "Event embedding unavailable"
    repos.vectors.upsert(
        VectorRecord(
            scope_id=SCOPE,
            space=scheme.EPISODIC_SPACE,
            source_type="event",
            source_id=identifier,
            model_name=repos.embedder.model_id,
            vector=tuple(vector),
            metadata={"observation_id": observations[0].id},
        )
    )
    save(
        directory / "success.json",
        {
            "after": snapshot("events", identifier),
            "summary_model": payload["model"],
            "text_dimension": len(vector),
        },
    )
    return "completed"


def run(smoke=False):
    assert_idle()
    health = call(18138, "/health")
    assert health["active_requests"] == health["inflight_requests"] == 0
    assert health["model"] == MODEL and health["enable_thinking"] is False
    frozen = json.loads((CONTENT / "plan.json").read_text())
    assert frozen["scope_id"] == SCOPE
    repos = repositories()
    targets = frozen["visual_targets"]
    events = frozen["scene_events"]
    if smoke:
        targets = [t for t in targets if t["asset_id"] in {EMPTY_SMOKE, OCR_REVIEW}]
        scene_ids = {eid for t in targets for eid in t["event_ids"]}
        events = [e for e in events if e["id"] in scene_ids]
    started = time.time()
    phase = "smoke" if smoke else "full"
    progress = {"started_at": started, "phase": phase, "results": [], "status": "running"}
    try:
        for name, items, function in (
            ("visual", targets, repair_visual),
            ("summary", events, repair_summary),
        ):
            pool = ThreadPoolExecutor(max_workers=4)
            try:
                futures = {pool.submit(function, item, repos): item for item in items}
                for future in as_completed(futures):
                    item = futures[future]
                    status = future.result()
                    progress["results"].append(
                        {"kind": name, "id": item.get("asset_id", item.get("id")), "status": status}
                    )
                    progress["updated_at"] = time.time()
                    save(CONTENT / phase / "progress.json", progress)
            finally:
                pool.shutdown(wait=True, cancel_futures=True)
        progress["status"] = "completed"
    except Exception as error:
        progress.update(status="failed", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        progress["finished_at"] = time.time()
        save(CONTENT / phase / "terminal.json", progress)
        print(json.dumps({k: v for k, v in progress.items() if k != "results"}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("plan", "smoke", "run"))
    args = parser.parse_args()
    if args.action == "plan":
        CONTENT.mkdir(parents=True, exist_ok=True)
        plan()
    else:
        run(smoke=args.action == "smoke")

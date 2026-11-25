"""Read-only database verification and a focused OCR check of the failed smoke."""

import argparse
import json
from pathlib import Path

from operate import EVAL_UNIT, GATEWAY_UNIT, PRODUCT_UNIT, call, systemctl
from recover_videos import SCOPE, connect, save, sha
from repair_content import CONTENT, EMPTY_SMOKE, OCR_REVIEW, completion
from verify_video_smoke import differences


def verify():
    frozen = json.loads((CONTENT / "plan.json").read_text())
    targets = [t for t in frozen["visual_targets"] if t["asset_id"] in {EMPTY_SMOKE, OCR_REVIEW}]
    observations = tuple(t["before"]["id"] for t in targets)
    event_ids = tuple(eid for t in targets for eid in t["event_ids"])
    assert len(observations) == len(set(event_ids)) == 2
    result = {"scope_id": SCOPE, "targets": [], "events": [], "read_only": True}
    with connect() as connection:
        connection.execute(
            "ATTACH DATABASE ? AS old", ((CONTENT / "before-content.db").as_uri() + "?mode=ro",)
        )
        result["preserved"] = {
            "assets": differences(connection, "assets"),
            "observations": differences(connection, "observations", "b.id NOT IN (?,?)", observations),
            "events": differences(connection, "events", "b.id NOT IN (?,?)", event_ids),
            "memory_vectors": differences(connection, "memory_vectors"),
            "text_vectors": differences(
                connection,
                "text_vectors",
                "NOT ((b.source_type='observation' AND b.source_id IN (?,?)) "
                "OR (b.source_type='event' AND b.source_id IN (?,?)))",
                observations + event_ids,
            ),
        }
        assert all(v["missing"] == v["changed_count"] == 0 for v in result["preserved"].values())
        for target in targets:
            identifier = target["asset_id"]
            asset = dict(connection.execute("SELECT * FROM assets WHERE id=?", (identifier,)).fetchone())
            assert sha(asset["path"]) == target["sha256"] == asset["content_sha256"]
            current = dict(
                connection.execute("SELECT * FROM observations WHERE asset_id=?", (identifier,)).fetchone()
            )
            success = json.loads((CONTENT / "visual" / identifier / "success.json").read_text())
            assert current == success["after"]
            if target["mode"] == "ocr_only":
                for field, value in target["before"].items():
                    if field not in {"ocr_text", "revision", "updated_at"}:
                        assert current[field] == value, field
            counts = {
                "text": connection.execute(
                    "SELECT count(*) FROM text_vectors WHERE source_type='observation' AND source_id=?",
                    (current["id"],),
                ).fetchone()[0],
                "visual": connection.execute(
                    "SELECT count(*) FROM memory_vectors WHERE source_id=? AND space='visual'", (identifier,)
                ).fetchone()[0],
                "lexical": connection.execute(
                    "SELECT count(*) FROM observation_search_fts WHERE observation_id=?", (current["id"],)
                ).fetchone()[0],
            }
            assert counts["text"] >= 2 and counts["visual"] >= 1 and counts["lexical"] > 0
            result["targets"].append({"asset": asset, "observation": current, "index_counts": counts})
        for identifier in event_ids:
            event = dict(connection.execute("SELECT * FROM events WHERE id=?", (identifier,)).fetchone())
            success = json.loads((CONTENT / "summary" / identifier / "success.json").read_text())
            assert event == success["after"] and event["summary"].strip()
            vector = dict(
                connection.execute(
                    "SELECT * FROM text_vectors WHERE source_type='event' "
                    "AND source_id=? AND space='episodic'",
                    (identifier,),
                ).fetchone()
            )
            assert vector["dimension"] == 2048
            result["events"].append(
                {"event": event, "vector_id": vector["id"], "dimension": vector["dimension"]}
            )
    result["runtime_pids"] = {
        unit: systemctl("show", unit, "-p", "MainPID", "--value")
        for unit in (PRODUCT_UNIT, EVAL_UNIT, GATEWAY_UNIT)
    }
    result["gateway"] = call(18138, "/health")
    result["evaluation_work"] = call(8874, "/api/v1/active-work")
    result["technical_status"] = "accepted_writes_and_indexes"
    result["content_status"] = "failed_ocr_hallucination_requires_focused_recheck"
    save(CONTENT / "smoke" / "verification.json", result)
    print(
        json.dumps(
            {
                "technical_status": result["technical_status"],
                "content_status": result["content_status"],
                "preserved": result["preserved"],
            }
        )
    )


def recheck():
    from sentrix.infrastructure.media.vlm_observer import _encode_image

    question = (
        "请逐字抄写这张原图中能清楚辨认的文字，保留原语言。"
        "只抄实际看得见且能辨认的字符，不根据场景补写菜单类别、货币或价格，"
        "不要接续数字序列；模糊、遮挡或无法确认的内容不抄。"
        '只返回JSON：{"ocr_text":"能确认的原文，若无则为空字符串",'
        '"unreadable_parts":"哪些区域看不清"}。'
    )
    for identifier in (EMPTY_SMOKE, OCR_REVIEW):
        with connect() as connection:
            asset = dict(connection.execute("SELECT * FROM assets WHERE id=?", (identifier,)).fetchone())
        assert asset["scope_id"] == SCOPE
        image = _encode_image(Path(asset["path"]), max_side=1280)
        assert image
        payload = completion(
            CONTENT / "ocr-recheck" / identifier / "model",
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": question},
                        {"type": "image_url", "image_url": {"url": image}},
                    ],
                }
            ],
            json_mode=True,
        )
        print(
            json.dumps(
                {
                    "asset_id": identifier,
                    "source_sha256": sha(asset["path"]),
                    "result": payload["choices"][0]["message"]["content"],
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("verify", "recheck"))
    if parser.parse_args().action == "verify":
        verify()
    else:
        recheck()

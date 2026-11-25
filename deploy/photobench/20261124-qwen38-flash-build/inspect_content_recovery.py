"""Inspect frozen content recovery results and raw failures without model requests."""

import argparse
import json
from collections import Counter
from pathlib import Path

from operate import EVAL_UNIT, GATEWAY_UNIT, PRODUCT_UNIT, call, systemctl
from recover_videos import connect, save, sha
from repair_content import CONTENT
from verify_video_smoke import differences


def inspect(output):
    plan = json.loads((CONTENT / "plan.json").read_text())
    observations = tuple(t["before"]["id"] for t in plan["visual_targets"])
    events = tuple(e["id"] for e in plan["scene_events"])
    indexed_events = tuple(sorted(set(events) | {e for t in plan["visual_targets"] for e in t["event_ids"]}))
    op = ",".join("?" for _ in observations)
    ep = ",".join("?" for _ in events)
    ip = ",".join("?" for _ in indexed_events)
    result = {"read_only": True, "visual": [], "summary": [], "requests": []}
    with connect() as connection:
        connection.execute(
            "ATTACH DATABASE ? AS old", ((CONTENT / "before-content.db").as_uri() + "?mode=ro",)
        )
        result["preserved"] = {
            "assets": differences(connection, "assets"),
            "memory_vectors": differences(connection, "memory_vectors"),
            "observations": differences(connection, "observations", f"b.id NOT IN ({op})", observations),
            "events": differences(connection, "events", f"b.id NOT IN ({ep})", events),
            "text_vectors": differences(
                connection,
                "text_vectors",
                f"NOT ((b.source_type='observation' AND b.source_id IN ({op})) "
                f"OR (b.source_type='event' AND b.source_id IN ({ip})))",
                observations + indexed_events,
            ),
        }
        linked_changes = differences(
            connection,
            "text_vectors",
            f"b.source_type='event' AND b.source_id IN ({ip}) AND b.source_id NOT IN ({ep})",
            indexed_events + events,
        )
        result["linked_event_index_changes"] = []
        for vector_id in linked_changes["changed_ids"]:
            vector = dict(
                connection.execute("SELECT * FROM text_vectors WHERE id=?", (vector_id,)).fetchone()
            )
            observation_id = json.loads(vector["metadata_json"])["observation_id"]
            matching = connection.execute(
                "SELECT vector_json,dimension FROM text_vectors WHERE source_type='observation' "
                "AND source_id=? AND space='episodic' AND model_name=?",
                (observation_id, vector["model_name"]),
            ).fetchone()
            assert observation_id in observations and matching is not None
            assert (
                vector["vector_json"] == matching["vector_json"]
                and vector["dimension"] == matching["dimension"]
            )
            assert vector["space"] == "episodic" and vector["dimension"] == 2048
            result["linked_event_index_changes"].append(
                {
                    "vector_id": vector_id,
                    "event_id": vector["source_id"],
                    "observation_id": observation_id,
                    "reason": "IndexObservationText also writes the linked event projection",
                }
            )
        for target in plan["visual_targets"]:
            identifier = target["asset_id"]
            current = dict(
                connection.execute(
                    "SELECT * FROM observations WHERE id=?", (target["before"]["id"],)
                ).fetchone()
            )
            asset = dict(connection.execute("SELECT * FROM assets WHERE id=?", (identifier,)).fetchone())
            success = CONTENT / "visual" / identifier / "success.json"
            saved = json.loads(success.read_text()) if success.exists() else None
            result["visual"].append(
                {
                    "id": identifier,
                    "path": asset["path"],
                    "observation": current,
                    "file_sha_matches": sha(asset["path"]) == target["sha256"],
                    "success": bool(saved),
                    "success_matches": bool(saved and saved["after"] == current),
                    "changed_from_plan": current != target["before"],
                    "ocr_status": saved.get("ocr_status") if saved else None,
                    "text_rows": connection.execute(
                        "SELECT count(*) FROM text_vectors WHERE source_type='observation' AND source_id=?",
                        (current["id"],),
                    ).fetchone()[0],
                    "lexical_rows": connection.execute(
                        "SELECT count(*) FROM observation_search_fts WHERE observation_id=?", (current["id"],)
                    ).fetchone()[0],
                }
            )
        for before in plan["scene_events"]:
            identifier = before["id"]
            current = dict(connection.execute("SELECT * FROM events WHERE id=?", (identifier,)).fetchone())
            success = CONTENT / "summary" / identifier / "success.json"
            saved = json.loads(success.read_text()) if success.exists() else None
            result["summary"].append(
                {
                    "id": identifier,
                    "success": bool(saved),
                    "success_matches": bool(saved and saved["after"] == current),
                    "changed_from_plan": current != before,
                    "summary": current["summary"],
                    "vector_dimensions": [
                        r[0]
                        for r in connection.execute(
                            "SELECT dimension FROM text_vectors WHERE source_type='event' "
                            "AND source_id=? AND space='episodic'",
                            (identifier,),
                        )
                    ],
                }
            )
    for kind in ("visual", "summary"):
        for response in sorted((CONTENT / kind).glob("*/model*/attempt-*/response.json")):
            raw = json.loads(response.read_text())
            request = json.loads(response.with_name("request.json").read_text())
            row = {
                "path": str(response),
                "kind": kind,
                "id": response.relative_to(CONTENT).parts[1],
                "status_code": raw["status_code"],
                "enable_thinking": request.get("enable_thinking"),
                "max_tokens": request["max_tokens"],
                "requested_model": request["model"],
            }
            if raw["status_code"] == 200:
                payload = json.loads(raw["body"])
                choice = payload["choices"][0]
                content = choice["message"].get("content") or ""
                row.update(
                    model=payload["model"],
                    finish_reason=choice["finish_reason"],
                    usage=payload.get("usage"),
                    reasoning=bool(choice["message"].get("reasoning_content")),
                    content_length=len(content),
                    content_head=content[:300],
                    content_tail=content[-300:],
                )
            else:
                row["error_body"] = raw["body"][:1000]
            result["requests"].append(row)
    result["counts"] = {
        kind: {
            "planned": len(result[kind]),
            "saved": sum(r["success"] for r in result[kind]),
            "matching": sum(r["success_matches"] for r in result[kind]),
        }
        for kind in ("visual", "summary")
    }
    result["response_counts"] = dict(
        Counter(f"{r['status_code']}/{r.get('finish_reason')}" for r in result["requests"])
    )
    result["runtime_pids"] = {
        unit: systemctl("show", unit, "-p", "MainPID", "--value")
        for unit in (PRODUCT_UNIT, EVAL_UNIT, GATEWAY_UNIT)
    }
    result["gateway"] = call(18138, "/health")
    result["active_work"] = call(8874, "/api/v1/active-work")
    save(output, result)
    print(
        json.dumps(
            {k: result[k] for k in ("counts", "response_counts", "preserved", "gateway", "active_work")}
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    inspect(parser.parse_args().output)

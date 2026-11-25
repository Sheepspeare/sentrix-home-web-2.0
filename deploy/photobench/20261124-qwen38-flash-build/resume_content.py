"""Finish unattempted content while retaining isolated output failures for review."""

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx
import repair_content as repair
from recover_videos import save

OUTPUT_ERRORS = {
    "Model output incomplete",
    "Model output is not JSON",
    "Unparseable visual output",
    "Missing visual caption after observation",
    "Missing model summary",
}


def execute(kind, item, repos):
    identifier = item.get("asset_id", item.get("id"))
    directory = repair.CONTENT / kind / identifier
    function = repair.repair_visual if kind == "visual" else repair.repair_summary
    if not (directory / "success.json").exists():
        # An unfinished model attempt is kept for individual review, never silently replayed.
        attempts = list(directory.glob("model*/attempt-*/request.json"))
        unfinished = [p for p in attempts if not (p.parent.parent / "accepted-response.json").exists()]
        if unfinished:
            return {
                "kind": kind,
                "id": identifier,
                "status": "deferred_prior_model_failure",
                "attempts": [str(p.parent) for p in unfinished],
            }
        if kind == "summary":
            ids = repos.events.observation_ids(identifier)
            observations = [repos.observations.get(oid) for oid in ids]
            if not observations or any(o is None or not o.caption.strip() for o in observations):
                return {"kind": kind, "id": identifier, "status": "deferred_missing_visual_evidence"}
    try:
        status = function(item, repos)
    except httpx.HTTPStatusError as error:
        response = error.response
        if response.status_code != 400:
            raise
        try:
            detail = response.json()["error"]
        except (ValueError, KeyError, TypeError):
            raise error from None
        if not (
            detail.get("code") == "invalid_parameter_error"
            and detail.get("message", "").startswith(
                "Model output became abnormal while generating a JSON response for response_format."
            )
        ):
            raise
        return {
            "kind": kind,
            "id": identifier,
            "status": "failed_model_output",
            "error": "Upstream aborted abnormal JSON generation",
            "evidence": str(directory),
            "upstream_error": detail,
        }
    except AssertionError as error:
        if str(error) not in OUTPUT_ERRORS:
            raise
        return {
            "kind": kind,
            "id": identifier,
            "status": "failed_model_output",
            "error": str(error),
            "evidence": str(directory),
        }
    return {"kind": kind, "id": identifier, "status": status}


def run(output):
    repair.assert_idle()
    health = repair.call(18138, "/health")
    assert health["active_requests"] == health["inflight_requests"] == 0
    assert health["model"] == repair.MODEL and health["enable_thinking"] is False
    assert not output.exists(), "Inspect existing output before registering another continuation"
    output.mkdir(parents=True)
    plan = json.loads((repair.CONTENT / "plan.json").read_text())
    assert plan["scope_id"] == repair.SCOPE
    repos = repair.repositories()
    progress = {"status": "running", "started_at": time.time(), "results": []}
    try:
        for kind, items in (("visual", plan["visual_targets"]), ("summary", plan["scene_events"])):
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(execute, kind, item, repos) for item in items]
                try:
                    for future in as_completed(futures):
                        progress["results"].append(future.result())
                        progress["updated_at"] = time.time()
                        save(output / "progress.json", progress)
                except Exception:
                    for future in futures:
                        future.cancel()
                    raise
        progress["unresolved_count"] = sum(
            r["status"] not in {"completed", "reused"} for r in progress["results"]
        )
        progress["status"] = "completed_with_gaps" if progress["unresolved_count"] else "completed"
    except Exception as error:
        progress.update(status="failed", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        progress["finished_at"] = time.time()
        save(output / "terminal.json", progress)
        print(json.dumps({k: v for k, v in progress.items() if k != "results"}))
    return 1 if progress["unresolved_count"] else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    raise SystemExit(run(parser.parse_args().output))

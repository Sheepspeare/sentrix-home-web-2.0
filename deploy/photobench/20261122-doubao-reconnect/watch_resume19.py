"""Monitor the new exact run without submitting any additional work."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "20261120-doubao-reuse"))
import watch_run  # noqa: E402

original_review = watch_run.review_reason


def review(snapshot):
    for field in (
        "exact_run_matches",
        "evaluation_app_matches",
        "source_manifest_unchanged",
        "prior_manifests_unchanged",
        "writer_source_matches",
    ):
        if not snapshot[field]:
            return "Nineteen-QA continuation invariant changed: " + field
    budgets = snapshot["execution_budgets"]
    if budgets["qa_concurrency"] != 4 or budgets["turn_timeout"] != 3660:
        return "Doubao continuation concurrency or turn timeout changed"
    if not snapshot["reasoning"] or not snapshot["graph_enabled"]:
        return "Doubao continuation reasoning or graph setting changed"
    if snapshot["status"] not in {"pending", "running", "cancelling"} and any(
        row.get("id") == snapshot["run_id"] and row.get("worker_alive") for row in snapshot["active"]
    ):
        return ""
    return original_review(snapshot)


if __name__ == "__main__":
    watch_run.review_reason = review
    sys.exit(watch_run.main())

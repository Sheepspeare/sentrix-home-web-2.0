"""Remove visually disproven smoke text and retain every previous revision.

This removes unsupported output; it does not supply a human OCR transcription.
Only the two reviewed frame records and one derived summary can change.
"""

import json
import shutil
from dataclasses import asdict

import repair_content as repair
from operate import assert_idle
from recover_videos import save, sha

UNSUPPORTED_CAPTION = "盘子下方垫着印有“VIP SPECIAL”字样的菜单纸。"
REVIEW_EVENT = "evt_45f85ad13474"


def corrected_semantics(observation, identifier):
    from sentrix.domain.media.ports import ObservationSemantics

    caption = observation.caption
    if identifier == repair.EMPTY_SMOKE:
        assert UNSUPPORTED_CAPTION in caption
        caption = caption.replace(UNSUPPORTED_CAPTION, "盘子下方垫着菜单纸。")
    return ObservationSemantics(
        **{
            field: getattr(observation, field)
            for field in (
                "place",
                "activity",
                "event_type",
                "objects",
                "clothing",
                "facts",
                "source_text",
                "confidence",
            )
        },
        caption=caption,
        ocr_text="",
    )


def main():
    from sentrix.application.retrieval.index_observation_text import IndexObservationText

    assert_idle()
    original_root = repair.CONTENT
    review = original_root / "smoke-review-v1"
    frozen = json.loads((original_root / "plan.json").read_text())
    repos = repair.repositories()
    for identifier in (repair.EMPTY_SMOKE, repair.OCR_REVIEW):
        target = next(t for t in frozen["visual_targets"] if t["asset_id"] == identifier)
        directory = original_root / "visual" / identifier
        prior = json.loads((directory / "success.json").read_text())
        result_path = review / identifier / "result.json"
        if result_path.exists():
            result = json.loads(result_path.read_text())
            assert repair.snapshot("observations", target["before"]["id"]) == result["after"]
            continue
        before_path = review / identifier / "before-success.json"
        if not before_path.exists():
            save(before_path, prior)
        else:
            prior = json.loads(before_path.read_text())
        asset = repos.assets.get(identifier)
        assert asset and sha(asset.path) == target["sha256"]
        observation = repos.observations.get(target["before"]["id"])
        assert observation
        current = repair.snapshot("observations", observation.id)
        if current == prior["after"]:
            semantics = corrected_semantics(observation, identifier)
            observation = repos.observations.enrich(observation.id, semantics=semantics)
            save(review / identifier / "applied.json", repair.snapshot("observations", observation.id))
        else:
            assert current == json.loads((review / identifier / "applied.json").read_text())
        event_id = target["event_ids"][0]
        outcome = IndexObservationText(embedder=repos.embedder, vectors=repos.vectors).index(
            observation, event_id, force=True
        )
        assert outcome.indexed and outcome.rows == 3
        lexical = repos.lexical.refresh_from_observation(observation)
        assert lexical > 0
        result = {
            **prior,
            "after": repair.snapshot("observations", observation.id),
            "text_index": asdict(outcome),
            "lexical_rows": lexical,
            "ocr_status": "unrecovered_unsupported_text_removed",
            "review_evidence": str(review / identifier),
            "previous_success_preserved": str(before_path),
        }
        save(result_path, result)
        save(directory / "success.json", result)
    # The first scene summary quoted the rejected text; rebuild only this invalidated projection.
    directory = original_root / "summary" / REVIEW_EVENT
    before_path = review / REVIEW_EVENT / "before-success.json"
    if not before_path.exists():
        save(before_path, json.loads((directory / "success.json").read_text()))
    before = json.loads(before_path.read_text())["after"]
    repair.CONTENT = review
    repair.repair_summary(before, repos)
    repair.CONTENT = original_root
    rewritten = json.loads((review / "summary" / REVIEW_EVENT / "success.json").read_text())
    assert "VIP" not in rewritten["after"]["summary"].upper()
    rewritten["previous_success_preserved"] = str(before_path)
    save(directory / "success.json", rewritten)
    shutil.copyfile(review / "summary" / REVIEW_EVENT / "success.json", review / "summary-result.json")
    save(
        review / "terminal.json",
        {
            "status": "completed_captions_and_indexes_ocr_unrecovered",
            "observations": [repair.EMPTY_SMOKE, repair.OCR_REVIEW],
            "resummarized_event": REVIEW_EVENT,
            "new_ocr_transcription": False,
            "remaining_partial_caption_gaps": frozen["partial_caption_gaps_deferred"],
        },
    )
    print(
        json.dumps(
            {
                "status": "completed_captions_and_indexes_ocr_unrecovered",
                "cleared_ocr": 2,
                "resummarized_event": REVIEW_EVENT,
            }
        )
    )


if __name__ == "__main__":
    main()

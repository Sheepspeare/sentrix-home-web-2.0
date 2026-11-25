import importlib.util
from collections import Counter
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("ablation_prepare", Path(__file__).with_name("prepare.py"))
prep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prep)


def fixture():
    sizes = [582, 50, 381]
    taxonomy = []
    for modality, size in zip(("图片", "视频", "邮件"), sizes, strict=True):
        for offset in range(size):
            number = len(taxonomy)
            taxonomy.append(
                {
                    "qa_id": f"atm-{number}",
                    "n": number + 1,
                    "modality": modality,
                    "native_qtype": ("number", "open_end", "list_recall")[offset % 3],
                    "primary": f"A{offset % 14}",
                    "question": "fixture question",
                }
            )
    for row, qid in zip(taxonomy[:4], prep.REPRESENTATIVES, strict=True):
        row["qa_id"] = qid
    histories = {
        name: [
            {
                "qa_id": row["qa_id"],
                "termination_reason": "step_budget_exhausted" if 10 <= index < 60 else "answered",
                "judge": {"normalized_score": 1},
            }
            for index, row in enumerate(taxonomy)
        ]
        for name in prep.RUNS
    }
    return taxonomy, histories


def test_selection_is_reproducible_and_partitions_all_1013_ids():
    taxonomy, histories = fixture()
    chosen, holdout = prep.select(taxonomy, histories)
    reordered, reordered_holdout = prep.select(
        list(reversed(taxonomy)), {k: list(reversed(v)) for k, v in histories.items()}
    )
    assert chosen == reordered and holdout == reordered_holdout
    ids = {row["qa_id"] for row in chosen}
    assert len(chosen) == len(ids) == 60 and len(holdout) == 953
    assert ids.isdisjoint(holdout)
    assert ids | set(holdout) == {row["qa_id"] for row in taxonomy}
    assert prep.REPRESENTATIVES.keys() <= ids
    assert Counter((r["modality"], r["qtype"]) for r in chosen) == prep.QUOTAS
    assert {r["ability"] for r in chosen} == {r["primary"] for r in taxonomy}
    stable = [r for r in chosen if r["stability_subset"]]
    assert len(stable) == 20
    assert Counter(r["modality"] for r in stable) == {"图片": 12, "视频": 4, "邮件": 4}


def test_duplicate_or_mismatched_history_ids_are_rejected():
    taxonomy, histories = fixture()
    histories[prep.RUNS[0]][0] = histories[prep.RUNS[0]][1]
    with pytest.raises(ValueError, match="unique historical"):
        prep.select(taxonomy, histories)
    taxonomy, histories = fixture()
    histories[prep.RUNS[0]][0]["qa_id"] = "unknown"
    with pytest.raises(ValueError, match="differ from taxonomy"):
        prep.select(taxonomy, histories)

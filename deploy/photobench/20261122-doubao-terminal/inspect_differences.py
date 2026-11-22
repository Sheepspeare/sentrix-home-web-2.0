"""Read changed field identities after the terminal preservation check stopped."""

import json
from collections import Counter

from inspect_terminal import PRIOR, ROOT, RUN, SplitRunReader, fingerprint, identity, save

reader = SplitRunReader(ROOT, ROOT / RUN)
state = reader.state()
current = {i["qa_id"]: i for i in state["items"]}
old = json.loads((PRIOR / "inventory.json").read_text())
restore = json.loads((PRIOR / "restore-four-response.json").read_text())
sources = {}
for run in (old["run_id"], restore["seed_run_id"]):
    previous = SplitRunReader(ROOT, ROOT / run).state()
    changed = []
    counts = Counter()
    for item in previous["items"]:
        after = current[item["qa_id"]]
        fields = [
            k for k in item.keys() | after.keys() if fingerprint(item.get(k)) != fingerprint(after.get(k))
        ]
        if fields:
            counts.update(fields)
            changed.append(
                {
                    "qa_id": item["qa_id"],
                    "fields": fields,
                    "before": {k: identity(item.get(k)) for k in fields},
                    "after": {k: identity(after.get(k)) for k in fields},
                }
            )
    sources[run] = {"changed_items": len(changed), "field_counts": dict(counts), "changes": changed}
result = {"run_id": RUN, "sources": sources}
save("preservation-differences.json", result)
print(json.dumps({run: {k: v for k, v in fact.items() if k != "changes"} for run, fact in sources.items()}))

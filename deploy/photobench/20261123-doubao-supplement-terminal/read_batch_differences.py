"""Read scoring-batch field changes after the strict terminal precheck stopped."""

import json

from inspect_terminal import BATCH, ROOT, RUN, SplitRunReader, fingerprint, identity, source

state = SplitRunReader(ROOT, ROOT / RUN).state()
original, _, _, _ = source()
before = next(b for b in original["scoring_batches"] if b["batch_id"] == BATCH)
after = next(b for b in state["scoring_batches"] if b["batch_id"] == BATCH)
print(
    json.dumps(
        {
            k: {"before": identity(before.get(k)), "after": identity(after.get(k))}
            for k in before.keys() | after.keys()
            if fingerprint(before.get(k)) != fingerprint(after.get(k))
        },
        ensure_ascii=False,
    ),
    flush=True,
)

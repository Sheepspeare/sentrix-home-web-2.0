"""Read the frozen question definition without hydrating answer traces or starting services."""

import argparse
import json
from pathlib import Path

from photobench.domain.datasets.qa_taxonomy import qa_fingerprint
from photobench.infrastructure.results.split_json import thaw
from photobench.infrastructure.results.split_store import SplitRunReader

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--run", required=True)
    args = parser.parse_args()
    reader = SplitRunReader(args.results, args.results / args.run)
    snapshot = thaw(reader.reader.value(reader.reader.walk(reader.manifest["root"], ["qa_snapshot"])))
    assert snapshot["definition_sha256"] == qa_fingerprint(snapshot["rows"])
    print(json.dumps({"read_revision": reader.revision, "qa_snapshot": snapshot}, ensure_ascii=False))

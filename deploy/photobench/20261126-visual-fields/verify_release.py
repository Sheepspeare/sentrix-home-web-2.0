"""Verify a committed source bundle before isolated field tests."""

import compileall
import hashlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
manifest = json.loads((root / "source-sha256.json").read_text())
for name, expected in manifest.items():
    source = root / name
    assert hashlib.sha256(source.read_bytes()).hexdigest() == expected, name
assert compileall.compile_dir(str(root / "apps/api/src"), quiet=1)
assert compileall.compile_dir(str(root / "deploy/photobench/20261126-visual-fields"), quiet=1)
print(
    json.dumps(
        {
            "revision": json.loads((root / "build-info.json").read_text())["revision"],
            "files": len(manifest),
            "sha256": "verified",
            "compile": "passed",
        }
    )
)

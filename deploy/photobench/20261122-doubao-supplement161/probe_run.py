"""Read the exact authorized Doubao 161-QA supplement and preserved sources."""

import json

from supplement import snapshot

if __name__ == "__main__":
    print(json.dumps(snapshot(), ensure_ascii=False), flush=True)

import json
import os
from pathlib import Path

def main():
    aod = str(os.environ.get("ANTITHESIS_OUTPUT_DIR", "."))
    outfile = Path(aod) / "sdk.jsonl"
    m = {"antithesis_setup": { "status": "complete", "details": {"message": "Set up complete - ready for testing!" }}}
    print(json.dumps(m, indent=2))
    with outfile.open("w", encoding="utf-8") as f:
        f.writelines(json.dumps(m))

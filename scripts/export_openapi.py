"""Save the API's OpenAPI document to openapi.json in the repo root.

FastAPI builds the OpenAPI description from the code (routes, request
models, response models, documented error codes). This script downloads it
from the running service and writes it as readable, indented JSON so it can
be committed, reviewed in diffs and imported into other tools.

Run from the repo root while the stack is up:
    python scripts/export_openapi.py
"""
import json
import sys
import urllib.error
import urllib.request

SOURCE = "http://localhost:8000/openapi.json"
TARGET = "openapi.json"

try:
    with urllib.request.urlopen(SOURCE, timeout=5) as resp:
        spec = json.load(resp)
except urllib.error.URLError as err:
    sys.exit(f"Could not reach {SOURCE} ({err.reason}). Is the stack running? Try: docker compose up -d")

with open(TARGET, "w", encoding="utf-8", newline="\n") as f:
    json.dump(spec, f, indent=2, ensure_ascii=False)
    f.write("\n")

paths = spec.get("paths", {})
operations = sum(len(methods) for methods in paths.values())
print(f"Wrote {TARGET}: OpenAPI {spec.get('openapi')}, {len(paths)} paths, {operations} operations")
for path, methods in paths.items():
    for method, op in methods.items():
        codes = ", ".join(sorted(op.get("responses", {})))
        print(f"  {method.upper():6} {path:28} responses: {codes}")
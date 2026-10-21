"""Rate-limit demo for GET /availability (Assignment 2, Part A).

The limit is 5 calls per 10 seconds per client IP. This script:
  1. sends 7 calls back to back -> the first 5 are accepted, then 429s,
  2. reads the Retry-After header from the first 429,
  3. waits exactly that long and calls again -> accepted.

Uses only the Python standard library, so nothing needs installing.
Run from the repo root while the stack is up:
    python scripts/rate_limit_demo.py
"""
import json
import sys
import time
import urllib.error
import urllib.request

URL = "http://localhost:8000/availability?slot=09:00"


def call(n: int, show_body: bool = False) -> tuple[int, dict]:
    """Make one request and print one line describing the result."""
    try:
        with urllib.request.urlopen(URL, timeout=5) as resp:
            status, headers, body = resp.status, resp.headers, None
    except urllib.error.HTTPError as err:      # urllib raises for 4xx/5xx; we still want the response
        status, headers, body = err.code, err.headers, json.loads(err.read())
    except urllib.error.URLError as err:
        sys.exit(f"Could not reach {URL} ({err.reason}). Is the stack running? Try: docker compose up -d")

    line = (f"[{time.strftime('%H:%M:%S')}] call {n}: {status}"
            f"  X-RateLimit-Remaining={headers.get('X-RateLimit-Remaining')}")
    if status == 429:
        line += f"  Retry-After={headers.get('Retry-After')}s"
    print(line)
    if status == 429 and show_body:
        print(f"           body: {json.dumps(body)}")
    return status, headers


print(f"GET {URL}")
print("Limit: 5 calls per 10 seconds per client IP\n")

retry_after = None
for n in range(1, 8):
    status, headers = call(n, show_body=retry_after is None)
    if status == 429 and retry_after is None:
        retry_after = int(headers["Retry-After"])

if retry_after is not None:
    print(f"\nWaiting {retry_after}s, as Retry-After asked...\n")
    time.sleep(retry_after)
    call(8)
else:
    print("\nNo 429 seen - did something else already use the budget? Run the script again.")
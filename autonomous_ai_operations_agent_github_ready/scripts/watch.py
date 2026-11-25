import os, sys, time, httpx

base = os.getenv("OPS_API_URL", "http://localhost:8000")
key = sys.argv[1] if len(sys.argv) > 1 else os.getenv("OPS_API_KEY", "")
seen = set()
print("Watching live. Press Ctrl+C to stop.\n")
while True:
    try:
        r = httpx.get(f"{base}/api/logs", params={"limit": 50},
                      headers={"X-API-Key": key}, timeout=10)
        rows = r.json() if r.status_code == 200 else []
        for row in reversed(rows):
            if row["id"] not in seen:
                seen.add(row["id"])
                wid = (row["workflow_id"] or "-")[:8]
                print(f'{row["timestamp"][11:19]} {row["level"]:<7} {wid:<8} '
                      f'{row["agent"]:<16} {row["message"]}')
    except httpx.HTTPError as exc:
        print("API not reachable:", exc.__class__.__name__)
    time.sleep(1.5)
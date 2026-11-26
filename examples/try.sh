#!/usr/bin/env bash
# Try the verifier API. Start the service first:
#   docker compose up --build
# or locally:
#   uvicorn app.main:app --reload --port 8000
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== health =="
curl -sS "${BASE_URL}/health"
echo

echo "== 1) all 41 offsets should pass =="
curl -sS -X POST "${BASE_URL}/api/v1/verify" \
  -H 'Content-Type: application/json' \
  --data @"${DIR}/request_pass.json" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(json.dumps({k:d[k] for k in ("all_offsets_pass","offset_count","pass_count","miss_offsets_us")}, ensure_ascii=False, indent=2))'

echo "== 2) threshold: offsets -20..15 pass, 16..20 miss =="
curl -sS -X POST "${BASE_URL}/api/v1/verify" \
  -H 'Content-Type: application/json' \
  --data @"${DIR}/request_offset_threshold.json" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("pass:",d["pass_offsets_us"]); print("miss:",d["miss_offsets_us"]); print(json.dumps(d["results"][-1]["first_miss"], ensure_ascii=False, indent=2))'

echo "== 3) build-up: first miss is frame 3 / period 3 =="
curl -sS -X POST "${BASE_URL}/api/v1/verify" \
  -H 'Content-Type: application/json' \
  --data @"${DIR}/request_build_up_miss.json" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); m=d["results"][0]["first_miss"]; print(json.dumps({"frame_no":m["frame_no"],"period_no":m["period_no"],"deadline":m["deadline"],"hop1":m["hop1"],"hop2":m["hop2"],"late_by_us":m["late_by_us"]}, ensure_ascii=False, indent=2))'

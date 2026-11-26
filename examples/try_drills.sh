#!/usr/bin/env bash
# Try the reservation drill API. Start the service first:
#   docker compose up --build
# or locally:
#   uvicorn app.main:app --port 8000
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

py() { python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin), ensure_ascii=False, indent=2))'; }

echo "== 1) create a drill (config verification must pass) =="
DRILL_JSON="$(curl -sS -X POST "${BASE_URL}/api/v1/drills" \
  -H 'Content-Type: application/json' \
  --data @"${DIR}/drill_create.json")"
echo "${DRILL_JSON}" | py
DRILL_ID="$(echo "${DRILL_JSON}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["drill_id"])')"
echo "drill_id=${DRILL_ID}"

echo "== 2) reserve one diagnostic frame (earliest-delivery placement) =="
curl -sS -X POST "${BASE_URL}/api/v1/drills/${DRILL_ID}/reservations" \
  -H 'Content-Type: application/json' \
  --data @"${DIR}/drill_reservation.json" | py

echo "== 3) a second, identical request takes the next free intervals =="
curl -sS -X POST "${BASE_URL}/api/v1/drills/${DRILL_ID}/reservations" \
  -H 'Content-Type: application/json' \
  --data @"${DIR}/drill_reservation.json" | py

echo "== 4) an infeasible request returns available=false and occupies nothing =="
curl -sS -X POST "${BASE_URL}/api/v1/drills/${DRILL_ID}/reservations" \
  -H 'Content-Type: application/json' \
  --data @"${DIR}/drill_reservation_no_slot.json" | py

echo "== 5) drill snapshot: all reservations and per-hop occupancy =="
curl -sS "${BASE_URL}/api/v1/drills/${DRILL_ID}" | py

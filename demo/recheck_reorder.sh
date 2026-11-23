#!/usr/bin/env bash
# Demonstration: correcting inspection results re-orders the re-check queue.
#
# Start the service first:
#   docker compose up --build -d
# Then run against a *fresh* instance (the panel ids below must not be
# registered yet):
#   ./demo/recheck_reorder.sh
#
# Requires curl and jq.
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"

say() { printf '\n=== %s ===\n' "$1"; }
post() { curl -sS -X POST "$BASE_URL$1" -H 'Content-Type: application/json' -d "$2"; }
put()  { curl -sS -X PUT  "$BASE_URL$1" -H 'Content-Type: application/json' -d "$2"; }
get()  { curl -sS "$BASE_URL$1"; }

say "0. Before any inspection there is no calibration"
get /queue | jq -c . || true

say "1. Register pending boards plus calibration boards"
# Two pending boards we want ranked:
#   1001 score 10, huge loss
#   1002 score 90, small loss
# Inspected boards: score 10 has one clean result, score 90 one defect.
post /panels '{"panels":[
  {"panel_id": 1001, "score": 10, "loss": 1000000},
  {"panel_id": 1002, "score": 90, "loss": 9000},
  {"panel_id": 2001, "score": 10, "loss": 1, "inspection_result": 0},
  {"panel_id": 2002, "score": 90, "loss": 1, "inspection_result": 1}
]}' | jq -c .

say "2. Calibration: score 10 -> 0.0, score 90 -> 1.0"
get /calibration | jq -c '{calibration_version, rates}'

say "3. Initial queue: board 1002 leads (rate 1.0 x 9000)"
get '/queue?k=2' | jq -c '{calibration_version, items}'
FIRST_TOP=$(get '/queue?k=1' | jq -r '.items[0].panel_id')

say "4. Correct the score-10 inspection (2001) from clean to defect"
put /panels/2001/inspection '{"inspection_result": 1}' | jq -c .

say "5. Calibration after correction: score 10 -> 1.0 (single inspection), score 90 -> 1.0"
get /calibration | jq -c '{calibration_version, rates}'

say "6. Queue after correction: board 1001 now leads (1.0 x 1000000)"
get '/queue?k=2' | jq -c '{calibration_version, items}'
SECOND_TOP=$(get '/queue?k=1' | jq -r '.items[0].panel_id')

say "7. Invalid correction (result=7) is rejected and keeps the queue"
BEFORE=$(get '/queue?k=2')
HTTP=$(curl -sS -o /tmp/demo_invalid_body.json -w '%{http_code}' \
  -X PUT "$BASE_URL/panels/1002/inspection" \
  -H 'Content-Type: application/json' -d '{"inspection_result": 7}')
echo "HTTP $HTTP: $(jq -c . /tmp/demo_invalid_body.json)"
AFTER=$(get '/queue?k=2')
if [ "$BEFORE" = "$AFTER" ]; then
  echo "queue unchanged after invalid update: OK"
else
  echo "ERROR: queue changed after invalid update" >&2
  exit 1
fi

say "8. Unknown panel id is a 404 and also leaves the queue alone"
HTTP=$(curl -sS -o /tmp/demo_missing_body.json -w '%{http_code}' \
  -X PUT "$BASE_URL/panels/999999/inspection" \
  -H 'Content-Type: application/json' -d '{"inspection_result": 1}')
echo "HTTP $HTTP: $(jq -c . /tmp/demo_missing_body.json)"
[ "$(get '/queue?k=2')" = "$BEFORE" ] && echo "queue unchanged: OK"

say "9. Fill in a pending board (1001 -> inspected); it leaves the queue"
put /panels/1001/inspection '{"inspection_result": 0}' | jq -c .
get '/queue?k=2' | jq -c '{pending_panels, items}'

say "Summary"
echo "top board before correction: $FIRST_TOP"
echo "top board after  correction: $SECOND_TOP"
if [ "$FIRST_TOP" != "$SECOND_TOP" ]; then
  echo "reorder verified: OK"
else
  echo "ERROR: top board did not change" >&2
  exit 1
fi

#!/usr/bin/env bash
# Demonstration: the /audit endpoint returns the posterior probability that
# several defect-rate caps hold at the same time, plus calibration-version
# bookkeeping.
#
# Start the service first:
#   docker compose up --build -d
# Then run against a *fresh* instance (the panel ids below must not be
# registered yet):
#   ./demo/cap_audit.sh
#
# Requires curl and jq.
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"

say() { printf '\n=== %s ===\n' "$1"; }
post() { curl -sS -X POST "$BASE_URL$1" -H 'Content-Type: application/json' -d "$2"; }
put()  { curl -sS -X PUT  "$BASE_URL$1" -H 'Content-Type: application/json' -d "$2"; }
get()  { curl -sS "$BASE_URL$1"; }

say "0. Audit with no inspected panels -> 409 evidence_insufficient"
post /audit '{"checks":[{"score":10,"cap_percent":50}]}' | jq -c .

say "1. Register inspected boards at scores 10 and 90 (plus pending boards)"
post /panels '{"panels":[
  {"panel_id": 3001, "score": 10, "loss": 500},
  {"panel_id": 3002, "score": 90, "loss": 500},
  {"panel_id": 4001, "score": 10, "loss": 1, "inspection_result": 0},
  {"panel_id": 4002, "score": 10, "loss": 1, "inspection_result": 1},
  {"panel_id": 4003, "score": 90, "loss": 1, "inspection_result": 1},
  {"panel_id": 4004, "score": 90, "loss": 1, "inspection_result": 1}
]}' | jq -c .

say "2. Single cap: P(score-10 rate <= 50%)"
post /audit '{"checks":[{"score":10,"cap_percent":50}]}' | jq -c .

say "3. Multiple caps at once (the quality-report use case)"
# Three tiers in one request: inspected scores use their own component; the
# uninspected score 50 borrows the rate of score 10 via the left fallback.
post /audit '{"checks":[
  {"score": 10, "cap_percent": 50},
  {"score": 50, "cap_percent": 50},
  {"score": 90, "cap_percent": 100}
]}' | jq -c '{probability, calibration_version, recheck_required, resolved_checks}'

say "4. Tighten one tier; the joint probability moves"
post /audit '{"checks":[
  {"score": 10, "cap_percent": 20},
  {"score": 90, "cap_percent": 100}
]}' | jq -c '{probability, calibration_version}'

say "5. Pin the current calibration version, then correct an inspection"
VERSION=$(get /health | jq -r '.calibration_version')
put /panels/4001/inspection '{"inspection_result": 1}' | jq -c '{calibration_version}'
post /audit "$(jq -nc --argjson v "$VERSION" \
  '{checks:[{"score":10,"cap_percent":50}], calibration_version:$v}')" \
  | jq -c '{probability, calibration_version, recheck_required, warnings}'

say "6. Re-run without a pinned version: no warning, fresh probability"
post /audit '{"checks":[{"score":10,"cap_percent":50}]}' \
  | jq -c '{probability, calibration_version, recheck_required}'

say "7. Invalid requests are rejected (duplicate scores, out-of-range caps)"
for body in \
  '{"checks":[{"score":10,"cap_percent":50},{"score":10,"cap_percent":20}]}' \
  '{"checks":[{"score":256,"cap_percent":50}]}' \
  '{"checks":[{"score":10,"cap_percent":101}]}' \
  '{"checks":[]}'; do
  HTTP=$(curl -sS -o /tmp/audit_bad.json -w '%{http_code}' \
    -X POST "$BASE_URL/audit" -H 'Content-Type: application/json' -d "$body")
  echo "HTTP $HTTP for $body -> $(jq -c . /tmp/audit_bad.json)"
done

say "8. The re-check queue still updates with inspections as before"
get '/queue?k=2' | jq -c '{calibration_version, items}'

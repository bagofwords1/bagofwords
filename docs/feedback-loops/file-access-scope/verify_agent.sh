#!/usr/bin/env bash
# curl-only: a non-admin creates a report, uploads a file, and the agent reads it;
# another member tries to smuggle that file into his own chat via an @-mention.
set -u
S=${1:?state-dir}
B=${BASE:-http://localhost:8000}/api
ORG=$(cat $S/org); ALICE=$(cat $S/key_alice); BOB=$(cat $S/key_bob)
PASS=0; FAIL=0; TMP=$(mktemp -d)
req() { local k=$1; shift; CODE=$(curl -s -m 300 -o $TMP/body -w '%{http_code}' -H "Authorization: Bearer $k" -H "X-Organization-Id: $ORG" "$@"); BODY=$(cat $TMP/body); }
check() { if [ "$2" = "$3" ]; then PASS=$((PASS+1)); echo "  PASS  $1"; else FAIL=$((FAIL+1)); echo "  FAIL  $1 — expected '$2' got '$3' :: ${BODY:0:300}"; fi; }
json() { python3 -c "import sys,json;d=json.loads(sys.argv[1]);print(eval(sys.argv[2]))" "$BODY" "$1"; }
has_id() { python3 -c "import sys,json;d=json.loads(sys.argv[1]);print(any(f.get('id')==sys.argv[2] for f in d) if isinstance(d,list) else 'not-a-list')" "$BODY" "$1"; }
# The LLM key in this sandbox has no credit, so assert on what the agent is SENT:
# the backend logs every outbound Anthropic request body (LOG=backend log).
LOG=${LOG:?set LOG to the backend log file}
ctx_has() { # ctx_has <prompt marker> <needle> → True if every request for that prompt contains needle
  python3 - "$LOG" "$1" "$2" <<'PY'
import sys
log, marker, needle = sys.argv[1:]
reqs = [l for l in open(log, errors="ignore") if "Request options" in l and marker in l]
print("no-requests" if not reqs else all(needle in l for l in reqs))
PY
}
ctx_none() { # True if no request for that prompt contains needle
  python3 - "$LOG" "$1" "$2" <<'PY'
import sys
log, marker, needle = sys.argv[1:]
reqs = [l for l in open(log, errors="ignore") if "Request options" in l and marker in l]
print("no-requests" if not reqs else not any(needle in l for l in reqs))
PY
}

N=$((700000 + RANDOM))
T=tag$RANDOM$RANDOM   # prompt marker, deliberately unrelated to N
printf 'region,revenue\nnorth,%s\nsouth,1234\n' $N > $TMP/q3_sales.csv

echo "== alice (non-admin): report → upload → ask the agent"
req $ALICE -X POST "$B/reports" -H 'Content-Type: application/json' -d '{"title":"q3 sales via api"}'
check "create report" 200 $CODE; R=$(json "d['id']")
req $ALICE -X POST "$B/files" -F "file=@$TMP/q3_sales.csv" -F "report_id=$R"
check "upload file to report" 200 $CODE; F=$(json "d['id']"); echo "        file_id=$F"
req $ALICE -X POST "$B/reports/$R/completions" -H 'Content-Type: application/json' \
  -d "{\"prompt\":{\"content\":\"[$T] What is the revenue for the north region in the attached q3_sales.csv? Reply with just the number.\"}}"
check "completion returns" 200 $CODE
check "agent context carries the uploaded file (id + $N)" "True True" "$(ctx_has "[$T] What is the revenue" $N) $(ctx_has "[$T] What is the revenue" $F)"

echo "== alice: @-mention her own unattached file into a new chat"
printf 'region,revenue\nwest,%s\n' $((N+1)) > $TMP/west.csv
req $ALICE -X POST "$B/files" -F "file=@$TMP/west.csv"; F2=$(json "d['id']")
req $ALICE -X POST "$B/reports" -H 'Content-Type: application/json' -d '{"title":"mention own"}'; R2=$(json "d['id']")
req $ALICE -X POST "$B/reports/$R2/completions" -H 'Content-Type: application/json' \
  -d "{\"prompt\":{\"content\":\"[$T] What is the west revenue in @west.csv? Reply with just the number.\",\"mentions\":[{\"name\":\"FILES\",\"items\":[{\"id\":\"$F2\",\"filename\":\"west.csv\"}]}]}}"
check "completion with a mention of her own file" 200 $CODE
check "agent context carries the mentioned file ($((N+1)))" True $(ctx_has "[$T] What is the west revenue" $((N+1)))
req $ALICE "$B/reports/$R2/files"; check "mentioned file attached to her report" True $(has_id $F2)

echo "== bob: try to pull alice's file into his chat with a forged mention"
req $BOB -X POST "$B/reports" -H 'Content-Type: application/json' -d '{"title":"bob mention attack"}'; RB=$(json "d['id']")
req $BOB -X POST "$B/reports/$RB/completions" -H 'Content-Type: application/json' \
  -d "{\"prompt\":{\"content\":\"[$T] What is the north revenue in @q3_sales.csv? Reply with just the number, or say NOFILE if you have no such file.\",\"mentions\":[{\"name\":\"FILES\",\"items\":[{\"id\":\"$F\",\"filename\":\"q3_sales.csv\"}]}]}}"
check "completion returns" 200 $CODE
check "agent context has neither alice's data nor her file id" "True True" "$(ctx_none "[$T] What is the north revenue" $N) $(ctx_none "[$T] What is the north revenue" $F)"
req $BOB "$B/reports/$RB/files"; check "alice's file NOT attached to bob's report" False $(has_id $F)
req $BOB "$B/files/$F/content"; check "bob still cannot download it" 404 $CODE

rm -rf $TMP
echo; echo "RESULT: $PASS passed, $FAIL failed"; [ $FAIL -eq 0 ]

#!/usr/bin/env bash
# Loop B: one chat turn where the model writes a wrong visualization id.
#
#   run.sh <out_dir> <label> <insert|unknown|react>
#
#   insert   the model inserts one character into the real id (the reported slip)
#   unknown  the model writes an id that names nothing
#   react    the id is right, but the page lists runtime components in a
#            `const { … } = React` destructuring (they are undefined there)
#
# Needs the stack up (tools/agent/boot_stack.sh), the org seeded
# (tools/agent/seed_org.py --demo), setup.py run (writes <out_dir>/setup.json),
# and stub_llm.py running with STUB_VIZ_ID_FILE=<out_dir>/stub_viz_id and
# STUB_PAGE_FILE=<out_dir>/stub_page.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
OUT="$1"; LABEL="$2"; SLIP="$3"
API="${BOW_BASE_URL:-http://localhost:8000}"
DB_URL="${TEST_DATABASE_URL:-sqlite:///db/agent.db}"

read -r TOKEN ORG SOURCES < <(python3 -c "
import json,sys; d=json.load(open('$OUT/setup.json'))
print(d['token'], d['org_id'], json.dumps([s['id'] for s in d['data_sources']]).replace(' ',''))")

REPORT=$(curl -sf -X POST "$API/api/reports" -H "Authorization: Bearer $TOKEN" -H "X-Organization-Id: $ORG" \
  -H 'Content-Type: application/json' -d "{\"title\":\"Viz id: $LABEL\",\"files\":[],\"data_sources\":$SOURCES}" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
VIZ=$(cd "$ROOT/backend" && TESTING=true ENVIRONMENT=production TEST_DATABASE_URL="$DB_URL" uv run python "$HERE/seed_viz.py" "$REPORT" 2>/dev/null | tail -1)

case "$SLIP" in
  insert)  WRITTEN="${VIZ:0:13}a${VIZ:13}" ;;
  unknown) WRITTEN="$(python3 -c 'import uuid; print(uuid.uuid4())')" ;;
  react)   WRITTEN="$VIZ" ;;
  *) echo "unknown slip: $SLIP"; exit 2 ;;
esac
echo "$WRITTEN" > "$OUT/stub_viz_id"
echo "$SLIP" > "$OUT/stub_page"

(cd "$OUT" && node "$HERE/drive.mjs" "$OUT/setup.json" "$REPORT" "Build a dashboard of revenue by genre." "$OUT/$LABEL" > "$OUT/$LABEL.browser.json")

# What each create_artifact call returned (the stored summary), and what was
# saved (through the API: stored results and artifact content are encrypted at rest).
(cd "$ROOT/backend" && python3 - "$REPORT" "$VIZ" "$WRITTEN" "$API" "$TOKEN" "$ORG" <<'PY'
import json, sqlite3, sys, urllib.request
report, viz, written, api, token, org = sys.argv[1:7]
print(json.dumps({"report_id": report, "real_viz_id": viz, "id_the_model_wrote": written}))
db = sqlite3.connect("db/agent.db")
for status, summary in db.execute(
    "select te.status, te.result_summary from tool_executions te join agent_executions ae on ae.id = te.agent_execution_id "
    "where ae.report_id = ? and te.tool_name = 'create_artifact' order by te.created_at", (report,)):
    print(json.dumps({"create_artifact": status, "summary": summary}))
req = urllib.request.Request(f"{api}/api/artifacts/report/{report}",
                             headers={"Authorization": f"Bearer {token}", "X-Organization-Id": org})
for item in json.load(urllib.request.urlopen(req)):
    one = urllib.request.Request(f"{api}/api/artifacts/{item['id']}",
                                 headers={"Authorization": f"Bearer {token}", "X-Organization-Id": org})
    content = json.load(urllib.request.urlopen(one)).get("content") or {}
    print(json.dumps({"saved_artifact": item.get("title"), "version": item.get("version"),
                      "visualization_ids": content.get("visualization_ids"),
                      "code_binds_real_id": f'vizById("{viz}")' in (content.get("code") or "")}))
PY
) | tee "$OUT/$LABEL.result.jsonl"
cat "$OUT/$LABEL.browser.json" | tee -a "$OUT/$LABEL.result.jsonl"

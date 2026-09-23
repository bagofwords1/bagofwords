#!/usr/bin/env bash
# curl-only verification of per-user file access, driven with personal bow_ API keys.
# Usage: verify_files.sh <state-dir>   (state-dir written by seed.sh)
set -u
S=${1:?state-dir}
B=${BASE:-http://localhost:8000}/api
ORG=$(cat $S/org)
ALICE=$(cat $S/key_alice); BOB=$(cat $S/key_bob); CAROL=$(cat $S/key_carol); ADMIN=$(cat $S/key_admin)
PASS=0; FAIL=0
TMP=$(mktemp -d)

# req <key> <curl args...> → sets $CODE and $BODY
req() { local k=$1; shift; CODE=$(curl -s -o $TMP/body -w '%{http_code}' -H "Authorization: Bearer $k" -H "X-Organization-Id: $ORG" "$@"); BODY=$(cat $TMP/body); }
check() { # check <desc> <expected> <actual>
  if [ "$2" = "$3" ]; then PASS=$((PASS+1)); echo "  PASS  $1"; else FAIL=$((FAIL+1)); echo "  FAIL  $1 — expected '$2' got '$3' :: ${BODY:0:200}"; fi; }
json() { python3 -c "import sys,json;d=json.loads(sys.argv[1]);print(eval(sys.argv[2]))" "$BODY" "$1"; }
has_id() { python3 -c "import sys,json;d=json.loads(sys.argv[1]);print(any(f.get('id')==sys.argv[2] for f in d) if isinstance(d,list) else 'not-a-list')" "$BODY" "$1"; }
uid() { req $1 "$B/users/whoami"; json "d['id']"; }

RUN=$RANDOM
printf 'region,revenue\nnorth,%s\nsouth,200\n' $RUN > $TMP/sales.csv
printf 'secret-bob-%s\n' $RUN > $TMP/bob_private.txt
printf 'agent-kb-%s\n' $RUN > $TMP/agent_kb.txt

ALICE_ID=$(uid $ALICE); BOB_ID=$(uid $BOB); CAROL_ID=$(uid $CAROL)
[ -n "$BOB_ID" ] || { echo "could not resolve user ids"; exit 2; }

echo "== 1. Non-admin (alice) end-to-end via API key"
req $ALICE -X POST "$B/reports" -H 'Content-Type: application/json' -d "{\"title\":\"alice-report-$RUN\"}"
check "alice creates a report" 200 $CODE; A_R1=$(json "d['id']")
req $ALICE -X POST "$B/files" -F "file=@$TMP/sales.csv" -F "report_id=$A_R1"
check "alice uploads a file into her report" 200 $CODE; A_F1=$(json "d['id']")
req $ALICE "$B/reports/$A_R1/files"
check "alice lists her report's files" 200 $CODE; check "  ...and the upload is there" True $(has_id $A_F1)
req $ALICE "$B/files/$A_F1/content"
check "alice downloads her file" 200 $CODE; check "  ...bytes round-trip" "$(cat $TMP/sales.csv)" "$BODY"
req $ALICE "$B/files/$A_F1/embed_token"
check "alice mints an embed token" 200 $CODE
EMBED=$(json "d['url']")
CODE=$(curl -s -o /dev/null -w '%{http_code}' "${BASE:-http://localhost:8000}$EMBED"); check "  ...token serves the bytes without auth" 200 $CODE
req $ALICE "$B/files"
check "alice lists files" 200 $CODE; check "  ...sees her upload" True $(has_id $A_F1)
req $ALICE -X POST "$B/files" -F "file=@$TMP/sales.csv"
check "alice uploads an unattached file" 200 $CODE; A_F2=$(json "d['id']")
req $ALICE -X POST "$B/reports" -H 'Content-Type: application/json' -d "{\"title\":\"alice-report2-$RUN\",\"files\":[\"$A_F2\"]}"
check "alice creates a report with files=[her file]" 200 $CODE; A_R2=$(json "d['id']")
req $ALICE "$B/reports/$A_R2/files"; check "  ...file attached to the new report" True $(has_id $A_F2)
req $ALICE -X DELETE "$B/reports/$A_R2/files/$A_F2"
check "alice detaches a file from her report" 200 $CODE
req $ALICE "$B/reports/$A_R2/files"; check "  ...gone from the report" False $(has_id $A_F2)
req $ALICE -X POST "$B/projects" -H 'Content-Type: application/json' -d "{\"name\":\"alice-proj-$RUN\"}"
check "alice creates a project" 200 $CODE; A_P=$(json "d['id']")
req $ALICE -X PUT "$B/projects/$A_P/files" -H 'Content-Type: application/json' -d "{\"file_ids\":[\"$A_F2\"]}"
check "alice sets her file as a project default" 200 $CODE

echo "== 2. Another member (bob) cannot reach alice's files"
req $BOB "$B/files"; check "bob lists files" 200 $CODE
check "  ...alice's report upload not listed" False $(has_id $A_F1)
check "  ...alice's private-project file not listed" False $(has_id $A_F2)
req $BOB "$B/files/$A_F1/content"; check "bob downloads alice's file → 404" 404 $CODE
req $BOB "$B/files/$A_F1/embed_token"; check "bob mints embed token for alice's file → 404" 404 $CODE
req $BOB "$B/reports/$A_R1/files"; check "bob lists alice's private report files → 404" 404 $CODE
req $BOB -X DELETE "$B/reports/$A_R1/files/$A_F1"; check "bob detaches file from alice's report → 403" 403 $CODE
req $ALICE "$B/reports/$A_R1/files"; check "  ...alice's file still attached" True $(has_id $A_F1)
req $BOB -X POST "$B/files" -F "file=@$TMP/bob_private.txt" -F "report_id=$A_R1"
check "bob uploads into alice's report → 404" 404 $CODE
req $BOB -X POST "$B/files" -F "file=@$TMP/bob_private.txt" -F "report_id=00000000-0000-0000-0000-000000000000"
check "upload into a nonexistent report → 404" 404 $CODE
req $BOB -X POST "$B/reports" -H 'Content-Type: application/json' -d "{\"title\":\"bob-steal-$RUN\",\"files\":[\"$A_F1\"]}"
check "bob creates report with files=[alice's file]" 200 $CODE; B_R=$(json "d['id']")
req $BOB "$B/reports/$B_R/files"; check "  ...alice's file was NOT attached" False $(has_id $A_F1)
req $BOB "$B/files/$A_F1/content"; check "  ...and still not downloadable" 404 $CODE
req $BOB -X POST "$B/projects" -H 'Content-Type: application/json' -d "{\"name\":\"bob-proj-$RUN\"}"; B_P=$(json "d['id']")
req $BOB -X PUT "$B/projects/$B_P/files" -H 'Content-Type: application/json' -d "{\"file_ids\":[\"$A_F1\"]}"
check "bob sets alice's file as his project default → 404" 404 $CODE

echo "== 3. Sharing grants read access"
req $ALICE -X PUT "$B/reports/$A_R1/visibility/conversation" -H 'Content-Type: application/json' -d "{\"visibility\":\"shared\",\"shared_user_ids\":[\"$BOB_ID\"]}"
check "alice shares the conversation with bob" 200 $CODE
req $BOB "$B/reports/$A_R1/files"; check "bob lists the shared report's files" 200 $CODE; check "  ...sees the upload" True $(has_id $A_F1)
req $BOB "$B/files/$A_F1/content"; check "bob downloads a file from the shared conversation" 200 $CODE
req $BOB -X DELETE "$B/reports/$A_R1/files/$A_F1"; check "  ...but still cannot detach it → 403" 403 $CODE
req $CAROL "$B/files/$A_F1/content"; check "carol (not shared) still 404" 404 $CODE
req $ALICE -X PUT "$B/reports/$A_R1/visibility/conversation" -H 'Content-Type: application/json' -d '{"visibility":"none","shared_user_ids":[]}'
req $BOB "$B/files/$A_F1/content"; check "unsharing revokes bob's access → 404" 404 $CODE
req $ALICE -X PUT "$B/reports/$A_R1/visibility/artifact" -H 'Content-Type: application/json' -d '{"visibility":"internal"}'
check "alice publishes the dashboard org-internal" 200 $CODE
req $BOB "$B/files/$A_F1/content"; check "  ...chat upload (not in any artifact) stays private → 404" 404 $CODE
req $ALICE -X PUT "$B/projects/$A_P" -H 'Content-Type: application/json' -d '{"access":"org"}'
check "alice shares her project org-wide" 200 $CODE
req $BOB "$B/files"; check "bob now lists the project default file" True $(has_id $A_F2)
req $BOB "$B/files/$A_F2/content"; check "bob downloads the project default file" 200 $CODE

echo "== 4. Agent (data source) files follow agent access"
mkdir -p $TMP/kb
req $ADMIN -X POST "$B/data_sources" -H 'Content-Type: application/json' -d "{\"name\":\"kb-$RUN\",\"type\":\"network_dir\",\"config\":{\"root_path\":\"$TMP/kb\"},\"credentials\":{\"auth_type\":\"none\"},\"auth_policy\":\"system_only\",\"is_public\":false}"
check "admin creates a private agent" 200 $CODE; DS=$(json "d['id']")
req $ADMIN -X POST "$B/data_sources/$DS/files" -F "file=@$TMP/agent_kb.txt"
check "admin uploads to the agent library" 200 $CODE; DS_F=$(json "d['id']")
req $BOB "$B/files"; check "bob (no agent access) does not list the agent file" False $(has_id $DS_F)
req $BOB "$B/files/$DS_F/content"; check "bob cannot download it → 404" 404 $CODE
req $BOB -X POST "$B/files" -F "file=@$TMP/bob_private.txt" -F "data_source_id=$DS"
check "bob cannot upload into an agent he doesn't manage → 403" 403 $CODE
req $ADMIN -X POST "$B/data_sources/$DS/members" -H 'Content-Type: application/json' -d "{\"principal_type\":\"user\",\"principal_id\":\"$BOB_ID\"}"
check "admin grants bob access to the agent" 200 $CODE
req $BOB "$B/files"; check "bob now lists the agent file" True $(has_id $DS_F)
req $BOB "$B/files/$DS_F/content"; check "bob downloads the agent file" 200 $CODE; check "  ...bytes round-trip" "$(cat $TMP/agent_kb.txt)" "$BODY"
req $CAROL "$B/files/$DS_F/content"; check "carol (no access) still 404" 404 $CODE
req $BOB -X POST "$B/reports" -H 'Content-Type: application/json' -d "{\"title\":\"bob-agent-$RUN\",\"data_sources\":[\"$DS\"]}"
check "bob creates a report on the agent" 200 $CODE; B_R2=$(json "d['id']")
req $BOB "$B/reports/$B_R2/files"; check "  ...agent file snapshotted into bob's report" True $(has_id $DS_F)

echo "== 5. Admin sees everything"
req $ADMIN "$B/files"; check "admin lists alice's report upload" True $(has_id $A_F1)
req $ADMIN "$B/files/$A_F1/content"; check "admin downloads alice's file" 200 $CODE
req $ADMIN "$B/reports/$A_R1/files"; check "admin lists alice's report files" 200 $CODE

rm -rf $TMP
echo
echo "RESULT: $PASS passed, $FAIL failed"
[ $FAIL -eq 0 ]

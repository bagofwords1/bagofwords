#!/usr/bin/env bash
# Anonymous probes of the three public surfaces. Usage: probe.sh ids.json
IDS="$1"; B=http://localhost:8010/api
j() { python3 -c "import json,sys;d=json.load(open('$IDS'));print($1)"; }
IR=$(j "d['identity']['report_id']"); IQ=$(j "d['identity']['query_id']")
PR=$(j "d['plain']['report_id']"); PQ=$(j "d['plain']['query_id']")
TOK=$(j "d['conversation_token']")
echo "== identity report: public JSON step rows =="
curl -s "$B/r/$IR/queries/$IQ/step" | python3 -c "import sys,json;d=json.load(sys.stdin);print('rows',len((d.get('data') or {}).get('rows',[])),'withheld',d.get('snapshot_withheld'))"
echo "== identity report: export_html =="
curl -s -o /tmp/wp7_id.html -w "http %{http_code}\n" "$B/r/$IR/export_html"; echo "SECRET-ROW occurrences: $(grep -o 'SECRET-ROW-[0-9]' /tmp/wp7_id.html | wc -l)"
echo "== plain report: public JSON step rows / export_html =="
curl -s "$B/r/$PR/queries/$PQ/step" | python3 -c "import sys,json;d=json.load(sys.stdin);print('rows',len((d.get('data') or {}).get('rows',[])))"
curl -s -o /tmp/wp7_plain.html -w "http %{http_code}\n" "$B/r/$PR/export_html"; echo "PUBLIC-ROW occurrences: $(grep -o 'PUBLIC-ROW-[0-9]' /tmp/wp7_plain.html | wc -l)"
echo "== shared conversation /api/c/{token} =="
curl -s "$B/c/$TOK" | python3 -c "
import sys,json;d=json.load(sys.stdin);s=json.dumps(d)
print('raw email present:', 'jane.doe@acme-corp.com' in s, '| EMP-4821 present:', 'EMP-4821' in s, '| non-PII text present:', 'Q3 revenue' in s)
print('prompt:', [c['prompt'] for c in d['completions'] if c['role']=='user'][0]['content'])"

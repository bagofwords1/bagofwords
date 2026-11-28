#!/usr/bin/env bash
set -euo pipefail
D="sudo -n docker"; ROOT=/var/lib/bow-sandbox/egress
PRIV='"10.0.0.0/8","172.16.0.0/12","192.168.0.0/16","100.64.0.0/10","169.254.0.0/16","127.0.0.0/8","0.0.0.0/8","::1/128","fc00::/7","fe80::/10"'
adm() { sudo -n curl -sS --unix-socket $ROOT/admin/admin.sock -H 'Content-Type: application/json' "$@"; }
up() {
  sudo -n mkdir -p $ROOT/admin $ROOT/config $ROOT/etc; sudo -n cp ~/spike/egress-base.json $ROOT/etc/base.json
  $D run -d --name bow-sbx-egress --label bow.instance=spike --network bow-sbx --ip 10.213.77.3 --runtime runc --restart unless-stopped \
    --cap-drop ALL --cap-add NET_BIND_SERVICE --security-opt no-new-privileges \
    -v $ROOT/admin:/run/caddy-admin -v $ROOT/config:/config -v $ROOT/etc:/etc/caddy:ro \
    bow-spike-caddy-fp caddy run --resume --config /etc/caddy/base.json
}
# allow <id> <ip> <domain,domain,...>   (registries / allowlist)
allow() {
  local subj; subj=$(python3 -c 'import sys,json; print(json.dumps(sys.argv[1].split(",")))' "$3")
  adm -X DELETE http://caddy/id/eg-$1 >/dev/null 2>&1 || true
  adm -X PUT http://caddy/config/apps/http/servers/egress/routes/0 -d '{"@id":"eg-'$1'","match":[{"remote_ip":{"ranges":["'$2'"]}}],"handle":[{"handler":"forward_proxy","hide_ip":true,"hide_via":true,"acl":[{"subjects":['"$PRIV"']},{"subjects":'"$subj"',"allow":true},{"subjects":["all"]}]}]}'
}
# full <id> <ip>
full() {
  adm -X DELETE http://caddy/id/eg-$1 >/dev/null 2>&1 || true
  adm -X PUT http://caddy/config/apps/http/servers/egress/routes/0 -d '{"@id":"eg-'$1'","match":[{"remote_ip":{"ranges":["'$2'"]}}],"handle":[{"handler":"forward_proxy","hide_ip":true,"hide_via":true,"acl":[{"subjects":['"$PRIV"']},{"subjects":["all"],"allow":true}]}]}'
}
drop() { adm -X DELETE http://caddy/id/eg-$1; }
routes() { adm http://caddy/config/apps/http/servers/egress/routes | python3 -c 'import json,sys; print([r.get("@id") for r in json.load(sys.stdin)])'; }
logs() { $D logs --since "${1:-10m}" bow-sbx-egress 2>&1 | python3 -c '
import sys,json
for l in sys.stdin:
    try: j=json.loads(l)
    except Exception: continue
    if j.get("logger")!="http.log.access.egress": continue
    r=j["request"]; print(r["remote_ip"], r["method"], r["host"], j["status"])'; }
"$@"

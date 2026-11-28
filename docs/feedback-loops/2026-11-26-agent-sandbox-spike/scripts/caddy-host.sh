#!/usr/bin/env bash
set -euo pipefail
D="sudo -n docker"; ROOT=/var/lib/bow-sandbox/caddy
adm() { sudo -n curl -sS --unix-socket $ROOT/admin/admin.sock -H 'Content-Type: application/json' "$@"; }
up() {
  sudo -n mkdir -p $ROOT/admin $ROOT/data $ROOT/config $ROOT/etc
  sudo -n cp ~/spike/caddy-base.json $ROOT/etc/base.json
  $D run -d --name bow-sbx-caddy --label bow.instance=spike --network bow-sbx --ip 10.213.77.2 --runtime runc \
    --restart unless-stopped -p 8443:8443 \
    -v $ROOT/admin:/run/caddy-admin -v $ROOT/data:/data -v $ROOT/config:/config -v $ROOT/etc:/etc/caddy:ro \
    bow-spike-caddy-fp caddy run --resume --config /etc/caddy/base.json
}
# route <id> <ip>
route() {
  adm -X PUT http://caddy/config/apps/http/servers/sbx/routes/0 -d '{"@id":"sbx-'$1'","match":[{"header":{"X-Sandbox-Id":["'$1'"]}}],"handle":[{"handler":"subroute","routes":[
   {"match":[{"header":{"Content-Type":["application/grpc*"]}}],"handle":[{"handler":"reverse_proxy","flush_interval":-1,"transport":{"protocol":"http","versions":["h2c","2"]},"upstreams":[{"dial":"'$2':9090"}]}]},
   {"handle":[{"handler":"reverse_proxy","flush_interval":-1,"upstreams":[{"dial":"'$2':8080"}]}]}]}]}'
}
unroute() { adm -X DELETE http://caddy/id/sbx-$1; }
routes() { adm http://caddy/config/apps/http/servers/sbx/routes | python3 -c 'import json,sys; print([r.get("@id") for r in json.load(sys.stdin)])'; }
ca() { adm http://caddy/pki/ca/local | python3 -c 'import json,sys; print(json.load(sys.stdin)["root_certificate"],end="")'; }
"$@"

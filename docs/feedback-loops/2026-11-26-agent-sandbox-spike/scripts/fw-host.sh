#!/usr/bin/env bash
set -euo pipefail
D="sudo -n docker"; ROOT=/var/lib/bow-sandbox/firewall
# conf <bow_address|-> <full ips...>
conf() { local bow=$1; shift; [ "$bow" = "-" ] && bow=""; sudo -n mkdir -p $ROOT; printf 'BRIDGE=bow-sbx0\nSUBNET=10.213.77.0/24\nCADDY_IP=10.213.77.2\nEGRESS_IP=10.213.77.3\nCADDY_PORT=8443\nSANDBOX_PORTS=8080,9090\nBOW_ADDRESS="%s"\nFULL_IPS="%s"\n' "$bow" "$*" | sudo -n tee $ROOT/firewall.env >/dev/null; $D exec bow-sbx-firewall /bin/sh /apply.sh once >/dev/null 2>&1 || true; }
up() { $D rm -f bow-sbx-firewall >/dev/null 2>&1 || true; $D run -d --name bow-sbx-firewall --label bow.instance=spike --network host --runtime runc --cap-drop ALL --cap-add NET_ADMIN --cap-add NET_RAW --restart always -v $ROOT:/etc/bow-sbx:ro -e INTERVAL=30 bow-spike-fw; }
"$@"

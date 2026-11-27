#!/bin/sh
# bow-sbx-firewall: BoW's rules for the sandbox bridge.
#
#   apply.sh           assert the rules now and every $INTERVAL seconds (default 5)
#   apply.sh once      assert the rules once (BoW runs this with `docker exec` after a change)
#   apply.sh remove    delete BoW's chains and jumps (when a host is cleaned up)
#
# Configuration is read from /etc/bow-sbx/firewall.env on every pass:
#   BRIDGE         the sandbox bridge interface
#   CADDY_IP       the TLS router's address on the bridge
#   EGRESS_IP      the egress proxy's address on the bridge
#   CADDY_PORT     the router's port inside its container
#   SANDBOX_PORTS  ports the router may reach in a sandbox (comma separated)
#   BOW_ADDRESS    optional: the only source allowed to reach the router
#   FULL_IPS       sandboxes on the "full" policy (space separated)
#
# Sandbox-to-sandbox traffic is already dropped by Docker itself (the network
# is created with inter-container communication off, which also loads
# br_netfilter). These chains allow the two exceptions (router to sandbox,
# sandbox to proxy) and block everything else a sandbox could reach.
CONF=/etc/bow-sbx/firewall.env
PRIVATE="10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 169.254.0.0/16 127.0.0.0/8 224.0.0.0/4"

pick() {
  for b in iptables-nft iptables-legacy; do
    if $b -w -S DOCKER-USER >/dev/null 2>&1; then echo $b; return; fi
  done
}

remove() {
  for IPT in iptables-nft iptables-legacy; do
    $IPT -w -S DOCKER-USER >/dev/null 2>&1 || continue
    . $CONF 2>/dev/null
    $IPT -w -D DOCKER-USER -j BOW-SBX-FWD 2>/dev/null
    [ -n "$BRIDGE" ] && $IPT -w -D INPUT -i "$BRIDGE" -j BOW-SBX-IN 2>/dev/null
    for chain in BOW-SBX-FWD BOW-SBX-IN; do
      $IPT -w -F $chain 2>/dev/null; $IPT -w -X $chain 2>/dev/null
    done
  done
  echo "$(date -u +%FT%TZ) removed"
}

apply() {
  [ -r $CONF ] || { echo "missing $CONF"; return 1; }
  . $CONF
  IPT=$(pick)
  [ -n "$IPT" ] || { echo "unsupported: no iptables variant has the DOCKER-USER chain (Docker must manage iptables)"; return 1; }
  B=$BRIDGE
  {
    echo "*filter"; echo ":BOW-SBX-FWD - [0:0]"; echo ":BOW-SBX-IN - [0:0]"
    # replies to connections already open
    echo "-A BOW-SBX-FWD -i $B -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT"
    echo "-A BOW-SBX-FWD -o $B -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT"
    # router to a sandbox, sandbox to the proxy; everything else on the bridge is dropped
    echo "-A BOW-SBX-FWD -i $B -o $B -s $CADDY_IP -p tcp -m multiport --dports ${SANDBOX_PORTS:-8080,9090} -j ACCEPT"
    echo "-A BOW-SBX-FWD -i $B -o $B -d $EGRESS_IP -p tcp --dport 3128 -j ACCEPT"
    echo "-A BOW-SBX-FWD -i $B -o $B -j DROP"
    # private ranges, link-local (cloud metadata), loopback, multicast, outbound mail
    for net in $PRIVATE; do echo "-A BOW-SBX-FWD -i $B -d $net -j DROP"; done
    echo "-A BOW-SBX-FWD -i $B -p tcp --dport 25 -j DROP"
    # the proxy reaches the internet; sandboxes on the "full" policy do too
    echo "-A BOW-SBX-FWD -i $B -s $EGRESS_IP -j ACCEPT"
    for ip in $FULL_IPS; do echo "-A BOW-SBX-FWD -i $B -s $ip -j ACCEPT"; done
    echo "-A BOW-SBX-FWD -i $B -j DROP"
    # inbound: only the router's port, optionally only from the BoW server
    if [ -n "$BOW_ADDRESS" ]; then
      echo "-A BOW-SBX-FWD -o $B -d $CADDY_IP -p tcp --dport ${CADDY_PORT:-8443} -s $BOW_ADDRESS -j ACCEPT"
    else
      echo "-A BOW-SBX-FWD -o $B -d $CADDY_IP -p tcp --dport ${CADDY_PORT:-8443} -j ACCEPT"
    fi
    echo "-A BOW-SBX-FWD -o $B -j DROP"
    # a sandbox to the host itself
    echo "-A BOW-SBX-IN -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT"
    echo "-A BOW-SBX-IN -j DROP"
    echo "COMMIT"
  } > /tmp/rules
  ${IPT}-restore -w --noflush < /tmp/rules || return 1
  # Our jumps stay first, whatever another firewall tool inserted since.
  [ "$($IPT -w -S DOCKER-USER | sed -n 2p)" = "-A DOCKER-USER -j BOW-SBX-FWD" ] || {
    $IPT -w -D DOCKER-USER -j BOW-SBX-FWD 2>/dev/null; $IPT -w -I DOCKER-USER 1 -j BOW-SBX-FWD; }
  [ "$($IPT -w -S INPUT | sed -n 2p)" = "-A INPUT -i $B -j BOW-SBX-IN" ] || {
    $IPT -w -D INPUT -i $B -j BOW-SBX-IN 2>/dev/null; $IPT -w -I INPUT 1 -i $B -j BOW-SBX-IN; }
  BRNF=$(cat /proc/sys/net/bridge/bridge-nf-call-iptables 2>/dev/null || echo missing)
  echo "$(date -u +%FT%TZ) applied backend=$IPT full=[$FULL_IPS] bow=[$BOW_ADDRESS] bridge-nf=$BRNF"
}

case "$1" in
  once) apply; exit $? ;;
  remove) remove; exit 0 ;;
esac
while true; do
  apply || echo "$(date -u +%FT%TZ) apply failed"
  sleep "${INTERVAL:-5}"
done

#!/bin/sh
# bow-sbx-firewall: asserts BoW's rules for the sandbox bridge, forever.
# Reads /etc/bow-sbx/firewall.env: BRIDGE SUBNET CADDY_IP EGRESS_IP CADDY_PORT BOW_ADDRESS FULL_IPS SANDBOX_PORTS
CONF=/etc/bow-sbx/firewall.env
pick() { for b in iptables-nft iptables-legacy; do if $b -w -S DOCKER-USER >/dev/null 2>&1; then echo $b; return; fi; done; }
apply() {
  . $CONF
  IPT=$(pick); [ -n "$IPT" ] || { echo "no iptables backend has DOCKER-USER (is Docker managing iptables?)"; return 1; }
  B=$BRIDGE
  {
    echo "*filter"; echo ":BOW-SBX-FWD - [0:0]"; echo ":BOW-SBX-IN - [0:0]"
    echo "-A BOW-SBX-FWD -i $B -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT"
    echo "-A BOW-SBX-FWD -o $B -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT"
    echo "-A BOW-SBX-FWD -i $B -o $B -s $CADDY_IP -p tcp -m multiport --dports ${SANDBOX_PORTS:-8080,9090} -j ACCEPT"
    echo "-A BOW-SBX-FWD -i $B -o $B -d $EGRESS_IP -p tcp --dport 3128 -j ACCEPT"
    echo "-A BOW-SBX-FWD -i $B -o $B -j DROP"
    for net in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 169.254.0.0/16 127.0.0.0/8 224.0.0.0/4; do echo "-A BOW-SBX-FWD -i $B -d $net -j DROP"; done
    echo "-A BOW-SBX-FWD -i $B -p tcp --dport 25 -j DROP"
    echo "-A BOW-SBX-FWD -i $B -s $EGRESS_IP -j ACCEPT"
    for ip in $FULL_IPS; do echo "-A BOW-SBX-FWD -i $B -s $ip -j ACCEPT"; done
    echo "-A BOW-SBX-FWD -i $B -j DROP"
    if [ -n "$BOW_ADDRESS" ]; then echo "-A BOW-SBX-FWD -o $B -d $CADDY_IP -p tcp --dport ${CADDY_PORT:-8443} -s $BOW_ADDRESS -j ACCEPT"
    else echo "-A BOW-SBX-FWD -o $B -d $CADDY_IP -p tcp --dport ${CADDY_PORT:-8443} -j ACCEPT"; fi
    echo "-A BOW-SBX-FWD -o $B -j DROP"
    echo "-A BOW-SBX-IN -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT"
    echo "-A BOW-SBX-IN -j DROP"
    echo "COMMIT"
  } > /tmp/rules
  ${IPT}-restore -w --noflush < /tmp/rules || return 1
  $IPT -w -C DOCKER-USER -j BOW-SBX-FWD 2>/dev/null || $IPT -w -I DOCKER-USER 1 -j BOW-SBX-FWD
  $IPT -w -C INPUT -i $B -j BOW-SBX-IN 2>/dev/null || $IPT -w -I INPUT 1 -i $B -j BOW-SBX-IN
  # keep our jumps first
  [ "$($IPT -w -S INPUT | sed -n 2p)" = "-A INPUT -i $B -j BOW-SBX-IN" ] || { $IPT -w -D INPUT -i $B -j BOW-SBX-IN; $IPT -w -I INPUT 1 -i $B -j BOW-SBX-IN; }
  [ "$($IPT -w -S DOCKER-USER | sed -n 2p)" = "-A DOCKER-USER -j BOW-SBX-FWD" ] || { $IPT -w -D DOCKER-USER -j BOW-SBX-FWD; $IPT -w -I DOCKER-USER 1 -j BOW-SBX-FWD; }
  echo "$(date -u +%FT%TZ) applied backend=$IPT full=[$FULL_IPS] bow=[$BOW_ADDRESS] brnf=$(cat /proc/sys/net/bridge/bridge-nf-call-iptables 2>/dev/null || echo missing)"
}
if [ "$1" = "once" ]; then apply; exit $?; fi
trap 'apply' HUP
while true; do apply || echo "apply failed"; sleep ${INTERVAL:-30} & wait $!; done

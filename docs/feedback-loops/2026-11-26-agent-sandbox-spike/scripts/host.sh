#!/usr/bin/env bash
# Spike helper, runs ON the sandbox host. Every docker call goes through sudo -n.
set -euo pipefail
D="sudo -n docker"
NET=bow-sbx; SUBNET=10.213.77.0/24; GW=10.213.77.1; CADDY_IP=10.213.77.2; EGRESS_IP=10.213.77.3
ROOT=/var/lib/bow-sandbox

net() { $D network inspect $NET >/dev/null 2>&1 || $D network create --driver bridge --subnet $SUBNET --gateway $GW -o com.docker.network.bridge.name=bow-sbx0 -o com.docker.network.bridge.enable_icc=false --label bow.instance=spike $NET; }

# create <id> <ip> <image> <runtime> <token_sha> [mem] [disk_mb] [extra docker args...]
wsmount() { local id=$1; mountpoint -q $ROOT/ws/$id || sudo -n mount -o loop,nosuid,nodev $ROOT/ws/$id.img $ROOT/ws/$id; }
# create <id> <ip> <image> <runtime> <token_sha> [mem] [disk_mb] [extra docker args...]
create() {
  local id=$1 ip=$2 image=$3 rt=$4 sha=$5 mem=${6:-1g} disk=${7:-1024}; shift $(( $# < 7 ? $# : 7 ))
  sudo -n mkdir -p $ROOT/ws/$id
  local img=$ROOT/ws/$id.img
  if [ ! -f $img ]; then
    sudo -n truncate -s ${disk}M $img
    sudo -n mkfs.ext4 -q -F -m 0 -E root_owner=1000:1000 $img
  fi
  wsmount $id
  $D run -d --name bow-sbx-$id --hostname sandbox --label bow.sandbox=$id --label bow.instance=spike \
    --network $NET --ip $ip --runtime $rt --user 1000:1000 \
    --cap-drop ALL --security-opt no-new-privileges --pids-limit 512 --cpus 1 --memory $mem --memory-swap $mem \
    --read-only --tmpfs /tmp:rw,exec,size=256m,mode=1777 \
    -e HOME=/workspace/.home -e SANDBOX_ID=$id \
    -v $ROOT/ws/$id:/workspace -v $ROOT/bin/sandboxd:/usr/local/bin/sandboxd:ro \
    -w /workspace --entrypoint /usr/local/bin/sandboxd "$@" $image \
    --grpc-port 9090 --rest-port 8080 --root-dir /workspace --auth-token-sha256 $sha
}
stop() { $D stop -t 5 bow-sbx-$1 >/dev/null; sudo -n umount $ROOT/ws/$1; }
start() { wsmount $1; $D start bow-sbx-$1 >/dev/null; }
destroy() { local id=$1; $D rm -f bow-sbx-$id >/dev/null 2>&1 || true; sudo -n umount $ROOT/ws/$id 2>/dev/null || true; sudo -n rm -rf $ROOT/ws/$id.img $ROOT/ws/$id; }
"$@"

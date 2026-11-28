# bagofwords/sandbox-firewall: asserts BoW's packet-filter rules for the
# sandbox network on a sandbox host, forever.
#
# Runs with --network host --cap-add NET_ADMIN --cap-add NET_RAW and
# --restart always. It ships both iptables variants and uses the one in which
# Docker's DOCKER-USER chain exists, so it does not depend on the host's
# distribution or on ufw / firewalld.
FROM alpine:3.22
RUN apk add --no-cache iptables iptables-legacy
COPY apply.sh /apply.sh
ENTRYPOINT ["/bin/sh", "/apply.sh"]
LABEL org.opencontainers.image.title="bagofwords sandbox firewall helper" \
      org.opencontainers.image.source="https://github.com/bagofwords1/bagofwords"

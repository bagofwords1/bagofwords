FROM alpine:3.22
RUN apk add --no-cache iptables iptables-legacy
COPY apply.sh /apply.sh
ENTRYPOINT ["/bin/sh", "/apply.sh"]

# bagofwords/sandbox-caddy: Caddy with the forward-proxy plugin.
#
# One image, two roles on every sandbox host (see docs/design/agent-sandboxes.md):
#   bow-sbx-caddy   TLS router in front of the sandboxes (one published port)
#   bow-sbx-egress  egress proxy that decides what each sandbox may reach
#
# Both versions are pinned; they are the ones the phase 0 spike ran
# (docs/feedback-loops/2026-11-26-agent-sandbox-spike/).
ARG CADDY_VERSION=2.11.9
FROM caddy:${CADDY_VERSION}-builder AS build
ARG FORWARDPROXY_VERSION=v0.0.0-20260321230143-0aab84dad4fc
RUN xcaddy build --with github.com/caddyserver/forwardproxy@${FORWARDPROXY_VERSION}

FROM caddy:${CADDY_VERSION}
COPY --from=build /usr/bin/caddy /usr/bin/caddy
# The binary carries a file capability, so containers that drop all
# capabilities must add NET_BIND_SERVICE back or it cannot start.
LABEL org.opencontainers.image.title="bagofwords sandbox caddy" \
      org.opencontainers.image.source="https://github.com/bagofwords1/bagofwords"

FROM caddy:2.11-builder AS b
RUN xcaddy build --with github.com/caddyserver/forwardproxy
FROM caddy:2.11
COPY --from=b /usr/bin/caddy /usr/bin/caddy

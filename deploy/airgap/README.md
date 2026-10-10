# Bag of Words air-gap deployment

This bundle contains the Bag of Words, PostgreSQL, and Caddy container images
for one CPU architecture. The host needs Docker Engine with Docker Compose v2;
it does not need registry or package-manager access.

## Install

```bash
(cd .. && sha256sum -c bagofwords-airgap-*.tar.gz.sha256)
docker load -i images.tar
cp .env.example .env
```

Edit `.env` and replace the PostgreSQL password and encryption key. Set
`DOMAIN` and `BOW_BASE_URL` to the address users will open, then start the
services without contacting a registry:

```bash
docker compose config
docker compose up -d --pull never
docker compose ps
```

Open `http://<DOMAIN>`. Check service output with `docker compose logs`.

## TLS

The included Caddyfile serves HTTP because public ACME certificate issuance is
not available in a fully disconnected network. It is simplest to terminate TLS
at an existing internal load balancer. To terminate TLS in Caddy instead, mount
the customer's certificate and private key into the Caddy service and replace
the site address in `Caddyfile` with:

```caddyfile
{$DOMAIN} {
    tls /certs/server.crt /certs/server.key
    reverse_proxy app:3000
}
```

Expose port 443 and mount the certificate directory read-only in
`docker-compose.yaml` before starting the deployment.

## Scan

The archive checksum verifies the download. Security tools that scan a Docker
daemon can scan the three loaded image tags shown by `docker images`. Tools that
accept Docker archives can inspect `images.tar` directly.

## Source code

Each release folder also contains `bagofwords-source-<version>.tar.gz`, the
tracked source tree of the commit the Bag of Words image was built from. It is
the same for every CPU architecture. The commit hash is in `SOURCE_COMMIT` at
the top of the extracted folder.

```bash
sha256sum -c bagofwords-source-*.tar.gz.sha256
tar -xzf bagofwords-source-*.tar.gz
cat bagofwords-source-*/SOURCE_COMMIT
```

The archive holds source only; it does not include Python or Node.js
dependencies.

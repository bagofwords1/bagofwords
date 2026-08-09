# Feedback Loop — publish a complete Docker Compose air-gap bundle

Customers need one downloadable archive that they can scan and deploy without
access to Docker Hub. The release must provide both x86-64 and ARM64 variants,
each containing the Bag of Words, PostgreSQL, and Caddy images plus the files
needed by Docker Compose.

## Root cause (validated)

The repository previously had no air-gap deployment directory or publishing
workflow. The production Compose file also used `latest` with an always-pull
policy, so copying it into a disconnected environment would still attempt a
registry request. The dedicated Compose contract now pins the release through
`BOW_VERSION` and disables pulls for every service at
`deploy/airgap/docker-compose.yaml:1-56`. Packaging and S3 publication live at
`.github/workflows/airgap-bundle.yml:45-120`, and run only after the multi-arch
application manifest is published via `.github/workflows/docker-image.yml:173-180`.

## Loop A — deterministic reproduction (no external services)

Run the static bundle contract from the repository root:

```bash
bash tools/agent/check_airgap_bundle.sh
```

Before the feature was added, the observed result was:

```text
FAIL: missing deploy/airgap/docker-compose.yaml
```

The check is self-contained. It verifies the required deployment files, rejects
`latest`, requires `pull_policy: never` on all services, requires both target
architectures, and confirms image export, checksum, S3 upload, and release
workflow integration. It uses POSIX runner tooling (`grep`) rather than assuming
developer utilities such as ripgrep are installed by `ubuntu-latest`.

## The fix

- `deploy/airgap/` supplies Compose, Caddy, application configuration, an
  environment template, and customer instructions. Outbound telemetry and
  Intercom are disabled, while Caddy defaults to HTTP so it does not attempt
  public ACME issuance.
- `.github/workflows/airgap-bundle.yml` pulls and saves the exact release plus
  PostgreSQL and Caddy for `linux/amd64` and `linux/arm64`, creates one archive
  and checksum per architecture, and uploads them to the versioned S3 prefix.
- `.github/workflows/docker-image.yml` publishes a plain version image tag and
  invokes the reusable bundle workflow after the multi-architecture manifest
  exists. The invocation is skipped until the required S3 repository variables
  are configured, so existing releases do not fail during AWS setup. Uploads
  authenticate with the `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`
  repository secrets; the public bucket policy is responsible for anonymous
  read access, so the workflow does not depend on ACLs or an OIDC role.

Re-running Loop A produces:

```text
PASS: air-gap deployment bundle contract is complete
```

Render the Compose model without starting services:

```bash
BOW_VERSION=0.0.531 \
POSTGRES_PASSWORD=test-password \
BOW_ENCRYPTION_KEY=test-key \
docker compose -f deploy/airgap/docker-compose.yaml config
```

The rendered output contains all three expected images and
`pull_policy: never` for each service.

## What this proves / regression notes

The deterministic loop proves that the repository continues to describe both
architectures and cannot regress to registry pulls or a `latest` application
tag. The actual image pulls and S3 upload remain integration boundaries and run
in GitHub Actions with Docker Hub credentials and static AWS credentials kept
in repository secrets. Caddy runtime validation requires its container image;
its configuration is included in the same release job that pulls that image.

#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

required_files=(
  deploy/airgap/docker-compose.yaml
  deploy/airgap/Caddyfile
  deploy/airgap/bow-config.yaml
  deploy/airgap/.env.example
  deploy/airgap/README.md
  .github/workflows/airgap-bundle.yml
)

for file in "${required_files[@]}"; do
  if [[ ! -f "$file" ]]; then
    echo "FAIL: missing $file" >&2
    exit 1
  fi
done

compose=deploy/airgap/docker-compose.yaml
workflow=.github/workflows/airgap-bundle.yml
image_workflow=.github/workflows/docker-image.yml

if rg -n '(^|:)latest([[:space:]]|$)' "$compose"; then
  echo "FAIL: the air-gap Compose file must not use latest tags" >&2
  exit 1
fi

[[ "$(rg -c 'pull_policy:[[:space:]]*never' "$compose")" -eq 3 ]] || {
  echo "FAIL: every service must set pull_policy: never" >&2
  exit 1
}

rg -q 'linux/amd64' "$workflow" || { echo "FAIL: amd64 bundle is missing" >&2; exit 1; }
rg -q 'linux/arm64' "$workflow" || { echo "FAIL: arm64 bundle is missing" >&2; exit 1; }
rg -q 'docker save' "$workflow" || { echo "FAIL: workflow does not export images" >&2; exit 1; }
rg -q 'sha256sum' "$workflow" || { echo "FAIL: workflow does not create a checksum" >&2; exit 1; }
rg -q 'aws s3 cp' "$workflow" || { echo "FAIL: workflow does not upload to S3" >&2; exit 1; }
rg -q 'aws-access-key-id:[[:space:]]*\$\{\{ secrets\.AWS_ACCESS_KEY_ID \}\}' "$workflow" || {
  echo "FAIL: workflow does not use the configured AWS access key" >&2
  exit 1
}
rg -q 'aws-secret-access-key:[[:space:]]*\$\{\{ secrets\.AWS_SECRET_ACCESS_KEY \}\}' "$workflow" || {
  echo "FAIL: workflow does not use the configured AWS secret key" >&2
  exit 1
}
if rg -q 'role-to-assume|AIRGAP_AWS_ROLE_ARN|id-token:[[:space:]]*write' "$workflow"; then
  echo "FAIL: air-gap publishing must use static AWS credentials, not OIDC" >&2
  exit 1
fi
rg -q 'uses:[[:space:]]*\./\.github/workflows/airgap-bundle\.yml' "$image_workflow" || {
  echo "FAIL: image publishing does not invoke the air-gap workflow" >&2
  exit 1
}

echo "PASS: air-gap deployment bundle contract is complete"

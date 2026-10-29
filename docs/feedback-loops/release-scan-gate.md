# Feedback Loop — failed scans must block release publication

Versioned Docker tags and GitHub Releases could publish despite a failed security scan. This loop checks the release dependency contract without Docker Hub or AWS credentials.

## Root cause (validated)

At baseline 105a48a38, `.github/workflows/docker-image.yml` made the Trivy gate continue on error and used a merge condition that checked only build success. The shell restricted only `latest`, with a manual override. Only amd64 was scanned. `.github/workflows/release.yml` independently accepted the same dispatch event. The standalone airgap workflow uploaded without scanning its bundled images.

## Loop A — deterministic reproduction (no external services)

```sh
python3 -m venv /tmp/bow-gate-test
/tmp/bow-gate-test/bin/pip install PyYAML==6.0.3
/tmp/bow-gate-test/bin/python tools/agent/tests/test_release_scan_gate.py
```

The initial four checks against the baseline returned two failures and two errors: missing architecture matrix, missing airgap gate, and permissive merge condition. After the fix and adding GitHub Release coverage: five checks pass. The matrix check exercises 16 success/failure/cancelled/skipped combinations with publication replaced by an in-memory sink.

## The fix

Both image digests must pass High/Critical scanning, including unfixed vulnerabilities unless covered by a reviewed `.trivyignore` exception. No force flag bypasses the gate. Default GitHub success dependency semantics block all release tags after any scan failure or error. GitHub Releases are callable only after the gated merge. Both airgap architectures scan separate BOW, PostgreSQL, and Caddy archives before uploading to S3, including manual dispatches.

`Release gate regression` runs the credential-free checks on relevant PRs. Workflow syntax was also validated using actionlint with shellcheck disabled.

## What this proves / regression notes

This validates YAML gate configuration and a model of GitHub dependency semantics. It does not execute a real Trivy scan or a real Docker/S3 publication. A failed live scan rehearsal and a passing release remain operational evidence to collect after merge. Build jobs still upload untagged digests and registry cache before scanning; those are staging objects, not gated customer release tags. Development branch images are outside this release change.

The main ruleset currently prevents deletion/non-fast-forward updates but has no required PR or status checks. Configure protections separately after confirming the desired checks and permitted bypass actors. This change intentionally makes releases fail on unfixed High/Critical findings; no exception file exists at baseline.

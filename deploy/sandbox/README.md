# Agent sandbox images

Images BoW uses on a sandbox host (see `docs/design/agent-sandboxes.md`).
They are meant to be built and published with each BoW release, tagged with
the BoW version, by the workflow in `sandbox-images.workflow.yml`.

**That workflow is not active yet.** It sits here because the credentials it
was written with could not add files under `.github/workflows/`. To turn it
on, move it there:

```bash
git mv deploy/sandbox/sandbox-images.workflow.yml .github/workflows/sandbox-images.yml
```

It uses the same trigger and the same Docker Hub secrets as
`docker-image.yml`. Until it has run for a release, the default image names
below do not exist in a registry: set the `BOW_SANDBOX_*_IMAGE` variables or
preload the images (both described further down).

| Directory | Image | What it is |
|---|---|---|
| `image/` | `bagofwords/sandbox` | The image of the built-in "Default" environment: Python and common data packages |
| `caddy/` | `bagofwords/sandbox-caddy` | Caddy with the forward-proxy plugin. One image, two roles on a host: the TLS router in front of the sandboxes, and the egress proxy |
| `firewall/` | `bagofwords/sandbox-firewall` | The helper that asserts BoW's packet-filter rules for the sandbox network |
| `tools/` | `bagofwords/sandbox-tools` | Carries the `sandboxd` binary, built from the pinned upstream commit |

## Pinned versions

| What | Version | Where it is pinned |
|---|---|---|
| `sandboxd` | `kubernetes-sigs/agent-sandbox` commit `b0040f5e51a4b98341af899e3ce9da767e63216f` | `tools/Dockerfile`, and the stubs in `backend/app/services/sandbox/sandboxd_proto/` |
| Caddy | 2.11.9 | `caddy/Dockerfile` |
| Forward proxy plugin | `v0.0.0-20260321230143-0aab84dad4fc` | `caddy/Dockerfile` |

Change the `sandboxd` commit in both places together: the generated gRPC
stubs must match the daemon.

## Using other images

BoW reads the image names from the release version. To use a mirror, a
private registry or a local build, set these on the BoW server:

```
BOW_SANDBOX_IMAGE=registry.example.com/bow/sandbox:1.2.3            # the built-in environment's image
BOW_SANDBOX_CADDY_IMAGE=registry.example.com/bow/sandbox-caddy:1.2.3
BOW_SANDBOX_FIREWALL_IMAGE=registry.example.com/bow/sandbox-firewall:1.2.3
BOW_SANDBOX_TOOLS_IMAGE=registry.example.com/bow/sandbox-tools:1.2.3
```

## Air-gapped hosts

A host that cannot pull images needs them loaded beforehand:

```bash
docker save bagofwords/sandbox:VERSION bagofwords/sandbox-caddy:VERSION \
            bagofwords/sandbox-firewall:VERSION bagofwords/sandbox-tools:VERSION -o bow-sandbox-images.tar
# copy the file to the host, then on the host:
docker load -i bow-sandbox-images.tar
```

Then tick "Images are preloaded" on the backend. Bootstrap checks that each
image is present instead of pulling it, and names any that is missing.

## Building locally

```bash
docker build -t bagofwords/sandbox:dev          deploy/sandbox/image
docker build -t bagofwords/sandbox-caddy:dev    deploy/sandbox/caddy
docker build -t bagofwords/sandbox-firewall:dev deploy/sandbox/firewall
docker build -t bagofwords/sandbox-tools:dev    deploy/sandbox/tools
```

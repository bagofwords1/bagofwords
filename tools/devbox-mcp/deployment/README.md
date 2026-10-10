# devbox-mcp deployment

Runs the Bag of Words (bow) devbox MCP server (the code in [`..`](../README.md)) in the
sandbox cluster at `https://devbox.sndbx.bagofwords.com/mcp`, behind the Envoy Gateway set up
by the `sandbox-cluster` manifests in the devex repo, with API-key authentication enforced by
the Gateway.

```
  Claude Code / MCP host  --X-API-Key-->  https://devbox.sndbx.bagofwords.com/mcp
                                                   |
                                        Envoy Gateway (eg)
                                        SecurityPolicy: API key check
                                                   |
                                        Service devbox-mcp:3400
                                                   |
                                        Pod devbox-mcp  (namespace devbox, SA devbox-mcp)
                                          |  kube API: SandboxClaims, Services, HTTPRoutes
                                          |  gRPC 9090: sandboxd in each sandbox pod
```

## 1. Build and push the image

From `tools/devbox-mcp` (the Dockerfile's build context):

```bash
cd tools/devbox-mcp
docker build --platform linux/amd64 -t bagofwords/devbox-mcp:latest .
docker push bagofwords/devbox-mcp:latest
```

## 2. Create the API-key Secret

One entry per client, created in the `devbox` namespace. The entry's key is the client id, forwarded to the server as
`X-Client-Id`; its value is the API key the client sends in `X-API-Key`.

```bash
kubectl -n devbox create secret generic devbox-mcp-apikeys \
  --from-literal=dima="$(openssl rand -hex 32)"
kubectl -n devbox get secret devbox-mcp-apikeys -o jsonpath='{.data.dima}' | base64 -d; echo
```

Add or rotate a client later with `kubectl -n devbox patch secret devbox-mcp-apikeys --type=merge -p '{"stringData":{"<client>":"<key>"}}'`.
Envoy picks up Secret changes without a restart.

## 3. Deploy

```bash
kubectl apply -f tools/devbox-mcp/deployment/devbox-mcp.yaml
kubectl -n devbox rollout status deployment/devbox-mcp
kubectl -n devbox get httproute devbox-mcp securitypolicy devbox-mcp
```

DNS: `devbox.sndbx.bagofwords.com` is covered by the existing `*.sndbx.bagofwords.com`
record and wildcard certificate; nothing to add.

## 4. Verify

```bash
# no key -> 401 from the Gateway, never reaches the server
curl -si https://devbox.sndbx.bagofwords.com/mcp | head -1

# with key -> 406 (the MCP endpoint wants a JSON-RPC POST), proving the key passed
curl -si -H "X-API-Key: <key>" https://devbox.sndbx.bagofwords.com/mcp | head -1
```

## 5. Register the client

```bash
claude mcp add --transport http devbox https://devbox.sndbx.bagofwords.com/mcp \
  --header "X-API-Key: <key>"
```

## Notes

- **One replica only.** `claude_login` sessions live in the server's memory and
  streamable-HTTP sessions are bound to the instance that created them. The
  Deployment uses `Recreate` so a rollout never runs two.
- **Namespace.** Everything here lives in the `devbox` namespace, including the API-key
  Secret (the SecurityPolicy can only reference a Secret in its own namespace). The
  devboxes themselves are created in the sandbox namespace, `default` by default
  (`DEVBOX_NAMESPACE`).
- **RBAC.** The ServiceAccount is bound to the built-in `cluster-admin` ClusterRole, so
  the server can manage sandbox objects in any namespace. Anyone holding a valid API key
  therefore has cluster-wide reach through the tools the server exposes; keep the key set
  small and rotate on departures.
- **Reaching sandboxes.** From inside the cluster the server dials sandboxd's gRPC
  port 9090 on the sandbox pod IP directly. No router, no port-forward, no kubectl
  in the image.
- **Host validation.** `MCP_ALLOWED_HOSTS` must list the public hostname, or the
  server answers 421 to requests arriving through the Gateway.

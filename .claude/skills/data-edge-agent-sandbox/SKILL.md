---
name: data-edge-agent-sandbox
description: run the BoW data edge agent sandbox. Use when the user asks to run or start the data edge agent sandbox 
---

# Run BoW Data Edge Agent

Deploys and runs the **data edge agent** — the process that holds credentials
for local data sources and answers proxied queries over NATS.


## Prerequisites
- Running **NATS** instance: Assume **NATS** instance has been provisioned. 
- Make sure **PostgreSQL** instance is up and running. If not, deploy one. Use `tools/agent/deploy-postgresql.sh` for deploying and/or checking the PostgreSQL status. 
- You must create `config.yaml` configuration file based on the environment setup. 
- Copy `data_plane/data_edge_agent/config.example.yaml` to `data_plane/config.yaml`. **IMPORTANT** after copying remove all the configuration comments from the `config.yaml` file.  
- Update the `data_plane/config.yaml` following below instructions 
  - set `org_id`: to get `org_id` run
    ```bash
    TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/jwt/login \
      -H 'Content-Type: application/x-www-form-urlencoded' \
      --data-urlencode 'username=admin@example.com' --data-urlencode 'password=Sandbox123!' \
      | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
    curl -s http://localhost:8000/api/organizations -H "Authorization: Bearer $TOKEN" \
      | python3 -c "import sys,json;print(json.load(sys.stdin)[0]['id'])"
    ```
  - all the certificates and keys are stored in local kubernetes cluster 
  - to access local kubernetes cluster use kubeconfig located at default location: ~/.kube/config
  - the certificates are managed by `CertManager` 
  - set`tls_ca`: `tls_ca` stored in kubernetes secret, read `nats-ca-keypair` secret in `nats` namespace to fetch teh `tls_ca` file and store it locally
  - set `tls_cert`: for `tls_cert` you'll need to apply `Certificate` yaml into kubernetes cluster 
    replace `<org_id>` in the provided below YAML manifest and apply it to the kubernetes cluster 
    ```yaml
      apiVersion: cert-manager.io/v1
      kind: Certificate
      metadata:
        name: <org_id>-<edge_agent_id>
        namespace: nats
      spec:
        secretName: <org_id>-<edge_agent_id>
        issuerRef:
          name: nats-ca
          kind: Issuer
        commonName: <org_id>-<edge_agent_id>
        usages:
          - client auth
        duration: 8760h                    
        renewBefore: 720h
        privateKey:
          algorithm: ECDSA
          size: 256
    ```
    - watch for `Certificate` status, once ready fetch the `tls_cert` and `tls_key` from kubernetes secret: `<org_id>-<edge_agent_id>` and store the files locally under `data_plane/certs` directory    
    - once `tls_ca`, `tls_cert` and `tls_key` are stored locally, update the `config.yaml` accordingly   
  - configure single served PostgreSQL connection in `config.yaml`
    - Use `tools/agent/deploy-postgresql.sh` for checking the status and fetching connection details 
    - set a new connection under `connections` in `config.yaml`. add host port, database, schema and credentials accordingly to response received from `tools/agent/deploy-postgresql.sh` status check
  - add a new DataEdgeAgent configuration into nats instance 
    - **idempotently** update `nats-accounts` `ConfigMap` in `nats` namespace 
    - replace `<org_id>.<edge_agent_id>` with actual values 
      ```bash
      {
        user: "CN=<org_id>-<edge_agent_id>"
        permissions {
          subscribe = { allow: ["tunnel.<org_id>.<edge_agent_id>.>"] }
          publish   = { allow: ["tunnel.<org_id>.advertisements", "tunnel.<org_id>.<edge_agent_id>.>"] }
          allow_responses = { max: 1, expires: "20m" }
        }
      }
      ```
    - once ConfigMap will be updated, nats will automatically reload config and allow new DataEdgeAgent connect
- Generate `Certificate` for BoW App backend
  - Fetch `tls_ca` which is stored in kubernetes secret, read `nats-ca-keypair` secret in `nats` namespace to fetch the `tls_ca` and store it locally in file
  - Create `Certificate`, replace <org_id> with real org id discovered previously and apply below YAML manifest   
    ```yaml
    apiVersion: cert-manager.io/v1
    kind: Certificate
    metadata:
      name: <org_id>-worker
      namespace: nats
    spec:
      secretName: <org_id>-worker
      issuerRef: { name: nats-ca, kind: Issuer }
      commonName: <org_id>-worker        # <- the worker CN
      usages: [client auth]
      duration: 8760h
      privateKey: { algorithm: ECDSA, size: 256 } 
    ```
  - watch for `Certificate` status, once ready fetch the `tls_cert` and `tls_key` from kubernetes secret: `<org_id>-worker` and store the files locally under `backend/certs` directory
  - add the worker NATS user into the nats instance
    - **idempotently** update `nats-accounts` `ConfigMap` in `nats` namespace (its
      CN must match the worker cert exactly, and the worker gets org scope for
      tunnel subjects plus its own **org-scoped reply inbox** `_INBOX.bow.<org_id>.>`
      — the backend generates reply subjects under that same prefix, and scoping
      it keeps one org's replies from reaching another's worker. No
      `allow_responses` — that is for the edge agent). Replace `<org_id>` with the
      real value:
      ```bash
      {
        user: "CN=<org_id>-worker"
        permissions {
          publish   = { allow: ["tunnel.<org_id>.>"] }
          subscribe = { allow: ["tunnel.<org_id>.>", "_INBOX.bow.<org_id>.>"] }
        }
      }
      ```
    - once the ConfigMap is updated, nats reloads automatically and allows the backend to connect
  - update `backend/.env` and set `BOW_DATA_TUNNEL_ORG_ID` to the org id discovered earlier
  - update `backend/.env` and set `BOW_DATA_TUNNEL_TLS_CA` , `BOW_DATA_TUNNEL_TLS_CERT` and `BOW_DATA_TUNNEL_TLS_KEY` accordingly
  - update `backend/.env` and make sure the `BOW_DATA_TUNNEL_URL`is set to `tls://data-edge-tunnels.bow.dev.local:4222`
  - update `backend/.env` and set `BOW_DATA_TUNNEL_ENABLED=true` (the tunnel is off by
    default; when true, all of `BOW_DATA_TUNNEL_ORG_ID` + `BOW_DATA_TUNNEL_URL` + the three
    `BOW_DATA_TUNNEL_TLS_*` paths are required or the backend logs an error and stays disabled)


## Start Data Edge Agent and Boot the sandbox 
```bash
cd data_plane && uv run python -m data_edge_agent --config config.yaml
```
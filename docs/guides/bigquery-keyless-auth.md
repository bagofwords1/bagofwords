# BigQuery without a service-account key

A BigQuery connection normally stores a service-account JSON key. That key is a
long-lived secret: it has to be created, copied into BOW, and rotated by hand.

On a self-hosted deployment you can avoid the key entirely. The connection then
authenticates as **the deployment's own Google identity** through Google's
Application Default Credentials (ADC), and BOW stores no secret for it.

What "the deployment's own identity" is depends on where BOW runs. You set it up
once, on the server, outside BOW:

The Docker and Kubernetes recipes are in step 2.

| Where BOW runs | Identity it uses | Keyless? |
|---|---|---|
| Google Cloud (Compute Engine, GKE, Cloud Run) | The service account attached to the VM / pod | Yes |
| AWS or Azure VM, or Kubernetes elsewhere | Workload Identity Federation: Google trusts the AWS role, Azure managed identity, or Kubernetes service-account token the workload already has | Yes |
| A server with no cloud identity (bare metal, plain VM) | Workload Identity Federation with an X.509 certificate or an OIDC token your own tooling issues | No Google key, but something on the server still proves who it is |

## 1. Turn it on

The option is off by default. Enabling it lets anyone who can create a
connection in BOW run queries as the server's identity (and as any service
account that identity may impersonate), so enable it only where you operate both
the deployment and that identity.

```bash
BOW_ALLOW_GOOGLE_DEFAULT_CREDENTIALS=true
```

or in `bow-config.yaml`:

```yaml
security:
  allow_google_default_credentials: true
```

Restart BOW. While it is off, the option is not offered in the connection form
and connections already using it stop working with a message saying so.

## 2. Give the server an identity

Pick the recipe for how BOW is deployed. In every recipe the federated
principal or service account you end up with is the one to grant access to in
step 3.

For the federation recipes you first create, once, in Google Cloud:

1. a **workload identity pool** and a **provider** that trusts your workload's
   identity, restricted with an attribute condition to the one role, managed
   identity or service account BOW runs as;
2. a **credential configuration file** for that provider
   (`gcloud iam workload-identity-pools create-cred-config …`). The file holds
   **no secret** — only where the workload finds its own token and which
   provider to present it to.

### Docker (docker-compose or `docker run`)

**On a Google Cloud VM.** Attach a service account to the VM. Containers reach
the metadata server on their own, so only the opt-in is needed:

```yaml
# docker-compose.yaml
services:
  app:
    environment:
      - BOW_ALLOW_GOOGLE_DEFAULT_CREDENTIALS=true
```

```bash
docker run -p 3000:3000 -e BOW_ALLOW_GOOGLE_DEFAULT_CREDENTIALS=true bagofwords/bagofwords
```

If you will impersonate another service account (step 4), the VM's access
scopes must include `cloud-platform`.

**On an AWS or Azure VM.** Generate the credential configuration for the VM's
role or managed identity, then mount it and point ADC at it:

```bash
gcloud iam workload-identity-pools create-cred-config \
  projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/POOL/providers/PROVIDER \
  --aws --enable-imdsv2 \
  --output-file=credential-config.json      # use --azure on Azure
```

```yaml
# docker-compose.yaml
services:
  app:
    environment:
      - BOW_ALLOW_GOOGLE_DEFAULT_CREDENTIALS=true
      - GOOGLE_APPLICATION_CREDENTIALS=/run/google/credential-config.json
    volumes:
      - ./credential-config.json:/run/google/credential-config.json:ro
```

```bash
docker run -p 3000:3000 \
  -e BOW_ALLOW_GOOGLE_DEFAULT_CREDENTIALS=true \
  -e GOOGLE_APPLICATION_CREDENTIALS=/run/google/credential-config.json \
  -v "$PWD/credential-config.json:/run/google/credential-config.json:ro" \
  bagofwords/bagofwords
```

On AWS, raise the instance metadata hop limit to 2
(`aws ec2 modify-instance-metadata-options --http-put-response-hop-limit 2 …`).
With the default of 1, a container on the bridge network gets no answer from
the metadata service and the token exchange fails.

**On a server with no cloud identity** (bare metal, a plain VM). Federation
still needs something to vouch for the workload:

- **An OIDC token your tooling writes to a file** (a secrets-manager agent,
  SPIRE). Generate the configuration with
  `--credential-source-file=/run/google/token`, mount that token file next to
  the configuration as above; it is re-read whenever a token is needed.
- **X.509 certificates** from your own PKI (see Google's "Workload Identity
  Federation with X.509 certificates"). The private key is yours, short-lived,
  and rotated by your tooling; no Google key exists.

If neither fits, keep the service-account key and rotate it instead: edit the
connection, or `PUT /api/connections/{connection_id}` with
`{"credentials": {"credentials_json": "<new key>"}}`.

### Kubernetes (Helm chart in `k8s/chart`)

The chart needs no template change: use `extraEnv`, `serviceAccount`,
`extraVolumes` and `extraVolumeMounts` in your values file.

**On GKE, with Workload Identity Federation for GKE.** Grant the BigQuery roles
(step 3) to the pod's Kubernetes service account, either directly as a
principal or through a Google service account bound to it. With a bound Google
service account:

```yaml
# values.yaml
serviceAccount:
  name: bowapp
  annotations:
    iam.gke.io/gcp-service-account: bow@PROJECT_ID.iam.gserviceaccount.com
extraEnv:
  - name: BOW_ALLOW_GOOGLE_DEFAULT_CREDENTIALS
    value: "true"
```

```bash
gcloud iam service-accounts add-iam-policy-binding bow@PROJECT_ID.iam.gserviceaccount.com \
  --role=roles/iam.workloadIdentityUser \
  --member="serviceAccount:PROJECT_ID.svc.id.goog[NAMESPACE/bowapp]"
```

No credential file is involved.

**On any other cluster (EKS, AKS, on-prem).** Google trusts the cluster's own
service-account tokens. Create the provider as an OIDC provider with the
cluster's issuer URL (upload the cluster's signing keys to the provider if the
issuer is not reachable from the internet), then have Kubernetes project a token
for that provider into the pod and point the credential configuration at it:

```bash
gcloud iam workload-identity-pools create-cred-config \
  projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/POOL/providers/PROVIDER \
  --credential-source-file=/var/run/google/token \
  --output-file=credential-config.json

kubectl -n NAMESPACE create configmap bow-google-credential-config \
  --from-file=credential-config.json
```

```yaml
# values.yaml
extraEnv:
  - name: BOW_ALLOW_GOOGLE_DEFAULT_CREDENTIALS
    value: "true"
  - name: GOOGLE_APPLICATION_CREDENTIALS
    value: /etc/google/credential-config.json
extraVolumes:
  - name: google-token
    projected:
      sources:
        - serviceAccountToken:
            # must equal the audience the provider accepts
            audience: https://iam.googleapis.com/projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/POOL/providers/PROVIDER
            expirationSeconds: 3600
            path: token
  - name: google-credential-config
    configMap:
      name: bow-google-credential-config
extraVolumeMounts:
  - name: google-token
    mountPath: /var/run/google
    readOnly: true
  - name: google-credential-config
    mountPath: /etc/google
    readOnly: true
```

The federated principal to grant access to is the pod's service account:
`principal://iam.googleapis.com/projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/POOL/subject/system:serviceaccount:NAMESPACE:bowapp`.
Kubernetes rotates the projected token; nothing is stored in a Secret.

## 3. Grant BigQuery access

Grant the identity from step 2 what a BOW service account would need:
`roles/bigquery.jobUser` on the project that runs the queries, and
`roles/bigquery.dataViewer` on the datasets BOW should read.

## 4. Optional: impersonate a service account per connection

One server has one identity. To give connections different permissions, create a
service account per purpose, grant *it* the BigQuery roles, and let the server's
identity impersonate it:

```bash
gcloud iam service-accounts add-iam-policy-binding \
  bq-reader@PROJECT.iam.gserviceaccount.com \
  --role=roles/iam.serviceAccountTokenCreator \
  --member="SERVER_IDENTITY"
```

The IAM Service Account Credentials API must be enabled in the project.

## 5. Create the connection

In BOW: **Add connection → Google BigQuery**, pick
**Application Default Credentials** as the authentication method, fill
in the project and dataset, and optionally the **service account to
impersonate**. **Test connection** runs the whole chain and names the step that
failed:

| Message | Meaning |
|---|---|
| *Application Default Credentials are turned off on this deployment* | Step 1 was not done, or BOW was not restarted |
| *No Application Default Credentials were found on this deployment* | Step 2: no attached service account, or `GOOGLE_APPLICATION_CREDENTIALS` points nowhere |
| *The deployment's Google identity could not obtain an access token* | The federation exchange was refused: check the provider's audience and attribute condition |
| *Could not impersonate …* | Step 4: the Token Creator binding is missing |
| *Access Denied …* | Step 3: the identity has no BigQuery role |

A replaced credential configuration file is picked up without a restart.

## What does not change

- **Per-user sign-in** ("Require user authentication" with Sign in with Google)
  works as before, on top of either method. It still needs an OAuth client ID
  and secret on the connection — that is not a service-account key, but it is a
  secret.
- Switching an existing connection between a key and keyless auth removes the
  other method's stored fields: moving to keyless deletes the saved key.
- Only BigQuery connections have this option today.

# BigQuery without a service-account key

A BigQuery connection normally stores a service-account JSON key. That key is a
long-lived secret: it has to be created, copied into BOW, and rotated by hand.

On a self-hosted deployment you can avoid the key entirely. The connection then
authenticates as **the deployment's own Google identity** through Google's
Application Default Credentials (ADC), and BOW stores no secret for it.

What "the deployment's own identity" is depends on where BOW runs. You set it up
once, on the server, outside BOW:

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

### Google Cloud

Attach a service account to the VM, node pool, or Cloud Run service. Nothing
else is needed: containers reach the metadata server on their own.

If you will impersonate another service account (step 4) from a Compute Engine
VM, the VM's access scopes must include `cloud-platform`.

### AWS, Azure, or Kubernetes outside Google Cloud

1. In Google Cloud, create a workload identity pool and a provider that trusts
   your workload's identity. Restrict it with an attribute condition to the one
   role, managed identity, or service account BOW runs as.
2. Grant that federated principal access (step 3), or let it impersonate a
   service account (step 4).
3. Generate the credential configuration file:

   ```bash
   gcloud iam workload-identity-pools create-cred-config \
     projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/POOL/providers/PROVIDER \
     --aws --enable-imdsv2 \
     --output-file=credential-config.json
   ```

   Use `--azure` or `--credential-source-file=/path/to/token` instead of `--aws`
   for the other cases. The file contains **no secret** — only where the workload
   finds its own token and which provider to present it to.
4. Mount the file into the BOW container and point ADC at it:

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

On AWS with docker-compose, raise the instance metadata hop limit to 2
(`aws ec2 modify-instance-metadata-options --http-put-response-hop-limit 2 ...`).
With the default of 1, a container on the bridge network gets no answer from the
metadata service and the token exchange fails.

### A server with no cloud identity

Federation still needs something to vouch for the workload:

- **X.509 certificates** from your own PKI (see Google's "Workload Identity
  Federation with X.509 certificates"). The private key is yours, short-lived,
  and rotated by your tooling; no Google key exists.
- **An OIDC token your tooling writes to a file** (a secrets-manager agent, SPIRE).
  Use `--credential-source-file`; BOW re-reads the file whenever a token is needed.

If neither fits, keep the service-account key and rotate it instead: edit the
connection, or `PUT /api/connections/{connection_id}` with
`{"credentials": {"credentials_json": "<new key>"}}`.

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

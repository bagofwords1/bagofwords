## Install with Kubernetes
---
You can install Bag of Words on a Kubernetes cluster. The Helm chart can deploy the app with a bundled PostgreSQL instance **or** connect to an external managed database such as AWS Aurora with IAM authentication.

### 1. Add the Helm Repository

```bash
helm repo add bow https://helm.bagofwords.com
helm repo update
```

### 2. Install or Upgrade the Chart

Here are a few examples of how to install or upgrade the Bag of Words Helm chart:

### Deploy with a bundled PostgreSQL instance
```bash
helm upgrade -i --create-namespace \
 -nbowapp-1 bowapp bow/bagofwords \
 --set postgresql.auth.username=<PG-USER> \
 --set postgresql.auth.password=<PG-PASS> \
 --set postgresql.auth.database=<PG-DB>
```

### Deploy without TLS and with a custom hostname
```bash
helm upgrade -i --create-namespace \
 -nbowapp-1 bowapp bow/bagofwords \
  --set host=<HOST> \
 --set postgresql.auth.username=<PG-USER> \
 --set postgresql.auth.password=<PG-PASS> \
 --set postgresql.auth.database=<PG-DB> \
 --set ingress.tls=false
```

### Deploy with TLS, cert-manager, and Google OAuth
```bash
helm upgrade -i --create-namespace \
 -nbowapp-1 bowapp bow/bagofwords \
 --set host=<HOST> \
 --set postgresql.auth.username=<PG-USER> \
 --set postgresql.auth.password=<PG-PASS> \
 --set postgresql.auth.database=<PG-DB> \
 --set config.googleOauthEnabled=true \
 --set config.googleClientId=<CLIENT_ID> \
 --set config.googleClientSecret=<CLIENT_SECRET>
```

### Deploy with AWS Aurora and IAM Authentication

When using a managed database like AWS Aurora PostgreSQL, the chart skips the bundled PostgreSQL subchart and connects directly to your Aurora cluster. Passwords are generated at runtime using IAM — no static credentials are stored.

**Prerequisites:**
- An Aurora PostgreSQL cluster with IAM database authentication enabled
- A database user with `GRANT rds_iam TO <username>`
- An IAM role with `rds-db:connect` permission
- In EKS: an IRSA (IAM Roles for Service Accounts) annotation on the pod's service account

```bash
helm upgrade -i --create-namespace \
 -nbowapp-1 bowapp bow/bagofwords \
 --set host=<HOST> \
 --set database.auth.provider=aws_iam \
 --set database.auth.region=us-east-1 \
 --set database.auth.sslMode=require \
 --set database.host=<AURORA-CLUSTER-ENDPOINT> \
 --set database.port=5432 \
 --set database.username=<DB-USER> \
 --set database.name=<DB-NAME> \
 --set serviceAccount.annotations.'eks\.amazonaws\.com/role-arn'=arn:aws:iam::<ACCOUNT>:role/<ROLE-NAME>
```

For example, with a real Aurora cluster:
```bash
helm upgrade -i --create-namespace \
 -nbowapp-1 bowapp bow/bagofwords \
 --set host=bow.example.com \
 --set database.auth.provider=aws_iam \
 --set database.auth.region=us-east-1 \
 --set database.auth.sslMode=require \
 --set database.host=bow-pg1-instance-1.cry2og862pqb.us-east-1.rds.amazonaws.com \
 --set database.port=5432 \
 --set database.username=bow_user \
 --set database.name=postgres \
 --set serviceAccount.annotations.'eks\.amazonaws\.com/role-arn'=arn:aws:iam::123456789012:role/bow-rds-role
```

### Deploy with Aurora using values.yaml

For Aurora deployments, you can also set all values in a file:

```yaml
# aurora-values.yaml
host: bow.example.com

database:
  auth:
    provider: aws_iam
    region: us-east-1
    sslMode: require
  host: bow-pg1-instance-1.cry2og862pqb.us-east-1.rds.amazonaws.com
  port: 5432
  username: bow_user
  name: postgres

serviceAccount:
  name: bowapp
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::123456789012:role/bow-rds-role

config:
  encryptionKey: "<your-encryption-key>"
  baseUrl: "https://bow.example.com"
```

```bash
helm upgrade -i --create-namespace \
 -nbowapp-1 bowapp bow/bagofwords \
 -f aurora-values.yaml
```

### Use existing Secret
1. Make sure the namespace exists, if not create it
```bash
   kubectl create namespace <namespace>
```
2. Create the secret with the environment variables you want to override
```yaml
apiVersion: v1
kind: Secret
metadata:
  name: <secret-name>
  namespace: <namespace>
stringData:
  postgres-password: "<postgres-password>"
  BOW_DATABASE_URL: "postgresql://<postgres-user>:<postgres-password>@<postgres-host>:5432/<postgres-database>"
  BOW_BASE_URL: "<base-url>"
  BOW_ENCRYPTION_KEY: "<encryption-key>"
  BOW_GOOGLE_AUTH_ENABLED: "false"
  BOW_GOOGLE_CLIENT_ID: "<client-id>"
  BOW_GOOGLE_CLIENT_SECRET: "<client-secret>"
  BOW_ALLOW_UNINVITED_SIGNUPS: "false"
  BOW_ALLOW_MULTIPLE_ORGANIZATIONS: "false"
  BOW_VERIFY_EMAILS: "false"
  BOW_INTERCOM_ENABLED: "false"

  # SMTP Configuration
  BOW_SMTP_HOST: "<smtp-host>"
  BOW_SMTP_PORT: "<smtp-port>"
  BOW_SMTP_USERNAME: "<smtp-username>"
  BOW_SMTP_PASSWORD: "<smtp-password>"
  BOW_SMTP_FROM_NAME: "<from-name>"
  BOW_SMTP_FROM_EMAIL: "<from-email>"
  BOW_SMTP_USE_TLS: "true"
  BOW_SMTP_USE_SSL: "false"
  BOW_SMTP_USE_CREDENTIALS: "true"
  BOW_SMTP_VALIDATE_CERTS: "true"
```

**Note**: When using an existing secret, the values in the secret will override the default values from the ConfigMap. You only need to include the environment variables you want to override.

3. Deploy BoW Application
```bash
helm install \
  bowapp ./chart \
 -n bowapp-1 \
 --set postgresql.auth.existingSecret=existing-bowapp-secret \
 --set config.secretRef=existing-bowapp-secret
```


### Service Account annotations
For adding a SA annotation pass the following flag during `helm install` command
`--set serviceAccount.annotations.foo=bar`
Otherwise, set annotations directly in values.yaml file by updating
```yaml
serviceAccount:
  ...
  annotations:
    foo: bar
```

For IRSA (EKS IAM Roles for Service Accounts), annotate with the IAM role ARN:
```bash
--set serviceAccount.annotations.'eks\.amazonaws\.com/role-arn'=arn:aws:iam::<ACCOUNT>:role/<ROLE-NAME>
```

### Configure node selector
For adding a node selector to both the BowApp and the PostgreSQL instance set the following flag during `helm install`
command ` --set postgresql.primary.nodeSelector.'kubernetes\.io/hostname'=kind-control-plane`
Otherwise, set node selector directly in values.yaml
```yaml
postgresql:
  ...
  primary:
    ...
    nodeSelector:
      kubernetes.io/hostname: kind-control-plane
```

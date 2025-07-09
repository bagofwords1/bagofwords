## Install with Kubernetes
---
You can install Bag of words on a Kubernetes cluster. The following deployment will deploy the Bagofwords container alongside a postgres instance.

### 1. Add the Helm Repository

```bash
helm repo add bow https://bagofwords.com/helm
helm repo update
```

### 2. Install or Upgrade the Chart

Here are a few examples of how to install or upgrade the Bag of words Helm chart:

### Deploy with a pg instance
```bash
helm upgrade -i --create-namespace \
 -nbowapp-1 bowapp bow/bagofwords \
 --set postgresql.auth.username=<PG-USER> \
 --set postgresql.auth.password=<PG-PASS> \
 --set postgresql.auth.database=<PG-DB>
```

### Deploy without TLS and with a custom hostname
```bash
# deploy without TLS with custom hostname
helm upgrade -i --create-namespace \
 -nbowapp-1 bowapp bow/bagofwords \
  --set host=<HOST> \
 --set postgresql.auth.username=<PG-USER> \
 --set postgresql.auth.password=<PG-PASS> \
 --set postgresql.auth.database=<PG-DB> \
 --set ingress.tls=false
``` 

### Deploy without TLS and with a custom hostname
```bash
# deploy with TLS, certs by cert manager and Googole oauth enabled 
helm upgrade -i --create-namespace \
 -nbowapp-1 bowapp bow/bagofwords \
 --set host=<HOST> \
 --set postgresql.auth.username=<PG-USER> \
 --set postgresql.auth.password=<PG-PASS> \
 --set postgresql.auth.database=<PG-DB>
 --set config.googleOauthEnabled=true \
 --set config.googleClientId=<CLIENT_ID> \
 --set config.googleClientSecret=<CLIENT_SECRET>
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

```bash
helm upgrade -i --create-namespace \
 -nbowapp-1 bowapp ./chart \
 --set postgresql.auth.username=<PG-USER> \
 --set postgresql.auth.password=<PG-PASS> \
 --set postgresql.auth.database=<PG-DB>
```

```bash
# deploy without TLS with custom hostname
helm upgrade -i --create-namespace \
 -nbowapp-1 bowapp ./chart \
  --set host=<HOST> \
 --set postgresql.auth.username=<PG-USER> \
 --set postgresql.auth.password=<PG-PASS> \
 --set postgresql.auth.database=<PG-DB>
 --set ingress.tls=false
``` 


```bash
# deploy with TLS, certs by cert manager and Googole oauth enabled 
helm upgrade -i --create-namespace \
 -nbowapp-1 bowapp ./chart \
 --set host=<HOST> \
 --set postgresql.auth.username=<PG-USER> \
 --set postgresql.auth.password=<PG-PASS> \
 --set postgresql.auth.database=<PG-DB>
 --set config.googleOauthEnabled=true \
 --set config.googleClientId=<CLIENT_ID> \
 --set config.googleClientSecret=<CLIENT_SECRET>
``` 


### Use existing DB creds secret
1. Make sure the namespace exists, if not create it 
```bash
   kubectl create namespace bowapp-1
```
2. Create PostgreSQL secret 
```yaml
apiVersion: v1
kind: Secret
metadata:
  name: <secret-name>
  namespace: <namespace>
stringData:
  postgres-password: "<postgres-password>" 
  BOW_DATABASE_URL: "postgresql://<postgres-user>:<postgres-password>@<postgres-host>:5432/<postgres-database>"
  BOW_DEFAULT_LLM_API_KEY: "<default-api-llm-key>"
  BOW_ENCRYPTION_KEY: "<encryption-key>"
  BOW_GOOGLE_AUTH_ENABLED: "false"
  BOW_GOOGLE_CLIENT_ID: "<client-id>"
  BOW_GOOGLE_CLIENT_SECRET: "<client-secret>"
  BOW_SMTP_PASSWORD: "<smtp-password>"
```
3. Deploy BoW Application  
```bash
helm install \
  bowapp ./chart \
 -n bowapp-1 \
 --set postgresql.auth.existingSecret=existing-bowapp-secret \
 --set config.secretRef=existing-bowapp-secret
```


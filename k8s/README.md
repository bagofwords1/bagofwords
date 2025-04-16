
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



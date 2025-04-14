```bash
helm upgrade -i --create-namespace \
 -n bagofwords bowapp ./chart \
 --set postgresql.auth.username=user \
 --set postgresql.auth.password=password \
 --set postgresql.auth.database=pgdb
```
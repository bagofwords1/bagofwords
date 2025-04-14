```bash
helm upgrade -i --create-namespace \
 -nbowapp-1 bowapp ./chart \
 --set postgresql.auth.username=<PG-USER> \
 --set postgresql.auth.password=<PG-PASS> \
 --set postgresql.auth.database=<PG-DB>
```
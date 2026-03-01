# Bag of Words Helm Chart

This Helm chart deploys the **Bag of Words** service on Kubernetes with a bundled PostgreSQL instance or an external managed database (e.g. AWS Aurora with IAM authentication).

See [k8s/README.md](../README.md) for full installation examples including Aurora IAM auth, existing secrets, IRSA, and more.

## Quick Start

```bash
helm repo add bow https://helm.bagofwords.com
helm repo update

# Deploy with bundled PostgreSQL
helm upgrade -i --create-namespace \
 -nbowapp-1 bowapp bow/bagofwords \
 --set postgresql.auth.username=<PG-USER> \
 --set postgresql.auth.password=<PG-PASS> \
 --set postgresql.auth.database=<PG-DB>

# Deploy with AWS Aurora + IAM auth
helm upgrade -i --create-namespace \
 -nbowapp-1 bowapp bow/bagofwords \
 --set database.auth.provider=aws_iam \
 --set database.auth.region=us-east-1 \
 --set database.auth.sslMode=require \
 --set database.host=<AURORA-CLUSTER-ENDPOINT> \
 --set database.port=5432 \
 --set database.username=<DB-USER> \
 --set database.name=<DB-NAME> \
 --set serviceAccount.annotations.'eks\.amazonaws\.com/role-arn'=arn:aws:iam::<ACCOUNT>:role/<ROLE-NAME>
```

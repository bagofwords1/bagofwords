# Bag of Words Helm Chart

This Helm chart deploys the **Bag of Words** service on Kubernetes.

## Install with Kubernetes
---
You can install Bag of words on a Kubernetes cluster. The following deployment will deploy the Bagofwords container alongside a postgres instance.

### 1. Add the Helm Repository

```bash
helm repo add bow https://helm.bagofwords.com
helm repo update
```

### 2. Install or Upgrade the Chart

Here are a few examples of how to install or upgrade the Bag of words Helm chart:


```bash
helm upgrade -i --create-namespace \
 -nbowapp-1 bowapp bow/bagofwords \
 --set postgresql.auth.username=<PG-USER> \
 --set postgresql.auth.password=<PG-PASS> \
 --set postgresql.auth.database=<PG-DB>
```

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
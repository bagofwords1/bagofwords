# Bag of Words Helm Chart

This Helm chart deploys the **Bag of Words** service on Kubernetes.

## 🧩 Installation

### 1. Add the Helm Repository

```bash
helm repo add bow https://bagofwords.com/helm
helm repo update
```

```bash
helm upgrade -i --create-namespace \
 -n bagofwords bowapp ./chart \
 --set postgresql.auth.username=user \
 --set postgresql.auth.password=password \
 --set postgresql.auth.database=pgdb
```
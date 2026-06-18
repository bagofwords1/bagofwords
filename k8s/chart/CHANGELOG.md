# Changelog

## 2.0.0 — Breaking change

### ⚠ Breaking: Deployment selector labels changed

The `spec.selector.matchLabels` on the Deployment was changed from:

```yaml
app: bowapp
```

to the Kubernetes standard labels:

```yaml
app.kubernetes.io/name: <release-name>
app.kubernetes.io/instance: <release-name>
```

`spec.selector` is immutable in Kubernetes. Upgrading from 1.x will fail with:

```
field is immutable: spec.selector
```

**Migration:** Delete the existing Deployment before syncing the new chart version. Kubernetes will recreate it with the correct selector labels.

```bash
kubectl delete deployment <release-name> -n <namespace>
```

If you use ArgoCD, you can alternatively annotate the Deployment in your Application override:

```yaml
argocd.argoproj.io/sync-options: Replace=true
```

### Other changes in 2.0.0

- Chart templates fully rewritten with standard Kubernetes labels and helper functions
- Added: HPA, PodDisruptionBudget, NetworkPolicy, ServiceAccount templates (all disabled by default)
- Added: strict `values.schema.json` validation
- Added: configurable probes, `extraEnv`, `extraEnvFrom`, `extraVolumes`, security contexts, sidecars
- Removed: ServiceMonitor template (not needed)
- Removed: bundled Postgres subchart replaced by Bitnami postgresql dependency

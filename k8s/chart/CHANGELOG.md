# Changelog

## 2.1.0

### Added: persistent storage for uploaded files

Previously the chart mounted no volume for `/app/backend/uploads`, so user
uploads (CSV/Excel files, branding logos, avatars) lived on the pod's
ephemeral filesystem and were lost on every pod restart or rollout — while
their database records survived, causing later reads to fail with
`No such file or directory`.

- New `persistence.uploads` values: `enabled` (default `false`),
  `existingClaim`, `size`, `storageClass`, `accessModes`, `annotations`.
- When enabled, the chart creates a PVC (or uses `existingClaim`) mounted at
  `/app/backend/uploads`, and an init container seeds the expected
  `files/branding/avatars` subdirectories with `app`-user ownership.
- With the default `ReadWriteOnce` access mode, the deployment strategy
  defaults to `Recreate` (unless `strategy` is set explicitly) so upgrades
  don't deadlock on the volume attachment, and rendering fails if
  `replicaCount > 1` or `autoscaling.enabled=true`. Use a `ReadWriteMany`
  storage class for multi-replica setups.
- The created PVC carries `helm.sh/resource-policy: keep` by default so
  `helm uninstall` does not delete uploaded files.
- `NOTES.txt` now warns that uploads are ephemeral when persistence is off.

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

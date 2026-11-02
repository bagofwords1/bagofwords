# Changelog

## 3.1.0

### Upgrades now stop the old pods first (`strategy: Recreate`)

The Deployment now defaults to `strategy.type: Recreate` instead of the
Kubernetes default (`RollingUpdate`). Each new pod runs the database
migrations before it serves (`start.sh`), in one transaction that takes
exclusive table locks. Under a rolling update the old pods keep querying the
same tables meanwhile, and the migration can fail with
`psycopg2.errors.DeadlockDetected` on every retry. Even when it succeeds, the
old code can break against the new schema once it commits.

**What changes for you:** each upgrade has a short outage while the old pods
stop and the new ones start and migrate. The first upgrade to 3.1.0 is itself
a recreate.

**To keep rolling updates**, set them explicitly, accepting that an upgrade
whose migrations the previous version can't run against may deadlock or
break the old pods:

```yaml
strategy:
  type: RollingUpdate
```

## 3.0.0 — Breaking change

### ⚠ Breaking: bundled PostgreSQL no longer uses the Bitnami subchart

Bitnami stopped publishing versioned `docker.io/bitnami/postgresql` images, so
the subchart's pinned image (`17.2.0-debian-12-r3`) can no longer be pulled and
fresh installs hung in `ImagePullBackOff`. The chart now ships its own
single-instance PostgreSQL (`templates/postgresql/`) running the Docker
Official Image `postgres:17-bookworm`, and has no chart dependencies.

**Existing data is adopted in place.** The StatefulSet name, selector labels,
`serviceName`, PVC template (`data-<release>-postgresql-0`), on-disk layout
(`<volume>/data`, uid/gid 1001) and Secret keys (`password`,
`postgres-password`) all match what the Bitnami subchart rendered, and both
images are PostgreSQL 17 on Debian 12, so `helm upgrade` restarts the database
pod on the new image against the same volume — nothing is copied or converted.
Expect about a minute of database downtime: the old pod is stopped (up to 30s)
and the new one replays the write-ahead log on its first start. The minor
version moves from 17.2 to the current 17.x; check the PostgreSQL 17.x release
notes for any reindex advice that applies to you.

**Before upgrading:**

1. Recommended: snapshot the data volume or `pg_dump` the database. The data
   is reused in place, but there is no rollback to 2.x — its Bitnami image can
   no longer be pulled — so the backup is your way back if anything goes wrong.
2. Remove Bitnami-only keys under `postgresql:` from your values (e.g.
   `architecture`, `primary.resourcesPreset`, `metrics`, `readReplicas`). They
   are now rejected by the values schema instead of being silently ignored. The
   keys this chart still reads are listed under `postgresql.*` in README.md.
   Remove any `postgresql.image` override pointing at `bitnami`/`bitnamilegacy`
   too — the chart now refuses it, since a Bitnami image cannot start with this
   chart's settings.
3. Upgrade with `--set postgresql.migrateFromBitnami=true`. While the release
   still runs the Bitnami StatefulSet, `helm upgrade` **fails on purpose**
   without it, so a pipeline that does not pin `--version` cannot migrate the
   database unattended. It is a one-time opt-in: after this upgrade the flag is
   no longer needed (leaving it set is harmless). The check needs cluster access,
   so it does not run under `helm template` or Argo CD — pin the chart's major
   version there (`targetRevision: 2.*`) until you are ready.
4. If you changed the PVC template in 2.x (e.g. `primary.persistence.storageClass`
   or `global.storageClass`), set the same value under
   `postgresql.primary.persistence` — or, if the upgrade fails with
   `updates to statefulset spec for fields other than ... are forbidden`,
   delete only the StatefulSet object and upgrade again (the PVC and its data are
   kept):

   ```bash
   kubectl delete statefulset <release>-postgresql -n <namespace> --cascade=orphan
   ```

   Deleting it also removes what the upgrade check looks for, so take the
   backup from step 1 before you do.

### Other changes in 3.0.0

- The bundled PostgreSQL is no longer deployed when `database.auth.provider` is
  not `password` (IAM-authenticated external database). In 2.x the Bitnami
  StatefulSet was still created there but never used; upgrading removes it and
  its Service/Secret. Its PVC is kept — delete it yourself once you are sure
  nothing on it is needed.
- A `preStop` hook sends PostgreSQL a fast-shutdown signal (SIGINT), so stops
  (rollouts, node drains) take seconds instead of waiting the full grace period
  for the app's pooled connections and then being killed. The 2.x → 3.x
  migration itself doesn't benefit: the old Bitnami pod is stopped under its
  own spec, which has no hook.
- Password logins use `md5` in `pg_hba.conf`, as the Bitnami chart did. It runs
  SCRAM for SCRAM-stored passwords and still accepts MD5-stored ones on an
  adopted volume.
- If you render the chart without cluster access (`helm template`, Argo CD)
  and don't use `postgresql.auth.existingSecret`, set
  `postgresql.auth.postgresPassword`. Otherwise the chart can't read the stored
  superuser password and writes a new random one to the Secret on every sync
  (the database keeps the original). The Bitnami chart behaved the same way.
- With `postgresql.auth.existingSecret`, the Secret must contain the key the
  superuser password is read from (`postgres-password`, or `password` when
  `postgresql.auth.username` is `postgres`). A missing key now stops the pod
  with `CreateContainerConfigError` instead of a crash loop on first start.
- `postgresql.auth.database` is required whenever `postgresql.auth.username`
  names an app user, including with `existingSecret`.

- `postgresql.auth.username`, `password` and `database` are now required for the
  bundled database (unless `postgresql.auth.existingSecret` is set); a missing
  value fails the render instead of producing an unusable `BOW_DATABASE_URL`.
- The PostgreSQL pod now requests `100m` CPU / `256Mi` memory with no limits
  (Bitnami's `nano` preset capped it at `150m` / `192Mi`). Tune it with
  `postgresql.primary.resources`.
- `BOW_DATABASE_URL` now points at the real Service name when the release name
  itself contains `postgresql` (previously `<release>-postgresql`, which did not
  exist in that case).

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

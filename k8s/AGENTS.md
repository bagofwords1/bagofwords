# AGENTS Guidelines for Kubernetes layer

### Purpose
Concise overview of `@k8s/` with emphasis on the Helm chart layout, how values map to Kubernetes resources, and common install/ops flows.

### Structure
- **k8s/**
  - `README.md`: Install/upgrade instructions with `helm` examples, secrets usage, SA annotations, node selectors.
  - `AGENTS.md`: This guide.
  - `chart/`: Helm chart for Bag of Words
    - `Chart.yaml`, `Chart.lock`: Chart metadata and lockfile.
    - `values.yaml`: Default configuration values.
    - `templates/`: Rendered into K8s manifests
      - `deployment.yml`: App `Deployment` (container, env, probes, resources).
      - `svc.yml`: App `Service` (ClusterIP by default).
      - `ingress.yml`: `Ingress` (TLS optional) routing to Service.
      - `config.yml`: `ConfigMap`/config template for app env defaults.
      - `sa.yml`: `ServiceAccount` (optional annotations via values).
    - `charts/`: Vendored dependencies
      - `postgresql-<version>.tgz`: Bitnami PostgreSQL subchart.
    - `index.yaml`: Helm repo index (only used when packaging/publishing).

### Components and responsibilities
- **Deployment**: Runs the web app container; consumes env from `ConfigMap` and optional `Secret`.
- **Service**: Stable DNS for the app (`ClusterIP`).
- **Ingress**: Exposes the app at `host` with optional TLS. Integrates with your ingress controller/cert-manager.
- **Config**: Default env via `ConfigMap`. A user-provided `Secret` can override specific keys.
- **ServiceAccount**: Optional annotations for IAM/workload identity.
- **PostgreSQL (subchart)**: Optional in-cluster DB via Bitnami chart; can also point to an external DB.

### Configuration model (key `values.yaml` paths)
- **Hostname & TLS**
  - `host`: Public hostname used in `Ingress`.
  - `ingress.tls`: `true|false` to enable TLS on `Ingress`.
- **PostgreSQL**
  - `postgresql.auth.username`, `postgresql.auth.password`, `postgresql.auth.database`: In-cluster DB credentials.
  - `postgresql.auth.existingSecret`: Use an existing secret instead of inline values.
- **App configuration & secrets**
  - `config.secretRef`: Name of an existing `Secret` whose keys override defaults from the `ConfigMap`.
  - Environment keys commonly used by the app (set via Secret):
    - `BOW_DATABASE_URL`, `BOW_BASE_URL`, `BOW_ENCRYPTION_KEY`
    - Google OAuth: `BOW_GOOGLE_AUTH_ENABLED`, `BOW_GOOGLE_CLIENT_ID`, `BOW_GOOGLE_CLIENT_SECRET`
    - Signup/options: `BOW_ALLOW_UNINVITED_SIGNUPS`, `BOW_ALLOW_MULTIPLE_ORGANIZATIONS`, `BOW_VERIFY_EMAILS`
    - SMTP: `BOW_SMTP_HOST`, `BOW_SMTP_PORT`, `BOW_SMTP_USERNAME`, `BOW_SMTP_PASSWORD`, `BOW_SMTP_FROM_NAME`, `BOW_SMTP_FROM_EMAIL`, `BOW_SMTP_USE_TLS`, `BOW_SMTP_USE_SSL`, `BOW_SMTP_USE_CREDENTIALS`, `BOW_SMTP_VALIDATE_CERTS`
- **ServiceAccount**
  - `serviceAccount.annotations`: Arbitrary annotations map.
- **Scheduling**
  - Node selectors/affinity/tolerations can be set (example in README shows `postgresql.primary.nodeSelector`).

### Install and upgrade (helm)
- Add repo and update:
  - `helm repo add bow https://bagofwords.com/helm`
  - `helm repo update`
- Install with managed PostgreSQL:
  - `helm upgrade -i -n <namespace> <release> bow/bagofwords --set postgresql.auth.username=<PG-USER> --set postgresql.auth.password=<PG-PASS> --set postgresql.auth.database=<PG-DB>`
- Install without TLS and custom hostname:
  - `helm upgrade -i -n <namespace> <release> bow/bagofwords --set host=<HOST> --set postgresql.auth.username=<PG-USER> --set postgresql.auth.password=<PG-PASS> --set postgresql.auth.database=<PG-DB> --set ingress.tls=false`
- Install with TLS and Google OAuth enabled (cert-manager assumed):
  - `helm upgrade -i -n <namespace> <release> bow/bagofwords --set host=<HOST> --set postgresql.auth.username=<PG-USER> --set postgresql.auth.password=<PG-PASS> --set postgresql.auth.database=<PG-DB> --set config.googleOauthEnabled=true --set config.googleClientId=<CLIENT_ID> --set config.googleClientSecret=<CLIENT_SECRET>`

### Using an existing Secret
- Create `Secret` with only the keys you want to override; these values take precedence over the chart `ConfigMap` defaults.
- Example flags when installing from local chart sources:
  - `--set postgresql.auth.existingSecret=<secret-name>` and `--set config.secretRef=<secret-name>`

### ServiceAccount annotations
- Via flags: `--set serviceAccount.annotations.foo=bar`
- Or in `values.yaml` under `serviceAccount.annotations`.

### Node selectors
- For PostgreSQL primary, e.g.: `--set postgresql.primary.nodeSelector.'kubernetes\.io/hostname'=kind-control-plane`
- You can set selectors in `values.yaml` as needed for app and DB.

### Operations and debugging
- Check release status: `helm status <release> -n <namespace>`
- Inspect final values: `helm get values <release> -n <namespace>`
- List resources: `kubectl get all -n <namespace>`
- Describe pods: `kubectl describe pod <pod> -n <namespace>`
- Logs: `kubectl logs <pod> -n <namespace>`
- Port-forward for local testing: `kubectl port-forward svc/<service> 8080:80 -n <namespace>`

### Adding or evolving chart features (quick checklist)
1. Model new settings in `values.yaml` with sensible defaults.
2. Template changes in `templates/*.yml` using those values; keep resources decoupled and configurable.
3. Update `README.md` with new flags/examples; keep Secrets vs ConfigMap precedence clear.
4. If adding deps, vendor under `charts/` and update `Chart.lock`.
5. Validate with `helm template` and test with a kind/minikube cluster before publishing.


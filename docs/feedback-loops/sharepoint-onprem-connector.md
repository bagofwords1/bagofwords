# Feedback Loop — SharePoint Server connector, localhost to real EC2

Request: build an on-premises SharePoint connector with the existing SharePoint
file workflow, and prove it through localhost with screenshots. Implemented as
`sharepoint_onprem`, separate from Graph. NTLM is live-verified; live Kerberos
and native indexed-content search acceptance remain environment-dependent.

## Validated failures and fixes

1. The registry had no on-premises type. The initial connector-contract test
   failed with `KeyError: 'sharepoint_onprem'`. Added the REST client, separate
   config/auth schemas, registry entry, dependency, and file-tool/UI wiring.
2. A real-model first-turn test read the CSV but `create_data` failed because
   it received a SharePoint path instead of the materialized session-file UUID.
   `read_file.py:1011` now carries that UUID explicitly in the model observation.
   The regression failed for both short and longer tabular reads before the
   change (`None == <session UUID>`); both pass after it. A fresh real-model
   report now searches, reads, and creates the chart without a corrective prompt.
3. The legacy per-user credential form wrote datasource-scoped credentials,
   while runtime resolved connection-scoped credentials. The member test stayed
   `connect_required` after Save. The on-prem form now saves through the guarded
   connection endpoint (`routes/connection.py`,
   `ConnectionService.save_windows_user_credentials`). Other auth flows are
   unchanged. The password input also honors the schema's password UI hint.
4. A live non-admin Files panel returned no connections: it called the
   manage-only `/data_sources/{id}/connections` endpoint and received 403.
   The new `/file-connections` view endpoint returns only allowlisted file-scope
   metadata. The admin endpoint still requires manage permission. Both the
   Files panel and tree use the view projection.
5. Pre-save connection probing stripped an empty library name, accidentally
   selecting `*` instead of the default library. The parameter constructor now
   preserves this meaningful empty value for this connector. The API regression
   compares pre-save and persisted catalog counts.

## Loop A — deterministic, no third-party credentials

Use Python 3.12 and a fresh checkout/sandbox with a valid development config:

```bash
cd backend
uv sync --frozen --extra dev --extra kerberos
TESTING=true uv run pytest \
  tests/unit/test_sharepoint_onprem_client.py \
  tests/unit/test_graph_drive_multi_library.py \
  tests/unit/test_graph_drive_page_range.py \
  tests/unit/test_file_tools.py \
  tests/unit/test_read_file_session_files.py \
  tests/unit/test_read_file_observation_content.py \
  tests/e2e/test_sharepoint_onprem.py \
  tests/e2e/test_connection.py \
  tests/e2e/test_data_source.py \
  --db=sqlite -q --disable-warnings
```

The REST boundary is simulated; connector parsing, registry, routes, services,
permissions, encrypted credential storage, and test DB are real. Accounts and
connections are seeded through existing HTTP fixtures. Kerberos's external
GSSAPI boundary is simulated to verify mechanism selection, required mutual
authentication, missing-dependency failure, context renewal, and no global
credential-environment mutation. This is not proof of an AD ticket exchange.

Checks include library/default/all selection, recursion, scope and glob denial
on direct reads, literal apostrophe/hash/percent filenames, catalog/download
limits, pagination-origin rejection, provider error handling, CSV/Excel and PDF
pages, transient read retry, admin/member boundaries, credential save/delete,
and preventing a member fallback to system credentials. Generic Graph and
file-tool tests protect existing SharePoint behavior.

Final combined run: **123 passed**, including the on-prem API suite's **4
tests** and the default-library and member-view regression checks.
Six modified Vue SFCs also passed parser/script/template compilation. No
production frontend build or production Docker image build is claimed.

## Loop B — real SharePoint Server

Use a disposable site and an account permitted to create a test folder. The
verification script uploads only with explicit `--seed`, into a unique
`BOW-QA-<random>` folder, using `overwrite=false`. It never deletes anything.
The connector itself makes only GET requests.

```bash
cd backend
# Set SHAREPOINT_TEST_PASSWORD securely in the environment, or use the hidden
# password prompt. Never place the password in command-line arguments.
uv run python ../tools/sharepoint_onprem/verify_live.py \
  --site-url https://sharepoint.example.test/sites/bowtest \
  --username 'LAB\reader' --seed

# Recheck the generated folder without creating another one:
uv run python ../tools/sharepoint_onprem/verify_live.py \
  --site-url https://sharepoint.example.test/sites/bowtest \
  --username 'LAB\reader' --folder BOW-QA-<returned-suffix>
```

For an isolated HTTP-only lab, explicitly add `--allow-http`. The tested
disposable SharePoint Server site used Windows NTLM. The generated folder was a
unique `Documents/BOW-QA-<random>` folder; original user-uploaded files were
left untouched.

Observed passes:

| Check | Result |
|---|---|
| Site/current-user and library probe | Authenticated successfully |
| Scoped catalog | 8 synthetic files |
| CSV | North 1200, South 800, West 1500; total 3500 |
| XLSX | Sales sheet; Targets sheet target 4000 |
| PDF page range | Page 2 of 2; reimbursement 450, Finance approval |
| DOCX / PPTX | Finance owner, 3500 budget / green readiness |
| TXT / JSON / special filename | Parsed successfully; exact filename roundtrip |
| Original downloads | All 8 files readable; seeded bytes compared on upload run |
| Scope restriction | Non-CSV direct read denied under `*.csv` |
| Default library | Same 8 scoped files |
| Filename search | `policy` returns policy.txt immediately |
| Standard real-client integration harness | 1 passed, 18 other types deselected |

The standard harness supports environment variables without editing
`integrations.json`: `SHAREPOINT_TEST_SITE_URL`, `SHAREPOINT_TEST_USERNAME`,
`SHAREPOINT_TEST_PASSWORD`, `SHAREPOINT_TEST_LIBRARY`, `SHAREPOINT_TEST_FOLDER`,
and `SHAREPOINT_TEST_ALLOW_HTTP=true` for the isolated lab only. Run:

```bash
TESTING=true uv run pytest tests/integrations/ds_clients.py \
  -k sharepoint_onprem -q --disable-warnings
```

## Loop C — live UI and real LLM

In a fresh sandbox with free default ports:

```bash
tools/agent/boot_stack.sh
cd backend
uv run python ../tools/agent/seed_org.py
```

Configure a real supported LLM through Settings (never a mocked endpoint), then:

1. Add **SharePoint Server (on-prem)**; scope it to the generated fixture folder;
   enter Windows credentials; Test reports 8 files; save and attach to an agent.
2. Verify the file catalog and displayed site/library/folder scope.
3. Start a new report and ask:
   “Read sales.csv, calculate revenue by region and total revenue, and create a
   saved bar chart. Cite the source file. Do not change the source files.”
4. Verify the actual saved chart values (1500, 1200, 800), not just the final
   prose. The observed tool sequence is search → read → successful create_data.
5. Ask to compare policy.txt with PDF page 2 and read briefing.docx/status.pptx.
   Verify 450 euros, Finance, budget 3500 and green readiness, with file references.
6. Invite a regular member to a public test agent with a user-required NTLM
   connection. Verify no access before Connect; Test and Save their credentials;
   verify live file browsing; verify disconnect blocks further reads.

On the developer's Mac the existing app already occupied ports 3000/8000. It
was left running. QA used a separate migrated SQLite DB at
`/private/tmp/bow-sharepoint-onprem.db`, a backend on **8011**, and a loopback UI
proxy on **3012** (Nuxt assets from 3000, `/api` to 8011). This is an isolated
workspace, not a change to the original app's database or LLM configuration.
The development config/encryption key must remain stable across QA restarts.

Real-model UI verification used **GPT-5.4** and only the synthetic folder. The
sales file was byte-verified before the provider call. No original customer
documents were sent to the model. The member UI used a separate BOW member with
the authorized lab Windows account; distinct Windows identities and denied
responses are covered deterministically, not claimed as two real AD users.

## Evidence

Artifacts are under `media/pr/sharepoint-onprem-connector/`:

- `chart-final.png`: fresh-report real-model saved revenue chart.
- `documents-verified.png`: document answers and file references.

The remaining diagnostic screenshots stay only in the local QA workspace: they
may show ephemeral lab infrastructure identifiers and are intentionally not
versioned.

The first failed report is retained only in the local QA environment as
diagnostic evidence; the fresh fixed report is available there as well.

## Boundaries / remaining acceptance

- **Live Kerberos not verified.** The supplied SharePoint web application
  advertises NTLM only. Need private KDC reachability, correct HTTP SPN/IIS
  setup, and a client keytab/ticket cache. Service authentication is implemented;
  per-user Kerberos constrained delegation is not.
- **Native indexed-content hits not live verified.** Searching the unique
  synthetic policy content returned no hits. Filename search and direct reads
  work; the farm needs a crawl and matching search URLs before content-index
  acceptance. No farm settings were changed by this work.
- Native authentication, scope enforcement, parsing, and report workflows are
  verified as described; not every SharePoint version, file format, proxy,
  customer CA, large-library scale, or key-rotation scenario is proven.
- The overview's existing file counter counts directly uploaded agent files;
  remote library files are shown in the Files panel's live count.
- Synthetic files and local QA accounts remain for manual retesting. No
  customer files, containers, AD settings, firewall rules, or existing reports
  were deleted. No PR or commit was created.

See [deployment/authentication requirements](../sharepoint-server.md), including
the Helm values hooks and keytab ownership. No Helm-template change is needed.

# Configuration Manager (SCCM / MECM) — simulator and lab verification

Configuration Manager needs Windows Server, SQL Server and Active Directory, so
there is no container or SaaS sandbox for it. This folder has two tools:

| File | What it is |
|---|---|
| `mock_adminservice.py` | Protocol simulator of the AdminService REST API (OData v4) over **HTTPS with a self-signed certificate** and a **real NTLM handshake**. Seeded Contoso estate: site PS1, 80 devices in 3 offices, collections, hardware/software inventory, applications, deployments, software updates and compliance. Used by `backend/tests/unit/test_mecm_client.py` and the UI loop. |
| `lab_probe.py` | Read-only probe of a **real** site. Checks every assumption the connector makes (classes, keys, paging, OData options, lazy properties, error shapes, case rules) and prints a reconciliation report. |

## Run the simulator

```bash
uv run --project backend python tools/mecm/mock_adminservice.py
# https://localhost:8443/AdminService — the certificate PEM is written to tools/mecm/.certs/adminservice.pem
```

Accounts (password `Passw0rd!` for all; `DOMAIN\user` and `user@contoso.lab` both work):

| Account | Behaviour |
|---|---|
| `CONTOSO\svc_bow` | Full administrative user |
| `CONTOSO\helpdesk_lon` | Security scope limited to London devices |
| `CONTOSO\notadmin` | Authenticates, but is not a Configuration Manager admin → 403 |

Knobs (env, or set the module attributes from tests): `MECM_MOCK_PORT`,
`MECM_MOCK_PAGE_SIZE` (default 100), `MECM_MOCK_REJECT_TOP=1` (a route without
`$top`), `MECM_MOCK_NEXTLINK_ORIGIN` (`relative` or another host, to test
paging-link handling).

## Verify against a real lab

1. **Stand up a lab.** The quickest route is Microsoft's Azure quickstart
   template ["Configuration Manager current branch"](https://learn.microsoft.com/en-us/samples/azure/azure-quickstart-templates/sccm-currentbranch/)
   (domain controller, primary site with SQL Server, DP/MP, a Windows client;
   2–4 hours to provision, evaluation licence). The Microsoft Intune and
   Configuration Manager Evaluation Lab Kit (Hyper-V) also works.
2. **Network.** The template's network security group allows only RDP. Open
   TCP 443 on the primary site server (`<prefix>PS01`, which hosts the SMS
   Provider) to the address Bag of Words connects from — or run Bag of Words
   inside the same virtual network.
3. **Account.** In the console: Administration › Security › Administrative
   Users › Add, pick an AD account and give it **Read-only Analyst**.
4. **Certificate.** On PS01, in PowerShell, export the certificate bound to 443
   (the connect form shows the same command):
   ```powershell
   $hash = ((netsh http show sslcert ipport=0.0.0.0:443) -match 'Certificate Hash' -split ':\s*')[-1].Trim()
   $c = Get-Item "Cert:\LocalMachine\My\$hash"
   '-----BEGIN CERTIFICATE-----'; [Convert]::ToBase64String($c.RawData, 'InsertLineBreaks'); '-----END CERTIFICATE-----'
   ```
   The certificate's subject is the server's FQDN, so connect with that name
   (add a hosts-file entry pointing it at the public IP if needed).
5. **Probe.** Credentials go in env vars only:
   ```bash
   cd backend
   MECM_URL=ps01.contoso.com MECM_USER='CONTOSO\svc_bow' MECM_PASSWORD='...' \
   MECM_CA_FILE=/path/to/ps01.pem uv run python ../tools/mecm/lab_probe.py --json lab-report.json
   ```
   Every `FAIL` is a mismatch between the real site and the connector; every
   `note` is an observation worth folding back into the simulator.
6. **Connector.** Create the connection in the app (Add connection ›
   *Microsoft Configuration Manager (SCCM)*), or run the integration test with
   a local `backend/tests/integrations/integrations.json` (never committed):
   ```json
   {"mecm": {"enabled": true, "server_url": "ps01.contoso.com",
             "username": "CONTOSO\\svc_bow", "password": "...",
             "ca_certificate": "-----BEGIN CERTIFICATE-----\n..."}}
   ```
   ```bash
   cd backend && uv run pytest tests/integrations/ds_clients.py -k mecm -v
   ```

What is still unverified on a real site is listed in
`docs/feedback-loops/mecm-connector.md`.

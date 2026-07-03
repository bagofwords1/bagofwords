#!/bin/bash
# Provision (once) and run a Samba AD Domain Controller for the Kerberos lab.
#
# Creates:
#   - realm BOWLAB.LOCAL, domain BOWLAB
#   - service account svc-bow      (the app's identity / S4U impersonator)
#   - SQL service accounts mssql2022 / mssql2019 with MSSQLSvc SPNs
#   - test users alice / bob
#   - constrained delegation (protocol transition, S4U2Self+S4U2Proxy) from
#     svc-bow to both MSSQLSvc SPNs
#   - DNS A + PTR records so Kerberos hostname/SPN resolution works both ways
#   - keytabs exported to /keytabs (shared volume): svc-bow, mssql2022, mssql2019
set -euo pipefail

REALM="BOWLAB.LOCAL"
DOMAIN="BOWLAB"
ADMIN_PASS="${ADMIN_PASS:-Bowlab#Admin1}"
USER_PASS="${USER_PASS:-Bowlab#User1}"
SVC_PASS="${SVC_PASS:-Bowlab#Svc1}"

DC_IP="${DC_IP:-172.28.0.10}"
SQL2022_IP="${SQL2022_IP:-172.28.0.22}"
SQL2019_IP="${SQL2019_IP:-172.28.0.19}"

KEYTAB_DIR="/keytabs"
PROVISIONED_MARKER="/var/lib/samba/.provisioned"

log() { echo "[dc] $*"; }

provision() {
    log "Provisioning domain ${REALM} ..."
    rm -f /etc/samba/smb.conf
    samba-tool domain provision \
        --use-rfc2307 \
        --domain="${DOMAIN}" \
        --realm="${REALM}" \
        --server-role=dc \
        --dns-backend=SAMBA_INTERNAL \
        --adminpass="${ADMIN_PASS}" \
        --option="dns forwarder=127.0.0.11"

    # Samba writes a realm krb5.conf; make it the system default.
    cp /var/lib/samba/private/krb5.conf /etc/krb5.conf

    # Passwords never expire in the lab, and relax complexity/history.
    samba-tool domain passwordsettings set --complexity=off --history-length=0 \
        --min-pwd-age=0 --max-pwd-age=0 >/dev/null

    log "Starting samba to configure directory objects ..."
    samba -D
    sleep 5

    # --- accounts ---------------------------------------------------------
    log "Creating accounts ..."
    samba-tool user create svc-bow    "${SVC_PASS}"  --description="BoW app service account"
    samba-tool user create mssql2022  "${SVC_PASS}"  --description="SQL Server 2022 service account"
    samba-tool user create mssql2019  "${SVC_PASS}"  --description="SQL Server 2019 service account"
    samba-tool user create alice      "${USER_PASS}" --given-name=Alice --surname=Analyst
    samba-tool user create bob        "${USER_PASS}" --given-name=Bob   --surname=Builder

    # SQL accounts must not have expiring passwords (keytab would break).
    for u in svc-bow mssql2022 mssql2019 alice bob; do
        samba-tool user setexpiry "$u" --noexpiry >/dev/null
    done

    # --- SPNs for the SQL service accounts --------------------------------
    log "Registering MSSQLSvc SPNs ..."
    samba-tool spn add "MSSQLSvc/sql2022.bowlab.local"      mssql2022
    samba-tool spn add "MSSQLSvc/sql2022.bowlab.local:1433" mssql2022
    samba-tool spn add "MSSQLSvc/sql2019.bowlab.local"      mssql2019
    samba-tool spn add "MSSQLSvc/sql2019.bowlab.local:1433" mssql2019

    # The middle-tier (impersonating) account must itself have an SPN for the
    # KDC to issue it an S4U2Self evidence ticket.
    samba-tool spn add "bow/svc-bow.bowlab.local" svc-bow

    # --- constrained delegation on the app account ------------------------
    # protocol transition (S4U2Self "any protocol") + allowed targets (S4U2Proxy).
    log "Configuring constrained delegation for svc-bow ..."
    samba-tool delegation for-any-protocol svc-bow on
    samba-tool delegation add-service svc-bow "MSSQLSvc/sql2022.bowlab.local:1433"
    samba-tool delegation add-service svc-bow "MSSQLSvc/sql2019.bowlab.local:1433"
    samba-tool delegation add-service svc-bow "MSSQLSvc/sql2022.bowlab.local"
    samba-tool delegation add-service svc-bow "MSSQLSvc/sql2019.bowlab.local"

    # --- DNS records (forward + reverse) ----------------------------------
    log "Adding DNS records ..."
    local rev="28.172.in-addr.arpa"
    samba-tool dns add 127.0.0.1 "${REALM}" sql2022 A "${SQL2022_IP}" -U "Administrator%${ADMIN_PASS}" || true
    samba-tool dns add 127.0.0.1 "${REALM}" sql2019 A "${SQL2019_IP}" -U "Administrator%${ADMIN_PASS}" || true
    samba-tool dns zonecreate 127.0.0.1 "${rev}" -U "Administrator%${ADMIN_PASS}" || true
    samba-tool dns add 127.0.0.1 "${rev}" "${SQL2022_IP##*.}.0" PTR sql2022.bowlab.local -U "Administrator%${ADMIN_PASS}" || true
    samba-tool dns add 127.0.0.1 "${rev}" "${SQL2019_IP##*.}.0" PTR sql2019.bowlab.local -U "Administrator%${ADMIN_PASS}" || true

    # --- export keytabs ---------------------------------------------------
    log "Exporting keytabs to ${KEYTAB_DIR} ..."
    mkdir -p "${KEYTAB_DIR}"
    rm -f "${KEYTAB_DIR}"/*.keytab
    samba-tool domain exportkeytab "${KEYTAB_DIR}/svc-bow.keytab" --principal="svc-bow@${REALM}"

    # SQL keytabs must carry the SPN principals (that is what clients request).
    samba-tool domain exportkeytab "${KEYTAB_DIR}/mssql2022.keytab" --principal="MSSQLSvc/sql2022.bowlab.local@${REALM}"
    samba-tool domain exportkeytab "${KEYTAB_DIR}/mssql2022.keytab" --principal="MSSQLSvc/sql2022.bowlab.local:1433@${REALM}"
    samba-tool domain exportkeytab "${KEYTAB_DIR}/mssql2022.keytab" --principal="mssql2022@${REALM}"
    samba-tool domain exportkeytab "${KEYTAB_DIR}/mssql2019.keytab" --principal="MSSQLSvc/sql2019.bowlab.local@${REALM}"
    samba-tool domain exportkeytab "${KEYTAB_DIR}/mssql2019.keytab" --principal="MSSQLSvc/sql2019.bowlab.local:1433@${REALM}"
    samba-tool domain exportkeytab "${KEYTAB_DIR}/mssql2019.keytab" --principal="mssql2019@${REALM}"
    chmod 0644 "${KEYTAB_DIR}"/*.keytab

    # Write the client krb5.conf into the shared volume. forwardable=true is
    # REQUIRED: S4U2Proxy is refused unless the S4U2Self evidence ticket is
    # forwardable, which in turn needs a forwardable service TGT.
    cat > "${KEYTAB_DIR}/krb5.conf" <<EOF
[libdefaults]
    default_realm = ${REALM}
    dns_lookup_realm = false
    dns_lookup_kdc = true
    forwardable = true
    rdns = false

[realms]
    ${REALM} = {
        kdc = dc.bowlab.local
        admin_server = dc.bowlab.local
        default_domain = bowlab.local
    }

[domain_realm]
    .bowlab.local = ${REALM}
    bowlab.local = ${REALM}
EOF

    log "Stopping bootstrap samba ..."
    pkill -TERM samba || true
    sleep 3
    touch "${PROVISIONED_MARKER}"
    log "Provisioning complete."
}

if [ ! -f "${PROVISIONED_MARKER}" ]; then
    provision
else
    log "Already provisioned; reusing directory."
    cp /var/lib/samba/private/krb5.conf /etc/krb5.conf
    mkdir -p "${KEYTAB_DIR}"
    cp /etc/krb5.conf "${KEYTAB_DIR}/krb5.conf" || true
fi

log "Starting Samba AD DC in foreground ..."
exec samba -i -s /etc/samba/smb.conf

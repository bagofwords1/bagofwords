#!/bin/bash
# Configure a SQL Server on Linux container for Active Directory (Kerberos)
# authentication against the lab Samba DC, then create AD logins.
#
# Runs as the container entrypoint wrapper: it configures krb5 + the mssql
# keytab, launches sqlservr, waits for it, applies init.sql, and then execs
# sqlservr in the foreground.
#
# Env:
#   MSSQL_FQDN        e.g. sql2022.bowlab.local
#   MSSQL_KEYTAB      path to this instance's keytab (mounted, from the DC)
#   MSSQL_AD_ACCOUNT  privileged AD account name, e.g. mssql2022
#   SA_PASSWORD       sa password
set -uo pipefail

REALM="BOWLAB.LOCAL"
: "${MSSQL_FQDN:?}"; : "${MSSQL_KEYTAB:?}"; : "${MSSQL_AD_ACCOUNT:?}"; : "${SA_PASSWORD:?}"

log() { echo "[mssql-setup:${MSSQL_FQDN}] $*"; }

# Wait for the DC-exported krb5.conf + this instance's keytab to appear.
for i in $(seq 1 60); do
    [ -f /keytabs/krb5.conf ] && [ -f "${MSSQL_KEYTAB}" ] && break
    sleep 2
done
cp /keytabs/krb5.conf /etc/krb5.conf 2>/dev/null || true

DOMAIN="bowlab.local"
ADMIN_PASS="${ADMIN_PASS:-Bowlab#Admin1}"

# --- join the domain so SQL can resolve AD users (name -> SID) -------------
# Windows logins and Kerberos ticket->principal mapping require SSSD name
# resolution; the keytab alone only handles the crypto.
log "Joining domain ${DOMAIN} via SSSD ..."
echo "${ADMIN_PASS}" | adcli join --stdin-password --domain="${DOMAIN}" \
    --domain-controller="dc.${DOMAIN}" --login-user=Administrator 2>&1 \
    | sed "s/^/[mssql-setup:${MSSQL_FQDN}] /" || log "adcli join reported an issue (continuing)"

cat > /etc/sssd/sssd.conf <<EOF
[sssd]
domains = ${DOMAIN}
config_file_version = 2
services = nss, pam

[domain/${DOMAIN}]
id_provider = ad
auth_provider = ad
access_provider = ad
ad_domain = ${DOMAIN}
krb5_realm = ${REALM}
realmd_tags = manages-system joined-with-adcli
cache_credentials = True
ldap_id_mapping = True
use_fully_qualified_names = False
fallback_homedir = /home/%u@%d
default_shell = /bin/bash
EOF
chmod 600 /etc/sssd/sssd.conf

# Point NSS at sss so `id BOWLAB\alice` resolves.
sed -i 's/^passwd:.*/passwd:         files sss/' /etc/nsswitch.conf
sed -i 's/^group:.*/group:          files sss/'  /etc/nsswitch.conf

log "Starting SSSD ..."
sssd -D 2>/dev/null || service sssd start 2>/dev/null || true
sleep 3
if id "alice@${DOMAIN}" >/dev/null 2>&1; then
    log "AD name resolution OK ($(id -u "alice@${DOMAIN}"):alice)"
else
    log "WARNING: AD name resolution not working yet; Windows logins may fail."
fi

# Install the instance keytab where SQL Server expects a private copy.
# The secrets dir doesn't exist until first boot, so create it.
mkdir -p /var/opt/mssql/secrets
install -m 0600 -o mssql -g root "${MSSQL_KEYTAB}" /var/opt/mssql/secrets/mssql.keytab

# Point SQL Server at the keytab + privileged AD account (Kerberos).
/opt/mssql/bin/mssql-conf set network.kerberoskeytabfile /var/opt/mssql/secrets/mssql.keytab
/opt/mssql/bin/mssql-conf set network.privilegedadaccount "${MSSQL_AD_ACCOUNT}"
/opt/mssql/bin/mssql-conf set network.forceencryption 0

log "Starting sqlservr (as mssql user) ..."
# SQL Server refuses to run as root; the setup above needed root for the
# keytab + mssql-conf, so drop privileges to run the engine.
chown mssql:root /var/opt/mssql/secrets/mssql.keytab
su mssql -s /bin/bash -c "MSSQL_SA_PASSWORD='${SA_PASSWORD}' ACCEPT_EULA=Y /opt/mssql/bin/sqlservr" &
SQL_PID=$!

# Wait for SQL to accept connections.
SQLCMD="/opt/mssql-tools18/bin/sqlcmd"
[ -x "$SQLCMD" ] || SQLCMD="/opt/mssql-tools/bin/sqlcmd"
for i in $(seq 1 60); do
    "$SQLCMD" -S localhost -U sa -P "${SA_PASSWORD}" -No -Q "SELECT 1" >/dev/null 2>&1 && break
    sleep 2
done

log "Applying init.sql ..."
"$SQLCMD" -S localhost -U sa -P "${SA_PASSWORD}" -No -i /init.sql 2>&1 | sed "s/^/[mssql-setup:${MSSQL_FQDN}] /" || true

log "Ready. auth_scheme for AD logins will be KERBEROS."
wait "$SQL_PID"

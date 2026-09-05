#!/bin/bash
# Runner entrypoint: wait for the DC keytabs, install the lab krb5.conf, then
# run the delegation test suite against the production bagofwords code
# (mounted at /app).
set -uo pipefail

log() { echo "[runner] $*"; }

for i in $(seq 1 90); do
    [ -f /keytabs/svc-bow.keytab ] && [ -f /keytabs/krb5.conf ] && break
    sleep 2
done
cp /keytabs/krb5.conf /etc/krb5.conf 2>/dev/null || true

log "krb5.conf:"; sed 's/^/[runner]   /' /etc/krb5.conf

export PYTHONPATH=/app
export KRB5_CONFIG=/etc/krb5.conf
# The service keytab our KerberosTicketManager initiates from.
export KRB5_CLIENT_KTNAME=/keytabs/svc-bow.keytab

log "Running delegation test suite ..."
exec python -m pytest /lab/test_delegation.py -v -rs --no-header "$@"

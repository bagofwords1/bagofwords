# Samba Active Directory Domain Controller for the Kerberos lab.
# Acts as the KDC + LDAP + DNS that SQL Server joins and against which the app
# performs S4U2Self / S4U2Proxy constrained delegation.
ARG BASE_IMAGE=ubuntu:24.04
FROM ${BASE_IMAGE}
USER root

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      samba samba-ad-provision samba-ad-dc samba-dsdb-modules smbclient winbind \
      krb5-user krb5-config \
      dnsutils iproute2 procps ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# samba-tool provisions its own krb5.conf; don't let the krb5-user default win.
RUN rm -f /etc/krb5.conf

COPY provision.sh /usr/local/bin/provision.sh
RUN chmod +x /usr/local/bin/provision.sh

EXPOSE 53 53/udp 88 88/udp 389 445 464 636
ENTRYPOINT ["/usr/local/bin/provision.sh"]

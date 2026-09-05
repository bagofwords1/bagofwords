# SQL Server on Linux with Kerberos libraries supplied by Microsoft's image.
# The version is passed as a build arg so the image version remains explicit.
ARG MSSQL_TAG=2022-latest
FROM mcr.microsoft.com/mssql/server:${MSSQL_TAG}

USER root
ENV DEBIAN_FRONTEND=noninteractive

# SQL Server delegates Kerberos crypto to its keytab, but still needs SSSD to
# resolve AD login names to SIDs for CREATE LOGIN ... FROM WINDOWS.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      sssd sssd-ad sssd-tools adcli realmd \
      krb5-user libnss-sss libpam-sss \
      samba-common-bin ldap-utils dnsutils \
    && rm -rf /var/lib/apt/lists/*

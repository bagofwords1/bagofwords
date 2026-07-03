# SQL Server on Linux + the Active Directory client stack needed for Windows
# (Kerberos) logins. SQL Server can only CREATE LOGIN ... FROM WINDOWS and map
# incoming Kerberos tickets to a principal once the host can resolve AD
# users/groups via SSSD, so the image bakes in the join tooling. The version is
# passed as a build arg so the same Dockerfile builds 2022 and 2019.
ARG MSSQL_TAG=2022-latest
FROM mcr.microsoft.com/mssql/server:${MSSQL_TAG}

USER root
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      sssd sssd-ad sssd-tools adcli realmd \
      krb5-user libnss-sss libpam-sss \
      samba-common-bin ldap-utils dnsutils gosu \
    && rm -rf /var/lib/apt/lists/*

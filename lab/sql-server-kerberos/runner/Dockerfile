# Test runner: krb5 + python-gssapi + the Microsoft ODBC driver, plus the
# bagofwords Kerberos module under test. Mirrors the production image's krb5
# stack so the S4U path is exercised exactly as it runs in the app.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      python3 python3-venv python3-dev build-essential \
      krb5-user libkrb5-dev libgssapi-krb5-2 \
      unixodbc unixodbc-dev curl ca-certificates gnupg \
    && curl -sSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg && \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/ubuntu/24.04/prod noble main" > /etc/apt/sources.list.d/mssql-release.list && \
    apt-get update && \
    ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 && \
    rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir "gssapi>=1.8.3,<2" pyodbc sqlalchemy pandas pytest pydantic

COPY run-tests.sh /usr/local/bin/run-tests.sh
RUN chmod +x /usr/local/bin/run-tests.sh
ENTRYPOINT ["/usr/local/bin/run-tests.sh"]

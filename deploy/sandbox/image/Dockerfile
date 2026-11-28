# bagofwords/sandbox: the image of the built-in "Default" sandbox environment.
#
# Python with the packages data work usually needs, so that an environment
# with no internet access (the default) is useful as it is. Nothing here is
# specific to BoW: sandboxd is mounted into the container by the host, and
# any other image works as an environment image too.
#
# A sandbox runs as uid 1000 with a read-only root filesystem; /workspace is
# its size-limited home, and what a run installs with `pip install --user`
# lands there and persists.
FROM python:3.12-slim

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/matplotlib

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git unzip zip jq fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt && rm /tmp/requirements.txt

RUN useradd --uid 1000 --create-home --home-dir /home/sandbox --shell /bin/bash sandbox \
 && mkdir -p /workspace && chown 1000:1000 /workspace
USER 1000
WORKDIR /workspace

LABEL org.opencontainers.image.title="bagofwords agent sandbox" \
      org.opencontainers.image.source="https://github.com/bagofwords1/bagofwords"

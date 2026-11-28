# bagofwords/sandbox-tools: carries the sandboxd binary to a sandbox host.
#
# sandboxd is the open-source runtime daemon from kubernetes-sigs/agent-sandbox
# (Apache-2.0), built from the pinned upstream commit. Bootstrap copies
# /sandboxd out of this image to /var/lib/bow-sandbox/bin/sandboxd on the
# host; every sandbox mounts it read-only, so any image works as a sandbox
# image and none needs BoW-specific content.
#
# The commit must be the one app/services/sandbox/sandboxd_proto was
# generated from.
FROM golang:1.27.4 AS build
ARG SANDBOXD_COMMIT=b0040f5e51a4b98341af899e3ce9da767e63216f
ARG TARGETARCH
RUN git init -q /src && cd /src \
 && git remote add origin https://github.com/kubernetes-sigs/agent-sandbox.git \
 && git fetch -q --depth 1 origin ${SANDBOXD_COMMIT} && git checkout -q FETCH_HEAD
WORKDIR /src
RUN CGO_ENABLED=0 GOOS=linux GOARCH=${TARGETARCH} go build \
      -ldflags="-s -w \
        -X sigs.k8s.io/agent-sandbox/internal/version.gitVersion=bow-${SANDBOXD_COMMIT} \
        -X sigs.k8s.io/agent-sandbox/internal/version.gitSHA=${SANDBOXD_COMMIT}" \
      -o /sandboxd ./packages/sandboxd/cmd/sandboxd

FROM scratch
COPY --from=build /sandboxd /sandboxd
COPY --from=build /src/LICENSE /LICENSE.agent-sandbox
LABEL org.opencontainers.image.title="bagofwords sandbox tools (sandboxd)" \
      org.opencontainers.image.source="https://github.com/bagofwords1/bagofwords"

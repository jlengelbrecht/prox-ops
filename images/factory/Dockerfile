# syntax=docker/dockerfile:1
FROM --platform=linux/amd64 docker.io/library/debian:bookworm-slim@sha256:c67e92c8099ba22853ab4cfda7cfb494d253d6012ca82dee98ba8976772af018

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends bash ca-certificates coreutils git jq python3 procps util-linux && rm -rf /var/lib/apt/lists/*
RUN rm -f /etc/machine-id
RUN groupadd --gid 10001 factory && useradd --uid 10001 --gid 10001 --home-dir /home/factory --no-create-home --shell /bin/bash factory
COPY checksums.sha256 /tmp/checksums.sha256
COPY inputs/orca-server-linux-x64.tar.gz /tmp/orca-server-linux-x64.tar.gz
RUN cd / && sed 's@inputs/orca-server-linux-x64.tar.gz@tmp/orca-server-linux-x64.tar.gz@' /tmp/checksums.sha256 | sha256sum -c -
COPY safe-extract.py /opt/factory/safe-extract.py
RUN python3 /opt/factory/safe-extract.py /tmp/orca-server-linux-x64.tar.gz /opt/orca-server && test -x /opt/orca-server/bin/orca-ide && rm /tmp/orca-server-linux-x64.tar.gz /tmp/checksums.sha256
COPY inputs/afk/bin/ /opt/afk/bin/
COPY inputs/afk/lib/ /opt/afk/lib/
COPY inputs/afk/adapters/runtime-scope.sh /opt/afk/adapters/runtime-scope.sh
COPY poll-supervisor.sh /opt/factory/poll-supervisor.sh
RUN test -x /opt/afk/bin/agent-flow-kit && chmod 755 /opt/factory/poll-supervisor.sh && /opt/afk/bin/agent-flow-kit --help >/dev/null && /opt/orca-server/bin/orca-ide --version >/dev/null
ENV HOME=/home/factory XDG_CACHE_HOME=/cache XDG_STATE_HOME=/home/factory/.local/state TMPDIR=/scratch
USER 10001:10001
WORKDIR /home/factory
CMD ["/opt/orca-server/bin/orca-ide", "serve", "--bind", "127.0.0.1", "--port", "8765", "--refuse-public"]

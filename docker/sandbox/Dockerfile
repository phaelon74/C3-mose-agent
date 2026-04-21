# Isolated shell sandbox for TERMINAL_BACKEND=docker (mose-sandbox).
# Tools are baked in at build time; read_only root at runtime forbids apt-get.

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
      bash coreutils ca-certificates tini \
      curl wget openssl \
      bind9-dnsutils \
      iputils-ping iputils-tracepath traceroute \
      netcat-openbsd telnet \
      iproute2 net-tools procps \
      jq \
      openssh-client rsync \
      git \
      vim-tiny less \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /workspace

WORKDIR /workspace
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sleep", "infinity"]

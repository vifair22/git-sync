# syntax=docker/dockerfile:1.7
#
# git-sync container image.
# Build: `docker buildx build -t git-sync:latest .`
# Run:   see docker-compose.yml for the canonical invocation.

FROM python:3.14-slim

# `git` is used at runtime for clone/fetch/push-mirror; ca-certificates for HTTPS
# to GitLab and GitHub.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
           git git-filter-repo cloc ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ARG UID=1000
ARG GID=1000
RUN groupadd --system --gid ${GID} git-sync \
    && useradd  --system --uid ${UID} --gid ${GID} \
                --home-dir /var/lib/git-sync --shell /sbin/nologin git-sync

WORKDIR /opt/git-sync

COPY LICENSE pyproject.toml release_version ./
COPY scripts/ scripts/
COPY src/ src/

# Stamp the version BEFORE installing so the generated _version.py is baked
# into the wheel at install time. Otherwise the installed package in
# site-packages captures whatever _version.py was present when src/ was
# copied in (the host's stale stamp, or nothing at all).
RUN python scripts/stamp_version.py release \
    && pip install --no-cache-dir .

RUN mkdir -p /etc/git-sync /var/lib/git-sync/cache \
    && chown -R git-sync:git-sync /etc/git-sync /var/lib/git-sync

ENV GIT_SYNC_CONFIG=/etc/git-sync/config.toml \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

VOLUME ["/etc/git-sync", "/var/lib/git-sync"]

USER git-sync

ENTRYPOINT ["git-sync"]
CMD ["run"]

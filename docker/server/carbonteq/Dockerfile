# CarbonTeq dstack server, built from this fork's source.
#
# The upstream docker/server/release/Dockerfile installs `dstack` from PyPI by
# version and therefore does NOT contain the fork delta. This one builds the
# server (and the web UI) from the working tree.

FROM node:18-slim AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
ENV DSTACK_SERVER_LOG_FORMAT=json

WORKDIR /dstack-server

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        git \
        openssh-client \
        sqlite3 \
        tini \
    && rm -rf /var/lib/apt/lists/*

ADD https://astral.sh/uv/install.sh /uv-installer.sh
RUN sh /uv-installer.sh && rm /uv-installer.sh
ENV PATH="/root/.local/bin/:$PATH"

COPY pyproject.toml README.md ./
COPY src src
COPY skills skills
# hatchling ships src/dstack/_internal/server/statics/** as a wheel artifact,
# so dropping the built UI here is enough to get it into the install.
COPY --from=frontend /frontend/build src/dstack/_internal/server/statics

RUN uv pip install --system ".[all]"

EXPOSE 3000

ENTRYPOINT ["/usr/bin/tini", "--", "dstack", "server", "--host", "0.0.0.0", "--port", "3000"]

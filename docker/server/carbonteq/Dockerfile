# CarbonTeq dstack server.
#
# Follows the production pattern from ai-infra: start from the digest-pinned
# upstream image and replace ONLY the project wheel. The dependency graph then
# comes from upstream's immutable, already-qualified image rather than being
# re-resolved on every build.
#
# Differs from production in one way: the upstream web UI is preserved across
# the wheel swap. A source-built wheel contains no statics, so a plain
# --reinstall would uninstall upstream's and leave the server with no dashboard.
# The fork changes no frontend code, so upstream's UI is correct here.

ARG BASE_IMAGE=dstackai/dstack:0.20.29@sha256:6d57647be04cad42dff2343f4f50d41a3b8bb438ebc67165bc56aa92858e69ce

# Derive the release version from the git metadata in the build context, so a
# deploy never depends on somebody remembering to bump an environment variable.
# .dockerignore keeps only .git/HEAD, .git/refs and .git/packed-refs, which is a
# few kilobytes; version.sh reads them without needing a git binary.
#
# Its own stage for two reasons: the version changes on every commit, so keeping
# it here stops it from invalidating the wheel build's cache until the wheel is
# actually assembled, and both this image and the binaries image run the same
# script over the same context, which is what makes their versions agree.
FROM ${BASE_IMAGE} AS version
WORKDIR /version
COPY docker/server/carbonteq/version.sh ./
COPY .git .git
RUN sh version.sh > /release-version && cat /release-version

FROM ${BASE_IMAGE} AS build
# Optional override. CI can pin a version explicitly; leave it unset and the
# build resolves its own.
ARG RELEASE_VERSION=""
WORKDIR /build
COPY --from=version /release-version /release-version
COPY pyproject.toml README.md ./
COPY src src
COPY skills skills

# Bake the version into the package rather than passing it at run time.
# `_internal/settings.py` reads DSTACK_VERSION with `dstack.version.__version__`
# as its fallback, and `get_dstack_runner_version()`/`get_dstack_shim_version()`
# prefer DSTACK_VERSION over DSTACK_RUNNER_VERSION/DSTACK_SHIM_VERSION. So this
# single baked value drives the version the server reports *and* the runner and
# shim build that workers are told to install — no version env vars at all.
#
# version.py is edited in place rather than overwritten because it also carries
# the base-image versions dstack uses to build job containers.
RUN set -eu; \
    v="${RELEASE_VERSION:-$(cat /release-version)}"; \
    test -n "$v"; \
    sed -i "s|^__version__ = .*|__version__ = \"${v}\"|" src/dstack/version.py; \
    sed -i "s|^__is_release__ = .*|__is_release__ = True|" src/dstack/version.py; \
    grep -E '^__version__|^__is_release__' src/dstack/version.py

RUN /root/.local/bin/uv build --wheel -o /dist .

FROM ${BASE_IMAGE}

# Only used to label the image; the authoritative version is the one baked into
# the wheel above and recorded in /etc/carbonteq-release.
ARG RELEASE_VERSION
ARG SOURCE_COMMIT

COPY --from=version /release-version /etc/carbonteq-release
COPY --from=build /dist/*.whl /tmp/

RUN set -eu; \
    py=/root/.local/share/uv/tools/dstack/bin/python; \
    wheel="$(find /tmp -maxdepth 1 -type f -name 'dstack-*.whl')"; \
    test "$(printf '%s\n' "$wheel" | wc -l)" -eq 1; \
    statics="$("$py" -c "import dstack,pathlib;print(pathlib.Path(dstack.__file__).parent/'_internal/server/statics')")"; \
    cp -a "$statics" /tmp/statics.bak; \
    /root/.local/bin/uv pip install --python "$py" --no-deps --reinstall "$wheel"; \
    mkdir -p "$statics"; \
    cp -a /tmp/statics.bak/. "$statics"/; \
    rm -rf /tmp/statics.bak "$wheel"; \
    test "$(ls "$statics" | wc -l)" -gt 0

# The installed package must report the version this image was built at,
# otherwise the server would tell workers to fetch a build the binaries image
# does not serve. Checked here so a broken bake fails the build rather than the
# deployment.
RUN set -eu; \
    py=/root/.local/share/uv/tools/dstack/bin/python; \
    baked="$("$py" -c 'import dstack.version as v; print(v.__version__)')"; \
    expected="$(cat /etc/carbonteq-release)"; \
    test "$baked" = "${RELEASE_VERSION:-$expected}" \
        || { echo "baked version '$baked' != expected '${RELEASE_VERSION:-$expected}'" >&2; exit 1; }; \
    echo "release version: $baked"

# DSTACK_VERSION and DSTACK_RELEASE are deliberately not set. Both now come from
# the baked package: settings.py falls back to `dstack.version.__version__`, and
# `__is_release__` is set to True during the build. Setting them here would need
# the version passed in as a build arg again, which is the manual step this
# whole arrangement removes.

LABEL org.opencontainers.image.revision=${SOURCE_COMMIT}
LABEL org.opencontainers.image.version=${RELEASE_VERSION}
LABEL org.carbonteq.dstack.component-set=${SOURCE_COMMIT}

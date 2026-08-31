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
#
# The ctpolicy wheel (policy/) rides along as a second --no-deps install. It
# declares no dependencies precisely so that it cannot perturb the inherited
# dependency graph; everything it imports — pydantic v1, PyYAML, dstack — is
# already in the base image. Its `dstack.plugins` entry point is what lets the
# server discover it once `plugins: [ctpolicy]` is set in config.yml.

ARG BASE_IMAGE=dstackai/dstack:0.20.29@sha256:6d57647be04cad42dff2343f4f50d41a3b8bb438ebc67165bc56aa92858e69ce

FROM ${BASE_IMAGE} AS build
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src src
COPY skills skills
RUN /root/.local/bin/uv build --wheel -o /dist .
COPY policy policy
RUN /root/.local/bin/uv build --wheel -o /dist policy

FROM ${BASE_IMAGE}

ARG RELEASE_VERSION
ARG SOURCE_COMMIT

COPY --from=build /dist/*.whl /tmp/

RUN set -eu; \
    py=/root/.local/share/uv/tools/dstack/bin/python; \
    wheel="$(find /tmp -maxdepth 1 -type f -name 'dstack-*.whl')"; \
    test "$(printf '%s\n' "$wheel" | wc -l)" -eq 1; \
    policy_wheel="$(find /tmp -maxdepth 1 -type f -name 'ctpolicy-*.whl')"; \
    test "$(printf '%s\n' "$policy_wheel" | wc -l)" -eq 1; \
    statics="$("$py" -c "import dstack,pathlib;print(pathlib.Path(dstack.__file__).parent/'_internal/server/statics')")"; \
    cp -a "$statics" /tmp/statics.bak; \
    /root/.local/bin/uv pip install --python "$py" --no-deps --reinstall "$wheel"; \
    /root/.local/bin/uv pip install --python "$py" --no-deps --reinstall "$policy_wheel"; \
    mkdir -p "$statics"; \
    cp -a /tmp/statics.bak/. "$statics"/; \
    rm -rf /tmp/statics.bak "$wheel" "$policy_wheel"; \
    test "$(ls "$statics" | wc -l)" -gt 0; \
    "$py" -c "import ctpolicy; ctpolicy.PolicyPlugin()"

ENV DSTACK_VERSION=${RELEASE_VERSION}
ENV DSTACK_RELEASE=1

LABEL org.opencontainers.image.revision=${SOURCE_COMMIT}
LABEL org.opencontainers.image.version=${RELEASE_VERSION}
LABEL org.carbonteq.dstack.component-set=${SOURCE_COMMIT}

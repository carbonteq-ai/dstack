#!/bin/sh
# Prints the PEP 440 release version for the current commit, matching the
# contract in ai-infra's scripts/dstack_release_contract.py:
#
#   <upstream tag>+carbonteq.g<first 12 commit characters>
#
# The "g" prefix stops an all-numeric commit prefix from being normalized as a
# numeric local-version component. Usage: ./version.sh [commit-ish]
set -eu
UPSTREAM_TAG="${UPSTREAM_TAG:-0.20.29}"
commit="$(git rev-parse "${1:-HEAD}")"
printf '%s+carbonteq.g%.12s\n' "$UPSTREAM_TAG" "$commit"

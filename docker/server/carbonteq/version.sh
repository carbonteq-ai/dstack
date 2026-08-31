#!/bin/sh
# Prints the PEP 440 release version for the current commit, matching the
# contract in ai-infra's scripts/dstack_release_contract.py:
#
#   <upstream tag>+carbonteq.g<first 12 commit characters>
#
# The "g" prefix stops an all-numeric commit prefix from being normalized as a
# numeric local-version component.
#
# Usage:
#   ./version.sh                # HEAD of the checkout this script lives in
#   ./version.sh <commit-ish>   # a specific commit (needs the git binary)
#
# The Docker builds call this as well, which is what removes the manual version
# bump: the image derives its own version instead of being told one. That
# environment has no git binary and only the ref metadata in the context (see
# .dockerignore), so HEAD is resolved by reading .git directly when a usable
# repository is not available. Keep this POSIX sh — it runs in the dstack server
# image, in golang:bookworm, and from a normal shell.
set -eu

UPSTREAM_TAG="${UPSTREAM_TAG:-0.20.29}"
GIT_DIR="${GIT_DIR:-.git}"

fail() {
    echo "version.sh: $1" >&2
    exit 1
}

# Read the commit id straight out of .git. HEAD is either a symbolic ref to a
# branch — whose tip is a loose file, or an entry in packed-refs once git has
# packed it — or a raw commit id when the checkout is detached.
resolve_head_from_git_dir() {
    [ -f "$GIT_DIR/HEAD" ] || fail "no git metadata at $GIT_DIR and no commit given"
    head=$(cat "$GIT_DIR/HEAD")
    case "$head" in
        "ref: "*)
            ref=${head#ref: }
            if [ -f "$GIT_DIR/$ref" ]; then
                cat "$GIT_DIR/$ref"
            elif [ -f "$GIT_DIR/packed-refs" ]; then
                # Comment lines start with '#' and peeled-tag lines with '^';
                # neither has a second field, so they never match a ref name.
                # Done with the shell alone so this needs no awk in the image.
                while read -r sha name _rest; do
                    if [ "$name" = "$ref" ]; then
                        echo "$sha"
                        break
                    fi
                done < "$GIT_DIR/packed-refs"
            fi
            ;;
        *)
            echo "$head"
            ;;
    esac
}

# A trimmed .git has refs but no object database, and git refuses to operate on
# it. Checking for objects/ is what routes the Docker builds to the reader above
# rather than to a git that would fail confusingly.
usable_git_repo() {
    command -v git >/dev/null 2>&1 &&
        [ -d "$GIT_DIR/objects" ] &&
        git rev-parse --git-dir >/dev/null 2>&1
}

if [ $# -gt 0 ]; then
    command -v git >/dev/null 2>&1 || fail "resolving '$1' needs the git binary"
    commit=$(git rev-parse "$1")
elif usable_git_repo; then
    commit=$(git rev-parse HEAD)
else
    commit=$(resolve_head_from_git_dir)
fi

# Guard against an empty or truncated read producing a version that looks valid
# but pins nothing — that would silently stop workers from being upgraded.
case "$commit" in
    *[!0-9a-f]*) fail "resolved commit '$commit' is not a hex object id" ;;
    ????????????*) ;;
    *) fail "resolved commit '$commit' is too short to identify a build" ;;
esac

printf '%s+carbonteq.g%.12s\n' "$UPSTREAM_TAG" "$commit"

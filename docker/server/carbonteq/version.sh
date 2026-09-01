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

# Resolve against THIS tree, not the caller's. The script lives at
# <root>/docker/server/carbonteq/version.sh, so the root is three levels up.
#
# Without this the script answers about whatever repository the shell happens to
# be standing in: run from a consumer repo that vendors this one as a submodule,
# `git rev-parse HEAD` returns the CONSUMER's commit and the image is tagged with
# a foreign sha — silently, with exit 0. That is worse than failing, because the
# version looks well-formed and pins nothing.
# Two layouts have to work. In a checkout the script sits at
# <root>/docker/server/carbonteq/version.sh. In the image builds it is COPYed
# alone into a flat WORKDIR next to a trimmed .git, so three levels up would
# climb out of the build context entirely — hence "the first candidate that has
# a .git" rather than a fixed depth.
SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$SCRIPT_DIR
for candidate in "$SCRIPT_DIR/../../.." "$SCRIPT_DIR" "$PWD"; do
    if [ -e "$candidate/.git" ]; then
        REPO_ROOT=$(CDPATH='' cd -- "$candidate" && pwd)
        break
    fi
done
GIT_DIR="${GIT_DIR:-$REPO_ROOT/.git}"

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

# Whether git can answer about REPO_ROOT.
#
# Asking git itself rather than looking for `.git/objects`: a submodule's `.git`
# is a FILE holding a `gitdir:` pointer, so the directory probe called a perfectly
# usable checkout unusable and fell through to the raw reader, which then failed
# on the same file. `--git-dir` follows the pointer and answers correctly for
# both layouts.
#
# The Docker builds still take the reader below, because their context carries
# `.git/HEAD` and refs but no object database, and `rev-parse` needs objects.
usable_git_repo() {
    command -v git >/dev/null 2>&1 &&
        git -C "$REPO_ROOT" rev-parse --git-dir >/dev/null 2>&1 &&
        [ -n "$(git -C "$REPO_ROOT" rev-parse --show-toplevel 2>/dev/null)" ]
}

if [ $# -gt 0 ]; then
    command -v git >/dev/null 2>&1 || fail "resolving '$1' needs the git binary"
    commit=$(git -C "$REPO_ROOT" rev-parse "$1")
elif usable_git_repo; then
    commit=$(git -C "$REPO_ROOT" rev-parse HEAD)

    # The commit must belong to THIS project. If the resolved tree has no
    # src/dstack/version.py we are describing somebody else's history — the
    # exact failure this guard exists for — and a wrong version is worse than
    # no version, because it tags an image that workers will never be told to
    # upgrade to.
    if ! git -C "$REPO_ROOT" cat-file -e "$commit:src/dstack/version.py" 2>/dev/null; then
        fail "commit $commit has no src/dstack/version.py — that is not this repository"
    fi
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

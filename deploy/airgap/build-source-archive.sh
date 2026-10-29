#!/usr/bin/env bash
# Package the tracked source tree of one commit for the air-gap release.
# Usage: build-source-archive.sh <version> <git-ref> <output-dir>
set -euo pipefail

version="${1:?version required}"
ref="${2:?git ref required}"
out_dir="${3:?output directory required}"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
commit="$(git -C "$repo_root" rev-parse --verify "${ref}^{commit}")"
name="bagofwords-source-${version}"
archive="${name}.tar.gz"

mkdir -p "$out_dir"
out_dir="$(cd "$out_dir" && pwd)"
meta="$(mktemp -d)"
trap 'rm -rf "$meta"' EXIT
printf '%s\n' "$commit" > "$meta/SOURCE_COMMIT"

# git archive ships only tracked files (no .git, node_modules or build output)
# and honours export-ignore in .gitattributes. Fixed mtime keeps it reproducible.
git -C "$repo_root" archive --format=tar --prefix="${name}/" \
  --add-file="$meta/SOURCE_COMMIT" "$commit" \
  | gzip -n > "${out_dir}/${archive}"

(cd "$out_dir" && sha256sum "$archive" > "${archive}.sha256")
echo "${out_dir}/${archive} (${commit})"

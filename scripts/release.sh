#!/usr/bin/env bash
set -euo pipefail

SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_CODEX="${HOME}/.codex/skills/solo-2611"
DEFAULT_CLAUDE="${HOME}/.claude/skills/solo-2611"

if [[ $# -gt 0 ]]; then
  TARGETS=("$@")
elif [[ -n "${SOLO2611_RELEASE_TARGETS:-}" ]]; then
  IFS=':' read -r -a TARGETS <<<"${SOLO2611_RELEASE_TARGETS}"
else
  TARGETS=("${DEFAULT_CODEX}" "${DEFAULT_CLAUDE}")
fi

scripts_hash() {
  local root="$1"
  (
    cd "${root}/scripts"
    find . -type f -not -path '*/__pycache__/*' -print0 |
      LC_ALL=C sort -z |
      xargs -0 shasum -a 256 |
      shasum -a 256 |
      awk '{print $1}'
  )
}

version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["version"])' "${SOURCE}/VERSION.json")"
source_hash="$(scripts_hash "${SOURCE}")"

for target in "${TARGETS[@]}"; do
  mkdir -p "$(dirname "${target}")"
  rsync -a --delete \
    --exclude '.git/' \
    --exclude '.pytest_cache/' \
    --exclude '__pycache__/' \
    --exclude '.DS_Store' \
    "${SOURCE}/" "${target}/"
  target_version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["version"])' "${target}/VERSION.json")"
  target_hash="$(scripts_hash "${target}")"
  if [[ "${target_version}" != "${version}" || "${target_hash}" != "${source_hash}" ]]; then
    echo "发布校验失败：${target} version=${target_version} hash=${target_hash}" >&2
    exit 2
  fi
  printf '已发布 %s version=%s scripts=%s\n' "${target}" "${target_version}" "${target_hash}"
done

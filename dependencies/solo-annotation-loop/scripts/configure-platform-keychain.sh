#!/usr/bin/env bash
set -euo pipefail

SERVICE="${SOLO_MANAGER_KEYCHAIN_SERVICE:-solo-manager-token}"

if [[ "${1:-}" == "--help" ]]; then
  printf '用法: %s\n' "$(basename "$0")"
  printf '在 macOS Keychain 中交互式保存或更新 Solo Manager 访问令牌。\n'
  printf '默认 service: %s\n' "$SERVICE"
  exit 0
fi

if [[ $# -gt 0 ]]; then
  printf '不接受位置参数，使用 --help 查看用法。\n' >&2
  exit 2
fi

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo '此脚本仅支持 macOS。' >&2
  exit 1
fi
if [[ -z "${USER:-}" ]]; then
  echo '环境变量 USER 为空，无法确定 Keychain account。' >&2
  exit 1
fi

security add-generic-password -a "$USER" -s "$SERVICE" -U -w
printf '已保存到 macOS Keychain: service=%s account=%s\n' "$SERVICE" "$USER"

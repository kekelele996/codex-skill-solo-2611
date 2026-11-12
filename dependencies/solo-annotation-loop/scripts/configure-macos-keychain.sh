#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEVICE_USERNAME="${BENZHI_DEVICE_USERNAME:-}"
REGISTER_ONLY=0

usage() {
  printf '用法: %s [--username 设备用户名] [--register-only]\n' "$(basename "$0")"
  printf '为当前设备保存独立的 Claude Code API Key。\n'
  printf '不传用户名时，会从设备名和机器 UUID 自动生成唯一用户名。\n'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --username)
      [[ $# -ge 2 ]] || { usage >&2; exit 2; }
      DEVICE_USERNAME="$2"
      shift 2
      ;;
    --register-only)
      REGISTER_ONLY=1
      shift
      ;;
    --help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo '此脚本仅支持 macOS。' >&2
  exit 1
fi

if ! command -v security >/dev/null 2>&1; then
  echo '未找到 macOS security 命令。' >&2
  exit 1
fi

IDENTITY_ARGS=(--format json)
if [[ -n "$DEVICE_USERNAME" ]]; then
  IDENTITY_ARGS+=(--username "$DEVICE_USERNAME")
fi
IDENTITY_JSON="$(python3 "$SCRIPT_DIR/device-identity.py" "${IDENTITY_ARGS[@]}")"
USERNAME="$(printf '%s' "$IDENTITY_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["username"])')"
SERVICE="$(printf '%s' "$IDENTITY_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["keychainService"])')"
CONFIG_PATH="$(printf '%s' "$IDENTITY_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["configPath"])')"

if [[ "$REGISTER_ONLY" == "1" ]]; then
  printf '设备身份已登记。用户名=%s Keychain service=%s 配置文件=%s\n' "$USERNAME" "$SERVICE" "$CONFIG_PATH"
  exit 0
fi

security add-generic-password -a "$USERNAME" -s "$SERVICE" -U -w
NEW_KEY="$(security find-generic-password -a "$USERNAME" -s "$SERVICE" -w 2>/dev/null || true)"
LEGACY_KEY="$(security find-generic-password -a "$USER" -s benzhi-claude-code -w 2>/dev/null || true)"
printf '已保存到 macOS Keychain: service=%s account=%s 配置文件=%s\n' "$SERVICE" "$USERNAME" "$CONFIG_PATH"
if [[ -n "$NEW_KEY" && -n "$LEGACY_KEY" && "$NEW_KEY" == "$LEGACY_KEY" ]]; then
  printf '警告：当前设备 Key 与旧共享 Key 相同，上游身份仍可能串用。请换成该设备独立签发的 Key。\n' >&2
fi

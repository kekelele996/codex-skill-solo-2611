#!/usr/bin/env python3
"""Resolve and persist a stable per-device identity for isolated Claude Code."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def command_output(*args: str) -> str:
    try:
        result = subprocess.run(args, text=True, capture_output=True, check=False)
    except OSError:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def machine_uuid() -> str:
    if platform.system() == "Darwin":
        raw = command_output("ioreg", "-rd1", "-c", "IOPlatformExpertDevice")
        match = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', raw)
        if match:
            return match.group(1)
        serial = command_output("sysctl", "-n", "hw.serialnumber")
        if serial:
            return serial
    for path in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            value = ""
        if value:
            return value
    return f"{socket.gethostname()}:{platform.system()}:{platform.machine()}"


def slug(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value[:32] or "device"


def suggest_username() -> str:
    if platform.system() == "Darwin":
        name = command_output("scutil", "--get", "ComputerName")
    else:
        name = ""
    name = name or socket.gethostname().split(".")[0]
    digest = hashlib.sha256(machine_uuid().encode("utf-8")).hexdigest()[:8]
    return f"{slug(name)}-{digest}"


def config_path(explicit: str | None) -> Path:
    value = explicit or os.environ.get("BENZHI_DEVICE_CONFIG") or "~/.codex/benzhi-claude-device.json"
    return Path(value).expanduser().resolve()


def load_config(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def build_identity(path: Path, requested_username: str | None) -> dict:
    existing = load_config(path)
    username = (requested_username or existing.get("username") or os.environ.get("BENZHI_DEVICE_USERNAME") or suggest_username()).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,63}", username):
        raise SystemExit("用户名需为三到六十四个字母数字、点、下划线或连字符，且以字母数字开头")
    keychain_service = (os.environ.get("BENZHI_CLAUDE_KEYCHAIN_SERVICE") or existing.get("keychainService") or f"benzhi-claude-code-{username}").strip()
    if not keychain_service:
        raise SystemExit("Keychain service 不能为空")
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "username": username,
        "keychainService": keychain_service,
        "account": username,
        "hostname": socket.gethostname(),
        "platform": platform.system(),
        "machineIdShort": hashlib.sha256(machine_uuid().encode("utf-8")).hexdigest()[:8],
        "createdAt": existing.get("createdAt") or now,
        "updatedAt": now,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve the per-device Claude Code identity")
    parser.add_argument("--config", help="identity JSON path")
    parser.add_argument("--username", help="set or override the device username")
    parser.add_argument("--format", choices=("json", "shell"), default="json")
    args = parser.parse_args()

    path = config_path(args.config)
    identity = build_identity(path, args.username)
    if args.username or not path.exists() or load_config(path) != identity:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(identity, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)

    if args.format == "shell":
        print(f"DEVICE_USERNAME={shlex.quote(identity['username'])}")
        print(f"KEYCHAIN_SERVICE={shlex.quote(identity['keychainService'])}")
        print(f"DEVICE_ACCOUNT={shlex.quote(identity['account'])}")
        print(f"DEVICE_IDENTITY_CONFIG={shlex.quote(str(path))}")
    else:
        print(json.dumps({**identity, "configPath": str(path)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

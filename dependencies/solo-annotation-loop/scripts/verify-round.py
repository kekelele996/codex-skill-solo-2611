#!/usr/bin/env python3
"""Run declared project checks and record their real exit codes."""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
from pathlib import Path


def run(command: str, cwd: Path) -> dict[str, object]:
    try:
        proc = subprocess.run(command, cwd=cwd, shell=True, text=True, capture_output=True, timeout=300)
        output = (proc.stdout + proc.stderr).strip()
        return {"command": command, "exitCode": proc.returncode, "ok": proc.returncode == 0, "output": output[-4000:]}
    except subprocess.TimeoutExpired as exc:
        return {"command": command, "exitCode": None, "ok": False, "output": f"timeout: {exc}"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run project verification commands")
    parser.add_argument("project", type=Path)
    parser.add_argument("--commands", type=Path, help="JSON list or object with testCommands/buildCommands")
    parser.add_argument("--command", action="append", default=[])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.project.resolve()
    if not root.is_dir():
        parser.error(f"项目目录不存在: {root}")
    commands = list(args.command)
    if args.commands:
        spec = json.loads(args.commands.read_text(encoding="utf-8"))
        if isinstance(spec, list):
            commands.extend(str(item) for item in spec)
        elif isinstance(spec, dict):
            for key in ("testCommands", "buildCommands", "checks"):
                commands.extend(str(item) for item in spec.get(key, []))
    if not commands:
        parser.error("至少提供一个 --command 或 --commands")
    results = [run(command, root) for command in commands]
    payload = {"projectPath": str(root), "results": results, "ok": all(item["ok"] for item in results)}
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

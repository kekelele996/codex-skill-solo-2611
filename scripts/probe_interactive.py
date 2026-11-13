#!/usr/bin/env python3
"""Exercise the official interactive entry point before enabling v4 broadly."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from common import api_key, base_url, load_config, load_state, out, run, utc_now, write_json
import session


def _result(ok: bool, **fields: Any) -> dict[str, Any]:
    return {"ok": ok, "checkedAt": utc_now(), **fields}


def probe(task_root: Path, *, prompt_file: Path | None = None) -> dict[str, Any]:
    state = load_state(task_root)
    prompt_path = prompt_file or (
        Path(state["promptPath"]) if state.get("promptPath") else None
    )
    if prompt_path is None or not prompt_path.is_file():
        raise RuntimeError("探针需要 --prompt-file 或任务 state.promptPath")
    prompt = prompt_path.read_text(encoding="utf-8")
    config = load_config()
    image = str((config.get("claude") or {}).get("image") or "")
    key = api_key(config)
    url = base_url(config)
    directory = task_root / "monitor" / "runs" / "probe-interactive"
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    attempt = 99
    original_attempt_dir = session.attempt_dir

    def _probe_dir(root: Path, index: int) -> Path:
        return directory

    session.attempt_dir = _probe_dir  # type: ignore[assignment]
    launched: dict[str, str] = {}
    checks: dict[str, Any] = {}
    try:
        launched = session.launch_tmux_container(
            task_root=task_root,
            state=state,
            attempt=attempt,
            image=image,
            key=key,
            base_url=url,
        )
        checks["replReady"] = session.wait_for_repl(launched["tmux"], timeout=90)
        if not checks["replReady"]:
            return _result(False, launched=launched, checks=checks)

        marker = f"评测探针-中文标点，引号“测试” {time.time_ns()}"
        run(["tmux", "send-keys", "-l", "-t", launched["tmux"], "--", marker], timeout=10)
        time.sleep(0.8)
        screen = session.capture_pane(launched["tmux"], lines=20)
        checks["literalInput"] = marker in screen
        checks["screenEvidence"] = screen[-2000:]
        run(["tmux", "send-keys", "-t", launched["tmux"], "C-u"], check=False, timeout=10)

        session.send_literal(launched["tmux"], prompt)
        deadline = time.monotonic() + 180
        trace = None
        while time.monotonic() < deadline:
            trace = session._latest_trace(directory / "live")
            if trace is not None and trace.stat().st_size > 0:
                break
            time.sleep(1)
        checks["liveMount"] = bool(trace and trace.stat().st_size > 0)
        checks["liveTrace"] = str(trace) if trace else ""
        if not checks["liveMount"]:
            return _result(False, launched=launched, checks=checks)

        run(["tmux", "send-keys", "-t", launched["tmux"], "C-c"], check=False, timeout=10)
        time.sleep(2)
        before = trace.stat().st_size
        session.send_continue(launched["tmux"])
        deadline = time.monotonic() + 180
        grew = False
        while time.monotonic() < deadline:
            if trace.stat().st_size > before:
                grew = True
                break
            time.sleep(1)
        checks["continueAccepted"] = grew
        return _result(all(bool(value) for key_name, value in checks.items() if key_name != "screenEvidence"), launched=launched, checks=checks)
    finally:
        session._remove_tmux_and_container(
            launched.get("container", ""),
            launched.get("tmux", ""),
        )
        session.attempt_dir = original_attempt_dir  # type: ignore[assignment]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-root", required=True)
    parser.add_argument("--prompt-file")
    args = parser.parse_args()
    result = probe(
        Path(args.task_root).resolve(),
        prompt_file=Path(args.prompt_file).expanduser().resolve() if args.prompt_file else None,
    )
    path = Path(args.task_root).resolve() / "monitor" / "probe-interactive.json"
    write_json(path, result)
    print(json.dumps({**result, "evidence": str(path)}, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())

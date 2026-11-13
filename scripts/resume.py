"""Resume helpers for unattended print runs and legacy task state."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from common import load_config, load_state, out, run, save_state, utc_now


def _alive(pid: Any) -> bool:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    try:
        os.kill(value, 0)
    except (OSError, ValueError):
        return False
    return True


def last_running_attempt(state: dict[str, Any]) -> dict[str, Any] | None:
    for attempt in reversed(state.get("attempts") or []):
        if attempt.get("status") in {"running", "pending_collect"}:
            return attempt
    return None


def _container_status(name: str) -> str:
    if not name:
        return ""
    result = run(
        ["docker", "inspect", "--format", "{{.State.Status}}", name],
        check=False,
        timeout=20,
    )
    return out(result) if result.returncode == 0 else ""


def _collect_print_attempt(task_root: Path, state: dict[str, Any], attempt: dict[str, Any]) -> dict[str, Any]:
    import solo2611 as cli

    name = str(attempt.get("container") or "")
    directory = Path(str(attempt.get("dir") or ""))
    result = attempt.get("result") if isinstance(attempt.get("result"), dict) else {}
    info = cli._collect_container(task_root, state, name, directory, result)
    attempt["finishedAt"] = utc_now()
    attempt["completion"] = info.get("completion") or {}
    attempt["errors"] = list(info.get("errors") or [])
    if info.get("ok"):
        cli._accept_attempt(task_root, state, attempt, info)
        attempt["status"] = "accepted"
        save_state(task_root, state)
        return {
            "ok": True,
            "status": "accepted",
            "sessionId": info.get("sessionId"),
            "promptId": info.get("promptId"),
            "next": "record",
        }
    attempt["status"] = "rejected"
    state["status"] = "run_failed"
    save_state(task_root, state)
    return {"ok": False, "status": "rejected", "errors": info.get("errors") or []}


def resume_print(task_root: Path) -> dict[str, Any]:
    """Reattach to the one running opportunity without creating a new one."""
    state = load_state(task_root)
    attempt = last_running_attempt(state)
    if attempt is None:
        return {"ok": False, "status": "idle", "message": "没有需要接回的运行中尝试"}
    pid = attempt.get("pid")
    detached_process_alive = _alive(pid)
    name = str(attempt.get("container") or "")
    status = _container_status(name)
    if not status:
        attempt["status"] = "lost"
        attempt["errors"] = list(attempt.get("errors") or []) + ["容器已不存在"]
        state["status"] = "run_failed"
        save_state(task_root, state)
        return {
            "ok": False,
            "status": "lost",
            "message": "容器已不存在；不会自动重跑，请换题或按旧流程 reset",
        }
    if status == "running":
        config = load_config()
        claude = config.get("claude") or {}
        total_limit = float(claude.get("timeoutSeconds") or 10800)
        silent_limit = float(claude.get("silentSeconds") or 1200)
        directory = Path(str(attempt.get("dir") or ""))
        stdout_path = directory / "stdout.jsonl"
        size = stdout_path.stat().st_size if stdout_path.is_file() else 0
        last_growth = time.monotonic()
        started = time.monotonic()
        while _container_status(name) == "running":
            time.sleep(5)
            current_size = stdout_path.stat().st_size if stdout_path.is_file() else 0
            if current_size != size:
                size = current_size
                last_growth = time.monotonic()
            if time.monotonic() - last_growth >= silent_limit:
                attempt["timeout"] = True
                attempt["timeoutKind"] = "silent"
                run(["docker", "stop", "-t", "10", name], check=False, timeout=60)
                break
            if time.monotonic() - started >= total_limit:
                attempt["timeout"] = True
                attempt["timeoutKind"] = "total"
                run(["docker", "stop", "-t", "10", name], check=False, timeout=60)
                break
        if detached_process_alive:
            attempt["reattachedAt"] = utc_now()
            attempt["reattachedFromPid"] = int(pid)
            save_state(task_root, state)
    return _collect_print_attempt(task_root, state, attempt)

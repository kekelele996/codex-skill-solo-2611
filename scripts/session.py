"""Managed interactive Claude session and independent supervisor.

The official image supports only its ``interactive`` and ``print`` entry
points.  This module keeps the official interactive session alive behind tmux,
uses the live Claude projects JSONL as the only semantic source of truth, and
records every automatic decision in ``supervisor.log``.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from common import (
    CONTAINER_TRACE_DIR,
    container_slug,
    file_lock,
    load_config,
    load_state,
    out,
    read_json,
    run,
    save_state,
    sha256_bytes,
    Solo2611Error,
    utc_now,
)
import trace as tr

CONTINUE_TEXT = "继续"
CONTINUE_HARD_LIMIT = 2
TERMINAL = {"accepted", "aborted"}
ACTIVE = {"launching", "prompt_sent", "running", "continuing", "holding", "manual_input", "closing"}
RUNNING_ATTEMPT_STATUSES = {"running", "start_failed", "pending_collect"}
REPL_READY = re.compile(r"(?m)(?:^|\n)\s*(?:>|❯|›)\s*$")
SCREEN_BUSY = re.compile(r"(?i)esc\s+to\s+interrupt|ctrl\+c\s+to\s+interrupt|thinking|working")
ROUND_STOP_REASONS = {"end_turn", "max_tokens", "stop_sequence"}
RETRY_AFTER = re.compile(r"(?i)retry[-_ ]after[^0-9]{0,12}(\d+)")


def _parse_time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _iso(value: datetime | None = None) -> str:
    return (value or datetime.now(timezone.utc)).replace(microsecond=0).isoformat()


def _plus_seconds(seconds: float) -> str:
    return _iso(datetime.now(timezone.utc) + timedelta(seconds=max(0, seconds)))


def attempt_dir(task_root: Path, attempt: int) -> Path:
    return task_root / "monitor" / "runs" / f"attempt-{attempt:02d}"


def supervisor_log(task_root: Path, attempt: int) -> Path:
    return attempt_dir(task_root, attempt) / "supervisor.log"


def commands_log(task_root: Path, attempt: int) -> Path:
    return attempt_dir(task_root, attempt) / "commands.jsonl"


def append_event(
    task_root: Path,
    attempt: int,
    event: str,
    **fields: Any,
) -> dict[str, Any]:
    item = {"at": utc_now(), "event": event, **fields}
    path = supervisor_log(task_root, attempt)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path.with_suffix(".lock")):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
    return item


def read_events(task_root: Path, attempt: int, *, limit: int = 1000) -> list[dict[str, Any]]:
    path = supervisor_log(task_root, attempt)
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def tail_log(path: Path, limit: int = 30) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def effective_continue_max(config: dict[str, Any] | None = None) -> int:
    config = config or load_config()
    raw = (config.get("claude") or {}).get("continueMax", 2)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 2
    return min(CONTINUE_HARD_LIMIT, max(0, value))


def _pid_alive(pid: Any) -> bool:
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
    try:
        command = subprocess.run(
            ["ps", "-p", str(value), "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    return "supervise" in command and "solo2611.py" in command


def supervisor_alive(state: dict[str, Any], task_root: Path | None = None) -> bool:
    session = state.get("sessionRun") or {}
    supervisor = session.get("supervisor") or {}
    if _pid_alive(supervisor.get("pid")):
        return True
    if task_root is None:
        return False
    lock = task_root / "monitor" / ".supervisor.lock"
    try:
        first = lock.read_text(encoding="utf-8").strip().split(":", 1)[0]
    except OSError:
        return False
    return _pid_alive(first)


def container_exists(name: str) -> bool:
    if not name:
        return False
    result = run(
        ["docker", "inspect", "--format", "{{.State.Status}}", name],
        check=False,
        timeout=20,
    )
    return result.returncode == 0 and out(result) in {"created", "running", "exited", "paused"}


def container_running(name: str) -> bool:
    if not name:
        return False
    result = run(
        ["docker", "inspect", "--format", "{{.State.Running}}", name],
        check=False,
        timeout=20,
    )
    return result.returncode == 0 and out(result).lower() == "true"


def tmux_alive(name: str) -> bool:
    if not name:
        return False
    return run(["tmux", "has-session", "-t", name], check=False, timeout=10).returncode == 0


def capture_pane(name: str, *, lines: int = 30) -> str:
    if not name:
        return ""
    result = run(
        ["tmux", "capture-pane", "-p", "-t", name, "-S", f"-{max(1, lines)}"],
        check=False,
        timeout=10,
    )
    return out(result) if result.returncode == 0 else ""


def _screen_busy(screen: str) -> bool:
    return bool(SCREEN_BUSY.search(screen or ""))


def _main_user_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event for event in events if tr.is_real_user_prompt(event)]


def round_ended(
    events: list[dict[str, Any]],
    *,
    file_size: int | None = None,
    previous_size: int | None = None,
    last_growth_at: float | None = None,
    screen: str = "",
    now: float | None = None,
    quiet_seconds: float = 10.0,
) -> bool:
    """Return True only when the trace and the terminal both look idle."""
    current = time.monotonic() if now is None else now
    assistants = [
        event
        for event in events
        if event.get("type") == "assistant" and not event.get("isSidechain")
    ]
    if not assistants:
        return False
    final = assistants[-1]
    index = events.index(final)
    if any(event.get("type") == "user" and not event.get("isSidechain") for event in events[index + 1 :]):
        return False
    message = final.get("message") if isinstance(final.get("message"), dict) else {}
    stop_reason = message.get("stop_reason")
    if not final.get("isApiErrorMessage") and stop_reason not in ROUND_STOP_REASONS:
        return False
    if file_size is not None and previous_size is not None and file_size != previous_size:
        return False
    if last_growth_at is not None and current - last_growth_at < quiet_seconds:
        return False
    if _screen_busy(screen):
        return False
    return True


def dangling_tool_calls(events: list[dict[str, Any]]) -> list[str]:
    uses: set[str] = set()
    results: set[str] = set()
    for event in events:
        content = tr._content(event)
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                uses.add(str(block.get("id") or ""))
            elif block.get("type") == "tool_result":
                results.add(str(block.get("tool_use_id") or ""))
    return sorted(item for item in uses - results if item)


def _final_assistant(events: list[dict[str, Any]]) -> dict[str, Any]:
    for event in reversed(events):
        if event.get("type") == "assistant" and not event.get("isSidechain"):
            return event
    return {}


def classify_round(
    info: dict[str, Any],
    result: dict[str, Any] | None,
    events: list[dict[str, Any]],
    *,
    gateway_failures: int = 0,
) -> dict[str, Any]:
    """Classify one ended assistant round.  This is table-driven by design."""
    final = _final_assistant(events)
    message = final.get("message") if isinstance(final.get("message"), dict) else {}
    text = tr._text_of(message.get("content")).strip()
    stop_reason = str(info.get("stopReason") or message.get("stop_reason") or "")
    api_error = bool(final.get("isApiErrorMessage")) or bool((result or {}).get("is_error"))
    lowered = text.lower()
    if api_error:
        if "429" in text or "rate limit" in lowered or "tokens per minute" in lowered:
            match = RETRY_AFTER.search(text)
            retry_after = int(match.group(1)) if match else 0
            return {
                "kind": "rate_limited",
                "verdict": "continue",
                "waitSeconds": max(120, retry_after),
                "detail": text[:500] or "429 rate limited",
            }
        if re.search(r"(?i)\b5\d\d\b|overloaded|gateway|bad gateway|service unavailable", text):
            return {
                "kind": "gateway",
                "verdict": "continue",
                "waitSeconds": 180 if gateway_failures else 60,
                "detail": text[:500],
            }
        if re.search(r"(?i)\b401\b|\b403\b|authentication|invalid.?api.?key|鉴权", text):
            return {"kind": "auth", "verdict": "hold", "waitSeconds": 0, "detail": text[:500]}
        if re.search(r"(?i)quota|insufficient.?balance|额度(不足|用尽)|\b402\b", text):
            return {"kind": "quota", "verdict": "hold", "waitSeconds": 0, "detail": text[:500]}
    if re.search(r"(?i)unrecognized_model|model.?not.?found|invalid model|模型名", text):
        return {"kind": "model_mismatch", "verdict": "hold", "waitSeconds": 0, "detail": text[:500]}
    if stop_reason == "max_tokens":
        return {"kind": "truncated", "verdict": "continue", "waitSeconds": 5, "detail": "max_tokens"}
    dangling = dangling_tool_calls(events)
    if dangling:
        return {
            "kind": "truncated",
            "verdict": "continue",
            "waitSeconds": 5,
            "detail": f"{len(dangling)} 个工具调用没有结果",
        }
    if tr.CUT_OFF.search(text):
        return {"kind": "truncated", "verdict": "continue", "waitSeconds": 5, "detail": text[-500:]}
    if text and tr.INCOMPLETE_TAIL.search(text[-400:]):
        return {"kind": "truncated", "verdict": "continue", "waitSeconds": 5, "detail": text[-500:]}
    if not text:
        return {"kind": "empty", "verdict": "continue", "waitSeconds": 5, "detail": "最终回复为空"}
    if tr.ASKING_USER.search(text[-500:]) or tr.PLAN_PROMISE.search(text[-500:]):
        return {"kind": "incomplete", "verdict": "continue", "waitSeconds": 5, "detail": text[-500:]}
    if stop_reason == "end_turn":
        return {"kind": "none", "verdict": "complete", "waitSeconds": 0, "detail": ""}
    return {
        "kind": "truncated",
        "verdict": "continue",
        "waitSeconds": 5,
        "detail": f"stop_reason={stop_reason or 'missing'}",
    }


def _trace_files(live: Path) -> list[Path]:
    if not live.is_dir():
        return []
    candidates: list[Path] = []
    for project in sorted(path for path in live.iterdir() if path.is_dir()):
        if project.name == "subagents":
            continue
        candidates.extend(sorted(project.glob("*.jsonl")))
    return sorted(candidates, key=lambda path: (path.stat().st_mtime, path.stat().st_size), reverse=True)


def _latest_trace(live: Path) -> Path | None:
    files = _trace_files(live)
    return files[0] if files else None


def _container_name(task_name: str, attempt: int) -> str:
    suffix = sha256_bytes(f"{task_name}:{attempt}:{time.time_ns()}".encode())[:4]
    return f"solo2611-{container_slug(task_name)}-{attempt:02d}-{suffix}".lower()


def _tmux_name(task_name: str, attempt: int) -> str:
    suffix = sha256_bytes(f"tmux:{task_name}:{attempt}:{time.time_ns()}".encode())[:4]
    return f"solo2611-{container_slug(task_name)}-{attempt:02d}-{suffix}".lower()


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600 if path.name == "env.sh" else 0o700)


def wait_for_repl(name: str, *, timeout: float = 60) -> bool:
    deadline = time.monotonic() + timeout
    bypass_accepted = False
    while time.monotonic() < deadline:
        screen = capture_pane(name, lines=30)
        if (
            not bypass_accepted
            and "Bypass Permissions mode" in screen
            and "Yes, I accept" in screen
        ):
            if re.search(r"(?m)^\s*❯\s*No, exit\s*$", screen):
                run(["tmux", "send-keys", "-t", name, "Down"], check=False, timeout=10)
                run(["tmux", "send-keys", "-t", name, "C-m"], check=False, timeout=10)
            elif re.search(r"(?m)^\s*❯\s*Yes, I accept\s*$", screen):
                run(["tmux", "send-keys", "-t", name, "C-m"], check=False, timeout=10)
            bypass_accepted = True
            time.sleep(0.8)
            continue
        if REPL_READY.search(screen):
            if not _screen_busy(screen):
                return True
        time.sleep(0.5)
    return False


def send_literal(name: str, text: str) -> None:
    run(["tmux", "send-keys", "-l", "-t", name, "--", text], timeout=10)
    run(["tmux", "send-keys", "-t", name, "C-m"], timeout=10)


def send_continue(name: str) -> None:
    # The literal is intentionally a constant.  No caller-controlled text is sent.
    run(["tmux", "send-keys", "-l", "-t", name, "--", CONTINUE_TEXT], timeout=10)
    run(["tmux", "send-keys", "-t", name, "C-m"], timeout=10)


def _docker_command(
    *,
    name: str,
    task_root: Path,
    workspace: Path,
    live: Path,
    image: str,
) -> list[str]:
    task_hash = sha256_bytes(str(task_root.resolve()).encode())[:12]
    return [
        "docker",
        "run",
        "-it",
        "--init",
        "--restart=no",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--name",
        name,
        "--label",
        "solo2611=true",
        "--label",
        f"solo2611.task={task_hash}",
        "--mount",
        f"type=bind,src={workspace},dst=/workspace",
        "--mount",
        f"type=bind,src={live},dst={CONTAINER_TRACE_DIR}",
        "-e",
        "apikey",
        "-e",
        "ANTHROPIC_BASE_URL",
        image,
        "interactive",
    ]


def launch_tmux_container(
    *,
    task_root: Path,
    state: dict[str, Any],
    attempt: int,
    image: str,
    key: str,
    base_url: str,
) -> dict[str, str]:
    workspace = Path(state["workspace"])
    directory = attempt_dir(task_root, attempt)
    live = directory / "live"
    live.mkdir(parents=True, exist_ok=True)
    os.chmod(live, 0o777)
    name = _container_name(str(state["taskName"]), attempt)
    tmux_name = _tmux_name(str(state["taskName"]), attempt)
    env_path = directory / "env.sh"
    launch_path = directory / "launch.sh"
    _write_private(
        env_path,
        "#!/bin/bash\n"
        f"export apikey={shlex.quote(key)}\n"
        f"export ANTHROPIC_BASE_URL={shlex.quote(base_url)}\n",
    )
    command = _docker_command(
        name=name,
        task_root=task_root,
        workspace=workspace,
        live=live,
        image=image,
    )
    _write_private(
        launch_path,
        "#!/bin/bash\nset -euo pipefail\n"
        f"source {shlex.quote(str(env_path))}\n"
        "exec " + " ".join(shlex.quote(part) for part in command) + "\n",
    )
    result = run(
        ["tmux", "new-session", "-d", "-s", tmux_name, "-c", str(workspace), str(launch_path)],
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(out(result) or result.stderr.decode(errors="replace"))
    return {"container": name, "tmux": tmux_name, "env": str(env_path), "launch": str(launch_path)}


def _remove_tmux_and_container(name: str, tmux_name: str) -> None:
    if tmux_name and tmux_alive(tmux_name):
        run(["tmux", "kill-session", "-t", tmux_name], check=False, timeout=10)
    if name and container_exists(name):
        run(["docker", "rm", "-f", name], check=False, timeout=60)


def _state_attempt(state: dict[str, Any], index: int) -> dict[str, Any] | None:
    for attempt in state.get("attempts") or []:
        if int(attempt.get("index") or 0) == index:
            return attempt
    return None


def prepare_attempt(task_root: Path, state: dict[str, Any], image: str) -> dict[str, Any]:
    index = len(state.get("attempts") or []) + 1
    directory = attempt_dir(task_root, index)
    for child in ("live", "traces"):
        (directory / child).mkdir(parents=True, exist_ok=True)
    if (directory / "live").is_dir():
        os.chmod(directory / "live", 0o777)
    now = utc_now()
    attempt = {
        "index": index,
        "container": "",
        "mode": "supervised",
        "image": image,
        "startedAt": now,
        "status": "running",
        "dir": str(directory),
        "streams": [],
        "rounds": 0,
        "continues": 0,
        "startFailed": False,
    }
    max_continue = effective_continue_max()
    config = load_config()
    total_seconds = float((config.get("claude") or {}).get("timeoutSeconds") or 10800)
    hold_minutes = float((config.get("claude") or {}).get("fatalHoldMinutes") or 30)
    state.setdefault("attempts", []).append(attempt)
    state["status"] = "running"
    state.pop("pendingContainer", None)
    state["sessionRun"] = {
        "schema": "solo2611/session-run/1",
        "mode": "supervised",
        "attemptIndex": index,
        "container": "",
        "tmuxSession": "",
        "supervisor": {},
        "liveTraceDir": str(directory / "live"),
        "status": "launching",
        "startedAt": now,
        "lastActivityAt": now,
        "deadline": {
            "totalAt": _plus_seconds(total_seconds),
            "noProgressAt": _plus_seconds(float((config.get("claude") or {}).get("noProgressSeconds") or 1200)),
            "holdUntil": None,
        },
        "nextActionAt": _plus_seconds(0),
        "continues": {"used": 0, "max": max_continue, "history": []},
        "rounds": [],
        "interrupts": [],
        "blocked": None,
        "abort": None,
        "outcome": None,
        "startAttempts": {"used": 0, "max": int((config.get("claude") or {}).get("startAttempts") or 2)},
    }
    append_event(task_root, index, "session.start", mode="supervised", attemptNo=index, maxContinues=max_continue)
    return attempt


def spawn_supervisor(
    *,
    task_root: Path,
    attempt: int,
    attach: bool = False,
) -> int:
    directory = attempt_dir(task_root, attempt)
    directory.mkdir(parents=True, exist_ok=True)
    log = (directory / "supervisor.out").open("ab", buffering=0)
    script = Path(__file__).resolve().parent / "solo2611.py"
    command = [
        sys.executable,
        str(script),
        "supervise",
        "--task-root",
        str(task_root),
        "--attempt",
        str(attempt),
    ]
    if attach:
        command.append("--attach")
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        env=env,
        start_new_session=True,
        close_fds=True,
    )
    log.close()
    return proc.pid


def start_supervised(
    task_root: Path,
    state: dict[str, Any],
    attempt_index: int,
) -> dict[str, Any]:
    config = load_config()
    claude = config.get("claude") or {}
    image = str(claude.get("image") or "")
    key = os.environ.get("SOLO2611_API_KEY", "").strip() or str(claude.get("apiKey") or "").strip()
    base_url = os.environ.get("SOLO2611_BASE_URL", "").strip() or str(claude.get("baseUrl") or "").strip()
    if not key:
        raise Solo2611Error("缺少本期团队 API Key")
    attempt = _state_attempt(state, attempt_index)
    if attempt is None:
        raise Solo2611Error(f"找不到 attempt {attempt_index}")
    attempt["startedAt"] = utc_now()
    launched = launch_tmux_container(
        task_root=task_root,
        state=state,
        attempt=attempt_index,
        image=image,
        key=key,
        base_url=base_url,
    )
    attempt["container"] = launched["container"]
    session = state["sessionRun"]
    session["container"] = launched["container"]
    session["tmuxSession"] = launched["tmux"]
    session["startAttempts"]["used"] = int(session["startAttempts"].get("used") or 0) + 1
    pid = spawn_supervisor(task_root=task_root, attempt=attempt_index)
    session["supervisor"] = {
        "pid": pid,
        "pgid": pid,
        "startedAt": utc_now(),
        "heartbeatAt": utc_now(),
    }
    save_state(task_root, state)
    return {
        "ok": True,
        "supervised": True,
        "attempt": attempt_index,
        "container": launched["container"],
        "tmux": launched["tmux"],
        "supervisor": pid,
        "next": "run-status",
    }


def _heartbeat(state: dict[str, Any]) -> None:
    session = state.setdefault("sessionRun", {})
    supervisor = session.setdefault("supervisor", {})
    supervisor["heartbeatAt"] = utc_now()


def _refresh_last_activity(state: dict[str, Any], *, progress: bool = False) -> None:
    session = state.setdefault("sessionRun", {})
    session["lastActivityAt"] = utc_now()
    if progress:
        config = load_config()
        seconds = float((config.get("claude") or {}).get("noProgressSeconds") or 1200)
        session.setdefault("deadline", {})["noProgressAt"] = _plus_seconds(seconds)


def _terminal(state: dict[str, Any]) -> bool:
    return str((state.get("sessionRun") or {}).get("status") or "") in TERMINAL


def _consume_commands(task_root: Path, attempt: int) -> list[dict[str, Any]]:
    path = commands_log(task_root, attempt)
    cursor_path = attempt_dir(task_root, attempt) / ".commands.cursor"
    offset = 0
    with suppress(OSError, ValueError):
        offset = int(cursor_path.read_text(encoding="utf-8"))
    if not path.is_file():
        return []
    size = path.stat().st_size
    if size <= offset:
        return []
    with path.open("rb") as handle:
        handle.seek(offset)
        chunk = handle.read()
    cursor_path.write_text(str(size), encoding="utf-8")
    rows: list[dict[str, Any]] = []
    for line in chunk.decode("utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def queue_continue(task_root: Path, attempt: int, *, note: str = "") -> None:
    path = commands_log(task_root, attempt)
    path.parent.mkdir(parents=True, exist_ok=True)
    item = {"at": utc_now(), "command": "continue", "note": note}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(item, ensure_ascii=False) + "\n")


def human_continue(task_root: Path, *, note: str = "") -> dict[str, Any]:
    state = load_state(task_root)
    session = state.get("sessionRun") or {}
    if _terminal(state):
        raise Solo2611Error("会话已终止，不能继续")
    if session.get("mode") != "supervised":
        raise Solo2611Error("print 模式不支持继续")
    continues = session.get("continues") or {}
    used, maximum = int(continues.get("used") or 0), int(continues.get("max") or 0)
    if used >= maximum:
        raise Solo2611Error("继续次数已用尽")
    if not supervisor_alive(state, task_root):
        raise Solo2611Error("监督器不在线；先运行 resume")
    attempt = int(session.get("attemptIndex") or 0)
    before = len(read_events(task_root, attempt))
    queue_continue(task_root, attempt, note=note)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        events = read_events(task_root, attempt)[before:]
        if any(event.get("event") == "continue.sent" for event in events):
            return {"ok": True, "sent": True, "used": int((load_state(task_root).get("sessionRun") or {}).get("continues", {}).get("used") or 0)}
        time.sleep(0.2)
    return {"ok": True, "sent": False, "queued": True, "message": "继续请求已排队，监督器会在输入框空闲后发送"}


def _abort_note(task_root: Path, state: dict[str, Any], reason: str, intent: str) -> Path:
    session = state.get("sessionRun") or {}
    directory = attempt_dir(task_root, int(session.get("attemptIndex") or 1))
    rounds = session.get("rounds") or []
    last = rounds[-1] if rounds else {}
    text = "\n".join(
        [
            "# 终止说明",
            "",
            f"- 原因：{reason}",
            f"- 触发：{intent}",
            f"- 已继续：{int((session.get('continues') or {}).get('used') or 0)} 次",
            f"- 已运行轮次：{len(rounds)}",
            f"- 最近中断：{last.get('kind') or '无'}",
            f"- 事件日志：`{supervisor_log(task_root, int(session.get('attemptIndex') or 1))}`",
            f"- 实时轨迹目录：`{session.get('liveTraceDir') or ''}`",
            "",
            "现场已保留，未恢复工作区。被终止的题应换题，不支持整题重跑。",
            "",
        ]
    )
    path = task_root / "delivery" / "终止说明.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def abort(task_root: Path, reason: str, *, by: str = "auto", detail: str = "") -> dict[str, Any]:
    """The only function allowed to stop and remove a managed session container."""
    state = load_state(task_root)
    session = state.get("sessionRun") or {}
    if _terminal(state):
        return {"ok": False, "status": session.get("status"), "message": "终态不可改变"}
    attempt = int(session.get("attemptIndex") or 0)
    container = str(session.get("container") or "")
    tmux_name = str(session.get("tmuxSession") or "")
    session["status"] = "closing"
    save_state(task_root, state)
    append_event(task_root, attempt, "session.closing", reason=reason, by=by)
    screen = capture_pane(tmux_name, lines=200)
    if tmux_name and tmux_alive(tmux_name):
        for key in ("C-c", "C-d", "C-d"):
            run(["tmux", "send-keys", "-t", tmux_name, key], check=False, timeout=10)
            time.sleep(0.5)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and tmux_alive(tmux_name):
            time.sleep(0.5)
    if container and container_exists(container):
        run(["docker", "stop", "-t", "10", container], check=False, timeout=30)
        run(["docker", "rm", container], check=False, timeout=60)
    if tmux_name and tmux_alive(tmux_name):
        run(["tmux", "kill-session", "-t", tmux_name], check=False, timeout=10)
    directory = attempt_dir(task_root, attempt)
    buffer = directory / "tmux.buffer.log"
    buffer.write_text(screen, encoding="utf-8")
    live = directory / "live"
    traces = directory / "traces"
    if live.is_dir():
        try:
            if traces.exists():
                import shutil

                shutil.rmtree(traces)
            import shutil

            shutil.copytree(live, traces)
            os.chmod(live, 0o555)
        except OSError:
            with suppress(OSError):
                os.chmod(live, 0o555)
    for name in ("env.sh", "launch.sh"):
        (directory / name).unlink(missing_ok=True)
    state = load_state(task_root)
    session = state.setdefault("sessionRun", {})
    session["status"] = "aborted"
    session["abort"] = {"reason": reason, "detail": detail, "at": utc_now(), "by": by}
    session["blocked"] = None
    session["nextActionAt"] = None
    state["status"] = "session_aborted"
    current_attempt = _state_attempt(state, attempt)
    if current_attempt is not None:
        current_attempt["status"] = "aborted"
        current_attempt["finishedAt"] = utc_now()
        current_attempt.setdefault("errors", []).append(reason)
    note = _abort_note(task_root, state, reason, detail or by)
    save_state(task_root, state)
    append_event(task_root, attempt, "session.aborted", reason=reason, by=by, note=str(note))
    return {"ok": True, "status": "aborted", "reason": reason, "note": str(note)}


def _round_number(events: list[dict[str, Any]]) -> int:
    return max(1, len(_main_user_events(events)))


def _record_round(
    state: dict[str, Any],
    events: list[dict[str, Any]],
    classification: dict[str, Any],
) -> dict[str, Any]:
    session = state.setdefault("sessionRun", {})
    rounds = session.setdefault("rounds", [])
    number = _round_number(events)
    info = tr.analyze_from_events(events)
    item = {
        "no": number,
        "startedAt": session.get("lastRoundStartedAt") or session.get("startedAt"),
        "endedAt": utc_now(),
        "stopReason": info.get("stopReason") or "",
        "kind": classification["kind"],
        "verdict": classification["verdict"],
        "detail": classification.get("detail") or "",
        "toolUses": int(info.get("toolUses") or 0),
        "finalChars": len(str(info.get("finalText") or "")),
        "continued": False,
    }
    if rounds and int(rounds[-1].get("no") or 0) == number:
        rounds[-1] = item
    else:
        rounds.append(item)
    if classification["verdict"] != "complete":
        interrupts = session.setdefault("interrupts", [])
        interrupts.append({"round": number, "kind": classification["kind"], "at": utc_now()})
    return item


def _copy_live_to_traces(task_root: Path, attempt: int) -> Path | None:
    directory = attempt_dir(task_root, attempt)
    live = directory / "live"
    traces = directory / "traces"
    if not live.is_dir() or not _trace_files(live):
        return None
    if traces.exists():
        import shutil

        shutil.rmtree(traces)
    import shutil

    shutil.copytree(live, traces)
    return traces


def _close_and_accept(task_root: Path, attempt_index: int) -> dict[str, Any]:
    state = load_state(task_root)
    session = state.setdefault("sessionRun", {})
    session["status"] = "closing"
    session["nextActionAt"] = None
    save_state(task_root, state)
    append_event(task_root, attempt_index, "session.closing")
    tmux_name = str(session.get("tmuxSession") or "")
    container = str(session.get("container") or "")
    if tmux_name and tmux_alive(tmux_name):
        run(["tmux", "send-keys", "-t", tmux_name, "C-d"], check=False, timeout=10)
        time.sleep(1)
        run(["tmux", "send-keys", "-t", tmux_name, "C-d"], check=False, timeout=10)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and tmux_alive(tmux_name):
            time.sleep(0.5)
        if tmux_alive(tmux_name):
            # Claude Code 2.1.319 keeps the REPL open on C-d. Closing the
            # attached tmux client sends EOF to docker, which exits the
            # container cleanly and leaves the bind-mounted trace intact.
            append_event(task_root, attempt_index, "session.closing_fallback", reason="tmux_eof")
            run(["tmux", "kill-session", "-t", tmux_name], check=False, timeout=10)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and container_running(container):
                time.sleep(0.5)
    copied = _copy_live_to_traces(task_root, attempt_index)
    import solo2611 as cli

    attempt = _state_attempt(state, attempt_index) or {}
    attempt["finishedAt"] = utc_now()
    result = attempt.get("result") if isinstance(attempt.get("result"), dict) else {}
    try:
        info = cli._collect_container(task_root, state, container, attempt_dir(task_root, attempt_index), result)
    except Exception:
        if copied is not None:
            # Collection preserves the container on failure; leave the evidence and
            # expose the error instead of pretending that the session was accepted.
            append_event(task_root, attempt_index, "session.aborted", reason="collect_failed")
        raise
    attempt["completion"] = info.get("completion") or {}
    attempt["errors"] = list(info.get("errors") or [])
    attempt["rounds"] = len(session.get("rounds") or [])
    attempt["continues"] = int((session.get("continues") or {}).get("used") or 0)
    if not info.get("ok"):
        return abort(
            task_root,
            "validation_failed",
            by="auto",
            detail="；".join(info.get("errors") or []),
        )
    attempt["status"] = "accepted"
    cli._accept_attempt(task_root, state, attempt, info)
    state = load_state(task_root)
    session = state.setdefault("sessionRun", {})
    used = int((session.get("continues") or {}).get("used") or 0)
    session["status"] = "accepted"
    session["nextActionAt"] = None
    session["blocked"] = None
    session["outcome"] = {
        "acceptedAt": utc_now(),
        "remark": f"模型运行中途中断，人工输入\"继续\"{used}次后完成" if used else "",
    }
    attempt = _state_attempt(state, attempt_index)
    if attempt is not None:
        attempt["status"] = "accepted"
        attempt["finishedAt"] = utc_now()
        attempt["rounds"] = len(session.get("rounds") or [])
        attempt["continues"] = used
    save_state(task_root, state)
    append_event(
        task_root,
        attempt_index,
        "session.accepted",
        sessionId=info.get("sessionId"),
        promptId=info.get("promptId"),
        continues=used,
    )
    for name in ("env.sh", "launch.sh"):
        (attempt_dir(task_root, attempt_index) / name).unlink(missing_ok=True)
    with suppress(OSError):
        os.chmod(attempt_dir(task_root, attempt_index) / "live", 0o555)
    return {"ok": True, "status": "accepted", "sessionId": info.get("sessionId"), "promptId": info.get("promptId")}


def _elapsed_since(value: Any) -> float:
    stamp = _parse_time(value)
    if stamp is None:
        return 0.0
    now = datetime.now(stamp.tzinfo or timezone.utc)
    return max(0.0, (now - stamp).total_seconds())


def _handle_human_commands(
    task_root: Path,
    state: dict[str, Any],
    attempt: int,
    commands: list[dict[str, Any]],
) -> bool:
    session = state.setdefault("sessionRun", {})
    changed = False
    for command in commands:
        if command.get("command") != "continue":
            continue
        continues = session.setdefault("continues", {"used": 0, "max": 0, "history": []})
        used, maximum = int(continues.get("used") or 0), min(CONTINUE_HARD_LIMIT, int(continues.get("max") or 0))
        if used >= maximum:
            append_event(task_root, attempt, "continue.rejected", reason="continues_exhausted", sentBy="human")
            continue
        screen = capture_pane(str(session.get("tmuxSession") or ""), lines=15)
        if _screen_busy(screen):
            append_event(task_root, attempt, "continue.deferred", reason="screen_busy", sentBy="human")
            continue
        send_continue(str(session.get("tmuxSession") or ""))
        used += 1
        continues["used"] = used
        history = continues.setdefault("history", [])
        history.append(
            {
                "no": used,
                "at": utc_now(),
                "sentBy": "human",
                "reason": "manual",
                "round": _round_number(tr.load_events(_latest_trace(Path(session["liveTraceDir"])))) if _latest_trace(Path(session["liveTraceDir"])) else 0,
                "waitedMs": 0,
                "note": str(command.get("note") or ""),
            }
        )
        previous_blocked = session.get("blocked")
        session["status"] = "running"
        session["blocked"] = None
        session["lastContinueAt"] = utc_now()
        _refresh_last_activity(state, progress=True)
        append_event(task_root, attempt, "continue.sent", no=used, sentBy="human", reason="manual")
        append_event(task_root, attempt, "round.start", round=used + 1)
        if previous_blocked:
            append_event(
                task_root,
                attempt,
                "hold.clear",
                kind=(previous_blocked or {}).get("kind"),
                by="human",
            )
        changed = True
    return changed


def _wait_seconds(kind: str, classification: dict[str, Any]) -> float:
    value = float(classification.get("waitSeconds") or 0)
    return max(0.0, value)


def supervisor_loop(task_root: Path, attempt: int, *, attach: bool = False) -> None:
    lock_path = task_root / "monitor" / ".supervisor.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        lock.seek(0)
        lock.truncate()
        lock.write(f"{os.getpid()}:{os.getpgrp()}:{utc_now()}")
        lock.flush()
        state = load_state(task_root)
        session = state.get("sessionRun") or {}
        if _terminal(state):
            return
        config = load_config()
        claude = config.get("claude") or {}
        image = str(claude.get("image") or "")
        max_continue = effective_continue_max(config)
        session.setdefault("continues", {})["max"] = max_continue
        session["supervisor"] = {
            "pid": os.getpid(),
            "pgid": os.getpgrp(),
            "startedAt": utc_now(),
            "heartbeatAt": utc_now(),
        }
        save_state(task_root, state)
        prompt_text = Path(state["promptPath"]).read_text(encoding="utf-8")
        directory = attempt_dir(task_root, attempt)
        live = directory / "live"
        prompt_sent = bool(session.get("promptSent"))
        last_size = 0
        last_growth_at = time.monotonic()
        last_heartbeat = time.monotonic()
        gateway_failures = sum(1 for item in session.get("rounds") or [] if item.get("kind") == "gateway")
        try:
            if not prompt_sent and not _trace_files(live):
                ready = wait_for_repl(str(session.get("tmuxSession") or ""), timeout=60)
                while not ready and not prompt_sent and not _trace_files(live):
                    current_attempt = _state_attempt(state, attempt) or {}
                    current_attempt["startFailed"] = True
                    start_state = session.setdefault("startAttempts", {"used": 0, "max": 2})
                    used = int(start_state.get("used") or 0)
                    maximum = min(2, max(1, int(start_state.get("max") or 2)))
                    append_event(
                        task_root,
                        attempt,
                        "session.start_failed",
                        attemptUsed=used,
                        maxAttempts=maximum,
                    )
                    _remove_tmux_and_container(
                        str(session.get("container") or ""),
                        str(session.get("tmuxSession") or ""),
                    )
                    for name in ("env.sh", "launch.sh"):
                        (directory / name).unlink(missing_ok=True)
                    if used >= maximum:
                        session["status"] = "start_failed"
                        session["error"] = "启动失败次数已用尽"
                        current_attempt["status"] = "start_failed"
                        current_attempt["finishedAt"] = utc_now()
                        state["status"] = "prompt_ready"
                        save_state(task_root, state)
                        return
                    current_attempt["status"] = "running"
                    save_state(task_root, state)
                    config = load_config()
                    spacing = max(5, int((config.get("claude") or {}).get("launchSpacingSeconds") or 20))
                    time.sleep(spacing)
                    config = load_config()
                    claude = config.get("claude") or {}
                    key = os.environ.get("SOLO2611_API_KEY", "").strip() or str(claude.get("apiKey") or "").strip()
                    url = os.environ.get("SOLO2611_BASE_URL", "").strip() or str(claude.get("baseUrl") or "").strip()
                    launched = launch_tmux_container(
                        task_root=task_root,
                        state=state,
                        attempt=attempt,
                        image=str(claude.get("image") or ""),
                        key=key,
                        base_url=url,
                    )
                    start_state["used"] = used + 1
                    session["container"] = launched["container"]
                    session["tmuxSession"] = launched["tmux"]
                    current_attempt["container"] = launched["container"]
                    current_attempt["startFailed"] = False
                    save_state(task_root, state)
                    ready = wait_for_repl(str(session.get("tmuxSession") or ""), timeout=60)
                if ready and not prompt_sent:
                    send_literal(str(session.get("tmuxSession") or ""), prompt_text)
                    prompt_sent = True
                    session["promptSent"] = True
                    session["status"] = "prompt_sent"
                    session["lastRoundStartedAt"] = utc_now()
                    _refresh_last_activity(state, progress=True)
                    append_event(
                        task_root,
                        attempt,
                        "prompt.sent",
                        chars=len(prompt_text),
                        sha256=sha256_bytes(prompt_text.encode("utf-8")),
                    )
                    append_event(task_root, attempt, "round.start", round=1)
                    for name in ("env.sh", "launch.sh"):
                        (directory / name).unlink(missing_ok=True)
                    save_state(task_root, state)
            while True:
                state = load_state(task_root)
                session = state.get("sessionRun") or {}
                if _terminal(state):
                    return
                now = time.monotonic()
                if now - last_heartbeat >= 60:
                    last_heartbeat = now
                    _heartbeat(state)
                    save_state(task_root, state)
                    append_event(task_root, attempt, "supervisor.heartbeat", pid=os.getpid())
                commands = _consume_commands(task_root, attempt)
                if commands and _handle_human_commands(task_root, state, attempt, commands):
                    save_state(task_root, state)
                    gateway_failures = sum(
                        1 for item in session.get("rounds") or [] if item.get("kind") == "gateway"
                    )
                path = _latest_trace(live)
                if path is not None:
                    size = path.stat().st_size
                    if size != last_size:
                        last_size = size
                        last_growth_at = now
                        _refresh_last_activity(state, progress=True)
                        save_state(task_root, state)
                    events = tr.load_events(path)
                    screen = capture_pane(str(session.get("tmuxSession") or ""), lines=30)
                    if events and session.get("status") not in {"holding", "manual_input"}:
                        if round_ended(
                            events,
                            file_size=size,
                            previous_size=last_size,
                            last_growth_at=last_growth_at,
                            screen=screen,
                            now=now,
                        ):
                            classification = classify_round(
                                tr.analyze_from_events(events, tolerate_incomplete=True),
                                None,
                                events,
                                gateway_failures=gateway_failures,
                            )
                            number = _round_number(events)
                            existing = session.get("rounds") or []
                            if not existing or int(existing[-1].get("no") or 0) != number:
                                item = _record_round(state, events, classification)
                                append_event(
                                    task_root,
                                    attempt,
                                    "round.end",
                                    round=number,
                                    kind=classification["kind"],
                                    verdict=classification["verdict"],
                                    detail=classification.get("detail", "")[:800],
                                    waitSeconds=classification.get("waitSeconds", 0),
                                )
                                session = state["sessionRun"]
                                if classification["verdict"] == "complete":
                                    save_state(task_root, state)
                                    result = _close_and_accept(task_root, attempt)
                                    print(json.dumps(result, ensure_ascii=False))
                                    return
                                if classification["verdict"] == "hold":
                                    hold_minutes = float(claude.get("fatalHoldMinutes") or 30)
                                    session["status"] = "holding"
                                    session["blocked"] = {
                                        "kind": classification["kind"],
                                        "since": utc_now(),
                                        "until": _plus_seconds(hold_minutes * 60),
                                    }
                                    session["deadline"]["holdUntil"] = session["blocked"]["until"]
                                    session["nextActionAt"] = None
                                    save_state(task_root, state)
                                    append_event(
                                        task_root,
                                        attempt,
                                        "hold.start",
                                        kind=classification["kind"],
                                        until=session["blocked"]["until"],
                                    )
                                    continue
                                continues = session.setdefault(
                                    "continues", {"used": 0, "max": max_continue, "history": []}
                                )
                                used = int(continues.get("used") or 0)
                                if used >= max_continue:
                                    abort(task_root, "continues_exhausted", by="auto")
                                    return
                                wait = _wait_seconds(classification["kind"], classification)
                                session["status"] = "continuing"
                                session["nextActionAt"] = _plus_seconds(wait)
                                session["pendingContinue"] = classification["kind"]
                                save_state(task_root, state)
                                if wait:
                                    time.sleep(wait)
                                state = load_state(task_root)
                                session = state.get("sessionRun") or {}
                                if _terminal(state):
                                    return
                                send_continue(str(session.get("tmuxSession") or ""))
                                used += 1
                                continues = session.setdefault(
                                    "continues", {"used": 0, "max": max_continue, "history": []}
                                )
                                continues["used"] = used
                                continues.setdefault("history", []).append(
                                    {
                                        "no": used,
                                        "at": utc_now(),
                                        "sentBy": "auto",
                                        "reason": classification["kind"],
                                        "round": number,
                                        "waitedMs": int(wait * 1000),
                                    }
                                )
                                item["continued"] = True
                                session["status"] = "running"
                                session["pendingContinue"] = None
                                session["nextActionAt"] = None
                                session["lastContinueAt"] = utc_now()
                                session["lastRoundStartedAt"] = utc_now()
                                _refresh_last_activity(state, progress=True)
                                save_state(task_root, state)
                                append_event(
                                    task_root,
                                    attempt,
                                    "continue.sent",
                                    no=used,
                                    sentBy="auto",
                                    reason=classification["kind"],
                                    waitedMs=int(wait * 1000),
                                )
                                append_event(task_root, attempt, "round.start", round=number + 1)
                                if classification["kind"] == "gateway":
                                    gateway_failures += 1
                if session.get("status") == "holding":
                    blocked = session.get("blocked") or {}
                    until = _parse_time(blocked.get("until"))
                    if until and datetime.now(until.tzinfo or timezone.utc) >= until:
                        abort(task_root, "hold_timeout", by="auto")
                        return
                last_continue = _parse_time(session.get("lastContinueAt"))
                if (
                    last_continue
                    and session.get("status") == "running"
                    and (datetime.now(last_continue.tzinfo or timezone.utc) - last_continue).total_seconds() >= 180
                ):
                    prompts = _main_user_events(path and tr.load_events(path) or [])
                    expected = 1 + int((session.get("continues") or {}).get("used") or 0)
                    if len(prompts) < expected and session.get("status") != "manual_input":
                        session["status"] = "manual_input"
                        session["blocked"] = {
                            "kind": "unresponsive",
                            "since": utc_now(),
                            "until": _plus_seconds(float(claude.get("fatalHoldMinutes") or 30) * 60),
                        }
                        save_state(task_root, state)
                        append_event(task_root, attempt, "manual_input", reason="unresponsive")
                if session.get("status") == "manual_input":
                    blocked = session.get("blocked") or {}
                    until = _parse_time(blocked.get("until"))
                    if until and datetime.now(until.tzinfo or timezone.utc) >= until:
                        abort(task_root, "manual_timeout", by="auto")
                        return
                total_at = _parse_time((session.get("deadline") or {}).get("totalAt"))
                if total_at and datetime.now(total_at.tzinfo or timezone.utc) >= total_at:
                    abort(task_root, "run_timeout", by="auto")
                    return
                no_progress_at = _parse_time((session.get("deadline") or {}).get("noProgressAt"))
                if (
                    no_progress_at
                    and session.get("status") not in {"holding", "manual_input", "continuing"}
                    and datetime.now(no_progress_at.tzinfo or timezone.utc) >= no_progress_at
                ):
                    abort(task_root, "no_progress", by="auto")
                    return
                time.sleep(2)
        except Exception as exc:
            state = load_state(task_root)
            session = state.setdefault("sessionRun", {})
            if str(session.get("status") or "") not in TERMINAL | {"holding", "manual_input"}:
                session["status"] = "session_lost"
                session["error"] = f"{type(exc).__name__}: {exc}"
                save_state(task_root, state)
                append_event(task_root, attempt, "session.lost", error=session["error"])
            raise


def resume_session(task_root: Path) -> dict[str, Any]:
    state = load_state(task_root)
    session = state.get("sessionRun") or {}
    if session.get("mode") == "print":
        import resume as print_resume

        return print_resume.resume_print(task_root)
    status = str(session.get("status") or "")
    if status == "accepted":
        return {"ok": True, "status": "accepted", "supervised": False}
    if status == "aborted":
        return {"ok": True, "status": "aborted", "supervised": False}
    if supervisor_alive(state, task_root):
        return {"ok": True, "status": status or "running", "supervised": False, "message": "监督器已在线"}
    attempt = int(session.get("attemptIndex") or 0)
    container = str(session.get("container") or "")
    tmux_name = str(session.get("tmuxSession") or "")
    if tmux_name and tmux_alive(tmux_name) and container_running(container):
        pid = spawn_supervisor(task_root=task_root, attempt=attempt, attach=True)
        state = load_state(task_root)
        session = state.setdefault("sessionRun", {})
        session["supervisor"] = {
            "pid": pid,
            "pgid": pid,
            "startedAt": utc_now(),
            "heartbeatAt": utc_now(),
        }
        save_state(task_root, state)
        return {"ok": True, "status": "running", "supervised": True, "supervisor": pid, "message": "监督器已接回"}
    current_attempt = _state_attempt(state, attempt)
    if current_attempt is not None:
        current_attempt["status"] = "lost"
        current_attempt["errors"] = list(current_attempt.get("errors") or []) + ["会话容器或 tmux 已不存在"]
    session["status"] = "session_lost"
    session["error"] = "会话容器或 tmux 已不存在，不能重启容器"
    save_state(task_root, state)
    append_event(task_root, attempt, "session.lost", reason="container_or_tmux_missing")
    return {
        "ok": False,
        "status": "session_lost",
        "supervised": False,
        "message": "会话已无法接回；不自动重跑，请换题",
    }


def status_view(task_root: Path, *, log_lines: int = 20) -> dict[str, Any]:
    state = load_state(task_root)
    session = state.get("sessionRun") or {}
    attempt = int(session.get("attemptIndex") or 0)
    rounds = session.get("rounds") or []
    continues = session.get("continues") or {"used": 0, "max": effective_continue_max()}
    blocked = session.get("blocked") or {}
    total_at = _parse_time((session.get("deadline") or {}).get("totalAt"))
    remaining = 0
    if total_at:
        remaining = int(
            max(
                0,
                (total_at - datetime.now(total_at.tzinfo or timezone.utc)).total_seconds(),
            )
        )
    state_value = str(session.get("status") or state.get("status") or "unknown")
    session_state = {
        "launching": "running",
        "prompt_sent": "running",
        "running": "running",
        "continuing": "waiting_continue",
        "holding": "holding",
        "manual_input": "manual_input",
        "closing": "running",
        "accepted": "accepted",
        "aborted": "aborted",
        "session_lost": "session_lost",
    }.get(state_value, state_value)
    container = str(session.get("container") or "")
    last_kind = ""
    if rounds:
        last_kind = str(rounds[-1].get("kind") or "")
    if not last_kind and session.get("interrupts"):
        last_kind = str(session["interrupts"][-1].get("kind") or "")
    return {
        "status": state_value,
        "startedAt": session.get("startedAt") or "",
        "sessionState": session_state,
        "round": len(rounds),
        "rounds": rounds,
        "continues": {
            "used": int(continues.get("used") or 0),
            "max": int(continues.get("max") or effective_continue_max()),
            "history": continues.get("history") or [],
        },
        "lastKind": last_kind,
        "lastActivityAt": session.get("lastActivityAt"),
        "nextActionAt": session.get("nextActionAt"),
        "remainingSeconds": remaining,
        "supervisorAlive": supervisor_alive(state, task_root),
        "container": container,
        "containerRunning": container_running(container),
        "tmuxSession": session.get("tmuxSession") or "",
        "tmuxAlive": tmux_alive(str(session.get("tmuxSession") or "")),
        "sessionId": (state.get("session") or {}).get("sessionId") or "",
        "promptId": (state.get("session") or {}).get("promptId") or "",
        "blocked": blocked or None,
        "abort": session.get("abort"),
        "outcome": session.get("outcome"),
        "logTail": tail_log(supervisor_log(task_root, attempt), log_lines),
        "attempt": attempt,
        "mode": session.get("mode") or "",
    }


def supervise_all(tasks_dir: Path, *, dry_run: bool = False) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for state_file in sorted(tasks_dir.glob("*/monitor/state.json")):
        task_root = state_file.parent.parent
        try:
            state = load_state(task_root)
        except Exception:
            continue
        status = str((state.get("sessionRun") or {}).get("status") or state.get("status") or "")
        if status in TERMINAL or status in {"start_failed", "prepared", "prompt_ready"}:
            continue
        item = {"taskRoot": str(task_root), "status": status}
        if not dry_run:
            try:
                item.update(resume_session(task_root))
            except Exception as exc:
                item["ok"] = False
                item["error"] = str(exc)
        items.append(item)
    return {"ok": all(item.get("ok", True) for item in items), "dryRun": dry_run, "items": items}


@contextmanager
def _noop() -> Iterator[None]:
    yield

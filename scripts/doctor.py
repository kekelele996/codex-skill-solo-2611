"""Read-only baselines and safe repair of stale task state."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from common import load_state, read_json, save_state, utc_now, write_json
from session import container_exists, supervisor_alive


FAILURE_CLASSES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("rate_limited", re.compile(r"(?i)\b429\b|rate.?limit|tokens per minute")),
    ("gateway", re.compile(r"(?i)\b5\d\d\b|overloaded|bad gateway|service unavailable|网关")),
    ("auth", re.compile(r"(?i)\b401\b|\b403\b|auth|鉴权")),
    ("quota", re.compile(r"(?i)quota|额度|余额")),
    ("truncated", re.compile(r"(?i)截断|中断|max_tokens|上下文")),
    ("timeout", re.compile(r"(?i)超时|timeout")),
    ("container", re.compile(r"(?i)container|容器|docker")),
)


def _parse(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _duration(attempt: dict[str, Any]) -> float:
    started = _parse(attempt.get("startedAt"))
    finished = _parse(attempt.get("finishedAt"))
    if started is None or finished is None:
        return 0.0
    return max(0.0, (finished - started).total_seconds())


def _reason(attempt: dict[str, Any]) -> str:
    parts = [str(item) for item in attempt.get("errors") or []]
    completion = attempt.get("completion") or {}
    parts.extend(str(item) for item in completion.get("errors") or [])
    if attempt.get("timeout"):
        parts.append("timeout")
    text = "；".join(parts) or str(attempt.get("detail") or "")
    for name, pattern in FAILURE_CLASSES:
        if pattern.search(text):
            return name
    return "other" if text else "unknown"


def baseline(tasks_dir: Path) -> dict[str, Any]:
    states = 0
    attempt_status: Counter[str] = Counter()
    failure_reasons: Counter[str] = Counter()
    nested_attempts: Counter[int] = Counter()
    total_duration = 0.0
    accepted_duration = 0.0
    unproductive_duration = 0.0
    accepted = 0
    for state_file in sorted(tasks_dir.glob("*/monitor/state.json")):
        data = read_json(state_file, {}) or {}
        if not isinstance(data, dict):
            continue
        states += 1
        attempts = [item for item in data.get("attempts") or [] if isinstance(item, dict)]
        nested_attempts[len(attempts)] += 1
        for attempt in attempts:
            status = str(attempt.get("status") or "unknown")
            attempt_status[status] += 1
            duration = _duration(attempt)
            total_duration += duration
            if status == "accepted":
                accepted += 1
                accepted_duration += duration
            else:
                unproductive_duration += duration
                failure_reasons[_reason(attempt)] += 1
    hours = total_duration / 3600
    unproductive_share = unproductive_duration / total_duration if total_duration else 0.0
    return {
        "schema": "solo2611/baseline/1",
        "generatedAt": utc_now(),
        "tasksDir": str(tasks_dir),
        "tasks": states,
        "attempts": sum(attempt_status.values()),
        "attemptStatus": dict(sorted(attempt_status.items())),
        "attemptsPerTask": dict(sorted(nested_attempts.items())),
        "failureReasons": dict(failure_reasons.most_common()),
        "containerSeconds": round(total_duration, 1),
        "containerHours": round(hours, 3),
        "accepted": accepted,
        "acceptedContainerSeconds": round(accepted_duration, 1),
        "unproductiveContainerSeconds": round(unproductive_duration, 1),
        "unproductiveContainerShare": round(unproductive_share, 4),
        "sessionsPerContainerHour": round(accepted / hours, 4) if hours else 0.0,
    }


def fix_stale(tasks_dir: Path) -> dict[str, Any]:
    fixed: list[dict[str, Any]] = []
    for state_file in sorted(tasks_dir.glob("*/monitor/state.json")):
        task_root = state_file.parent.parent
        try:
            state = load_state(task_root)
        except Exception:
            continue
        if state.get("status") != "running":
            continue
        session = state.get("sessionRun") or {}
        if supervisor_alive(state, task_root):
            continue
        container = str(session.get("container") or "")
        if container and container_exists(container):
            continue
        stale = [
            attempt
            for attempt in state.get("attempts") or []
            if isinstance(attempt, dict) and attempt.get("status") == "running"
        ]
        if not stale and session.get("status") not in {"launching", "prompt_sent", "running", "continuing"}:
            continue
        for attempt in stale:
            attempt["status"] = "lost"
            attempt["finishedAt"] = utc_now()
            attempt.setdefault("errors", []).append("doctor: 无人看管且容器不存在")
        if session:
            session["status"] = "lost"
            session["error"] = "无人看管且容器不存在"
        save_state(task_root, state)
        fixed.append({"taskRoot": str(task_root), "attempts": len(stale) or 1})
    return {"ok": True, "fixed": fixed, "count": len(fixed)}


def write_baseline(tasks_dir: Path, output: Path) -> dict[str, Any]:
    data = baseline(tasks_dir)
    write_json(output, data)
    return {"ok": True, "output": str(output), "summary": data}

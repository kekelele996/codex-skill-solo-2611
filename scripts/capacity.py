"""Machine-wide container capacity: one gate for every way a container starts.

``docker ps`` is the source of truth for containers that are already running.
A container that has been admitted but is not visible there yet is covered by
a short-lived lease, so two launchers can never both see "one slot free" and
both start.  Counting and admitting happen under one file lock.

Rules that keep the count from drifting above the limit:

* every launcher (first run, supervisor restart, print mode) calls ``acquire``;
* when Docker cannot be asked, nothing is admitted (fail closed);
* a lease only covers the gap before Docker knows the container.  It is
  dropped as soon as the container exists in any state (running: Docker
  counts it from then on; exited: the slot is free again), when the container
  is removed through the skill, or when it is older than
  ``LEASE_TTL_SECONDS`` without one, so neither a crashed launcher nor a
  container that died early can hold a slot.
"""

from __future__ import annotations

import hashlib
import sys
import time
from pathlib import Path
from typing import Any

from common import STATE_HOME, Solo2611Error, file_lock, out, read_json, run, utc_now, write_json

CONTAINER_LOCK = STATE_HOME / "containers.lock"
LEASE_FILE = STATE_HOME / "container-leases.json"
LEASE_TTL_SECONDS = 900.0
POLL_SECONDS = 5.0
DEFAULT_WAIT_SECONDS = 1800.0
MANAGED_PREFIX = "solo2611-"


def holder_key(task_root: Path) -> str:
    return "task:" + hashlib.sha256(str(Path(task_root).resolve()).encode()).hexdigest()[:16]


def _parse_managed_names(text: str) -> set[str]:
    """Accept both current labels and legacy managed names without a label."""
    names: set[str] = set()
    for raw in (text or "").splitlines():
        name, _, label = raw.partition("\t")
        name = name.strip()
        if not name:
            continue
        if name.startswith(MANAGED_PREFIX) or label.strip().lower() == "true":
            names.add(name)
    return names


def _docker_managed_names(*, include_stopped: bool) -> set[str] | None:
    args = ["docker", "ps"]
    if include_stopped:
        args.append("-a")
    args.extend(["--format", '{{.Names}}\t{{.Label "solo2611"}}'])
    try:
        result = run(args, check=False, timeout=30)
    except Exception:  # noqa: BLE001 - 超时或 docker 不存在都按“无法确认”处理
        return None
    if result.returncode != 0:
        return None
    return _parse_managed_names(out(result))


def running_containers() -> set[str] | None:
    """Names of running managed containers; None when Docker gave no usable answer."""
    return _docker_managed_names(include_stopped=False)


def known_containers() -> set[str]:
    """Names of managed containers in any state; empty when Docker gave no answer."""
    return _docker_managed_names(include_stopped=True) or set()


def _load() -> dict[str, dict[str, Any]]:
    try:
        data = read_json(LEASE_FILE, {}) or {}
    except Exception:  # noqa: BLE001 - 文件损坏时当作没有租约，由 docker 实况兜底
        return {}
    leases = data.get("leases") if isinstance(data, dict) else None
    return {str(k): v for k, v in leases.items() if isinstance(v, dict)} if isinstance(leases, dict) else {}


def _save(leases: dict[str, dict[str, Any]]) -> None:
    write_json(LEASE_FILE, {"schema": "solo2611/container-leases/1", "leases": leases}, mode=0o600)


def _prune(leases: dict[str, dict[str, Any]], running: set[str], now: float) -> dict[str, dict[str, Any]]:
    kept: dict[str, dict[str, Any]] = {}
    for holder, lease in leases.items():
        container = str(lease.get("container") or "")
        if container and container in running:
            continue  # 运行中的由 docker 计数；已退出的不再占名额。容器名每次启动唯一。
        try:
            age = now - float(lease.get("refreshedEpoch") or 0)
        except (TypeError, ValueError):
            continue
        if 0 <= age < LEASE_TTL_SECONDS:
            kept[holder] = lease
    return kept


def acquire(
    holder: str,
    *,
    limit: int,
    container: str = "",
    task_root: Path | None = None,
    wait_seconds: float = DEFAULT_WAIT_SECONDS,
) -> dict[str, Any]:
    """Take one container slot for ``holder``, waiting until one is free.

    Calling it again for a holder that already has a live lease only refreshes
    the lease (and records the container name), so "reserve early, launch
    later" costs one slot, not two.
    """
    limit = max(1, int(limit))
    deadline = time.monotonic() + max(0.0, wait_seconds)
    announced = False
    while True:
        reason = ""
        with file_lock(CONTAINER_LOCK):
            running = running_containers()
            now = time.time()
            if running is None:
                reason = "无法从 Docker 确认当前容器数"
            else:
                leases = _prune(_load(), running | known_containers(), now)
                mine = leases.get(holder)
                used = len(running) + len(leases)
                if mine is not None or used < limit:
                    leases[holder] = {
                        "container": container or str((mine or {}).get("container") or ""),
                        "taskRoot": str(task_root or (mine or {}).get("taskRoot") or ""),
                        "createdAt": (mine or {}).get("createdAt") or utc_now(),
                        "refreshedEpoch": now,
                    }
                    _save(leases)
                    return {
                        "holder": holder,
                        "limit": limit,
                        "running": len(running),
                        "pending": len(leases),
                        "renewed": mine is not None,
                    }
                _save(leases)
                reason = f"容器已达上限 {limit}（运行 {len(running)}，启动中 {len(leases)}）"
        if time.monotonic() >= deadline:
            raise Solo2611Error(f"{reason}，等待 {int(wait_seconds)} 秒仍无名额；稍后重试，不消耗会话机会")
        if not announced:
            print(f"{reason}，等待名额…", file=sys.stderr)
            announced = True
        time.sleep(POLL_SECONDS)


def release(holder: str) -> bool:
    """Give the slot back only after confirming no container is running for it.

    A launcher can throw after ``docker run`` has created the container but
    before state was saved. Releasing that lease blindly would let another
    launcher claim the same slot and exceed the hard limit.
    """
    try:
        with file_lock(CONTAINER_LOCK):
            leases = _load()
            lease = leases.get(holder)
            if lease is None:
                return False
            container = str(lease.get("container") or "")
            running = running_containers()
            if running is None:
                return False
            if container and container in running:
                return False
            leases.pop(holder, None)
            _save(leases)
            return True
    except Exception:  # noqa: BLE001
        return False


def release_container(container: str) -> None:
    """Drop the lease of a container that was just removed; never raises."""
    if not container:
        return
    try:
        with file_lock(CONTAINER_LOCK):
            leases = _load()
            kept = {k: v for k, v in leases.items() if str(v.get("container") or "") != container}
            if len(kept) != len(leases):
                _save(kept)
    except Exception:  # noqa: BLE001
        pass


def settle() -> None:
    """Drop leases whose container Docker already knows; called once a launch is over."""
    try:
        with file_lock(CONTAINER_LOCK):
            running = running_containers()
            if running is None:
                return
            before = _load()
            after = _prune(before, running | known_containers(), time.time())
            if after != before:
                _save(after)
    except Exception:  # noqa: BLE001
        pass


def snapshot(limit: int) -> dict[str, Any]:
    with file_lock(CONTAINER_LOCK):
        running = running_containers()
        before = _load()
        leases = _prune(before, (running or set()) | known_containers(), time.time())
        if running is not None and leases != before:
            _save(leases)
    used = (len(running) if running is not None else 0) + len(leases)
    return {
        "limit": int(limit),
        "dockerAvailable": running is not None,
        "running": sorted(running or []),
        "physicalRunning": len(running or []),
        "pending": [
            {"holder": holder, "container": lease.get("container"), "taskRoot": lease.get("taskRoot")}
            for holder, lease in leases.items()
        ],
        "used": used,
        "free": max(0, int(limit) - used) if running is not None else 0,
        "overLimit": running is not None and used > int(limit),
        "leaseTtlSeconds": int(LEASE_TTL_SECONDS),
        "managedPrefix": MANAGED_PREFIX,
    }

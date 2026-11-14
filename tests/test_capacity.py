"""Container capacity gate: never admits more than the limit, even under races."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import capacity  # noqa: E402
from common import Solo2611Error  # noqa: E402


class Docker(set):
    """Running container names; ``exited`` holds containers that still exist but stopped."""

    def __init__(self) -> None:
        super().__init__()
        self.exited: set[str] = set()


@pytest.fixture
def gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Docker:
    running = Docker()
    monkeypatch.setattr(capacity, "known_containers", lambda: set(running) | running.exited)
    monkeypatch.setattr(capacity, "CONTAINER_LOCK", tmp_path / "containers.lock")
    monkeypatch.setattr(capacity, "LEASE_FILE", tmp_path / "container-leases.json")
    monkeypatch.setattr(capacity, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(capacity, "running_containers", lambda: set(running))
    return running


def test_pending_launches_count_before_docker_sees_them(gate: Docker) -> None:
    capacity.acquire("task:a", limit=2, container="c-a")
    capacity.acquire("task:b", limit=2, container="c-b")
    # 两个容器都还没出现在 docker ps 里，第三个也不能进。
    with pytest.raises(Solo2611Error, match="容器已达上限 2"):
        capacity.acquire("task:c", limit=2, wait_seconds=0)
    assert capacity.snapshot(2)["used"] == 2


def test_reserving_early_then_launching_uses_one_slot(gate: Docker) -> None:
    first = capacity.acquire("task:a", limit=1)
    again = capacity.acquire("task:a", limit=1, container="c-a")
    assert first["renewed"] is False and again["renewed"] is True
    assert capacity.snapshot(1)["pending"][0]["container"] == "c-a"
    # 容器跑起来以后租约让位给 docker 实况，仍然只占一个名额。
    gate.add("c-a")
    view = capacity.snapshot(1)
    assert view["used"] == 1 and view["pending"] == [] and view["free"] == 0
    with pytest.raises(Solo2611Error):
        capacity.acquire("task:b", limit=1, wait_seconds=0)
    gate.clear()
    assert capacity.acquire("task:b", limit=1, wait_seconds=0)["running"] == 0


def test_stale_lease_and_release_free_the_slot(gate: Docker, monkeypatch: pytest.MonkeyPatch) -> None:
    capacity.acquire("task:a", limit=1, container="c-a")
    capacity.release("task:a")
    capacity.acquire("task:b", limit=1, container="c-b", wait_seconds=0)
    # 启动方崩溃、容器始终没起来：租约过期后名额自动归还。
    monkeypatch.setattr(capacity, "LEASE_TTL_SECONDS", 0.0)
    assert capacity.acquire("task:c", limit=1, wait_seconds=0)["pending"] == 1


def test_container_that_died_early_gives_the_slot_back(gate: Docker) -> None:
    capacity.acquire("task:a", limit=1, container="c-a")
    # 容器启动后几秒就退出，期间没有人查询过名额：租约不能再占着位置。
    gate.exited.add("c-a")
    assert capacity.snapshot(1)["free"] == 1
    assert capacity.acquire("task:b", limit=1, container="c-b", wait_seconds=0)["pending"] == 1


def test_removed_container_releases_its_lease(gate: Docker) -> None:
    capacity.acquire("task:a", limit=2, container="c-a")
    capacity.acquire("task:b", limit=2, container="c-b")
    capacity.release_container("c-a")
    capacity.release_container("")
    view = capacity.snapshot(2)
    assert [item["container"] for item in view["pending"]] == ["c-b"] and view["free"] == 1


def test_release_keeps_the_lease_when_the_launcher_is_already_running(
    gate: Docker,
) -> None:
    capacity.acquire("task:a", limit=1, container="c-a")
    gate.add("c-a")
    assert capacity.release("task:a") is False
    assert capacity.snapshot(1)["used"] == 1


def test_legacy_managed_prefix_is_counted_without_a_label() -> None:
    names = capacity._parse_managed_names(
        "solo2611-legacy-01-abcd\t\n"
        "custom-labelled\ttrue\n"
        "project-hub-api-1\t\n"
        "custom-app\tfalse\n"
    )
    assert names == {"solo2611-legacy-01-abcd", "custom-labelled"}


def test_settle_drops_leases_docker_already_counts(gate: Docker) -> None:
    capacity.acquire("task:a", limit=2, container="c-a")
    gate.add("c-a")
    capacity.settle()
    gate.clear()  # 容器随后被外部删除
    assert capacity.snapshot(2)["used"] == 0


def test_docker_unavailable_admits_nothing(gate: Docker, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(capacity, "running_containers", lambda: None)
    with pytest.raises(Solo2611Error, match="无法从 Docker 确认"):
        capacity.acquire("task:a", limit=3, wait_seconds=0)
    assert capacity.snapshot(3)["free"] == 0


def test_waiter_gets_the_slot_when_a_container_exits(gate: Docker) -> None:
    gate.add("c-a")
    threading.Timer(0.1, gate.clear).start()
    started = time.monotonic()
    assert capacity.acquire("task:b", limit=1, container="c-b", wait_seconds=5)["running"] == 0
    assert time.monotonic() - started < 3


def test_concurrent_launchers_never_exceed_the_limit(gate: Docker) -> None:
    limit, admitted, errors = 3, [], []
    lock = threading.Lock()
    barrier = threading.Barrier(12)

    def launcher(number: int) -> None:
        barrier.wait()
        try:
            capacity.acquire(f"task:{number}", limit=limit, container=f"c-{number}", wait_seconds=0)
        except Solo2611Error:
            return
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
            return
        with lock:
            admitted.append(number)

    threads = [threading.Thread(target=launcher, args=(n,)) for n in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(admitted) == limit
    assert capacity.snapshot(limit)["overLimit"] is False

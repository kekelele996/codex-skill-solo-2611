"""v4 managed-session state machine and trace rules (no Docker required)."""

from __future__ import annotations

import json
import os
import re
import fcntl
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

TMP = Path(tempfile.mkdtemp(prefix="solo2611-session-"))
os.environ.setdefault("CODEX_HOME", str(TMP / "codex"))
os.environ.setdefault("SOLO2611_CONFIG", str(TMP / "codex" / "solo-2611" / "config.json"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import doctor  # noqa: E402
import feedback as fb  # noqa: E402
import resume as print_resume  # noqa: E402
import session  # noqa: E402
import trace as tr  # noqa: E402
from common import Solo2611Error, load_state, save_state  # noqa: E402


def _event(kind: str, text: str = "", *, prompt_id: str = "", stop: str | None = None) -> dict:
    item: dict = {
        "type": kind,
        "sessionId": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "timestamp": "2026-11-13T09:00:00Z",
    }
    if kind == "user":
        item["promptId"] = prompt_id
        item["message"] = {"role": "user", "content": text}
    else:
        item["message"] = {
            "model": "auto_model/urm",
            "stop_reason": stop or "end_turn",
            "content": [{"type": "text", "text": text}],
        }
    return item


def _task_root(tmp_path: Path) -> Path:
    root = tmp_path / "task"
    root.mkdir()
    state = {
        "schema": "solo-2611/1",
        "taskName": "session-test",
        "workspace": str(root / "workspace"),
        "status": "running",
        "promptPath": str(root / "delivery" / "提示词.md"),
        "attempts": [
            {
                "index": 1,
                "container": "solo2611-test",
                "mode": "supervised",
                "startedAt": "2026-11-13T09:00:00Z",
                "status": "running",
                "dir": str(root / "monitor" / "runs" / "attempt-01"),
            }
        ],
        "sessionRun": {
            "schema": "solo2611/session-run/1",
            "mode": "supervised",
            "attemptIndex": 1,
            "container": "solo2611-test",
            "tmuxSession": "",
            "status": "running",
            "startedAt": "2026-11-13T09:00:00Z",
            "deadline": {
                "totalAt": "2026-11-13T12:00:00Z",
                "noProgressAt": "2026-11-13T09:20:00Z",
                "holdUntil": None,
            },
            "continues": {"used": 0, "max": 2, "history": []},
            "rounds": [],
            "interrupts": [],
            "blocked": None,
            "abort": None,
            "outcome": None,
        },
    }
    (root / "monitor" / "runs" / "attempt-01" / "live").mkdir(parents=True)
    (root / "delivery").mkdir()
    (root / "delivery" / "提示词.md").write_text("原始题面", encoding="utf-8")
    save_state(root, state)
    return root


@pytest.mark.parametrize(
    ("text", "is_error", "stop", "kind", "verdict"),
    [
        ("429 Tokens Per Minute", True, "stop_sequence", "rate_limited", "continue"),
        ("429 Too Many Requests", False, "stop_sequence", "rate_limited", "continue"),
        ("503 overloaded", True, "stop_sequence", "gateway", "continue"),
        ("HTTP 503 Service Unavailable", False, "stop_sequence", "gateway", "continue"),
        ("API Error: upstream failed", True, "stop_sequence", "api_error", "continue"),
        ("401 authentication", True, "stop_sequence", "auth", "hold"),
        ("quota exceeded", True, "stop_sequence", "quota", "hold"),
        ("normal answer", False, "max_tokens", "truncated", "terminate"),
        ("上下文已满", False, "end_turn", "truncated", "terminate"),
        ("批次状态为 interrupted，但字段、页面和验证均已完成。", False, "end_turn", "normal_end", "terminate"),
        ("Request interrupted by user", False, "end_turn", "request_interrupted", "continue"),
        ("streaming response error", False, "end_turn", "request_interrupted", "continue"),
        ("", False, "end_turn", "empty", "terminate"),
        ("需要我继续吗？", False, "end_turn", "incomplete", "terminate"),
        ("需要我提交、推送或开 PR 吗？", False, "end_turn", "incomplete", "terminate"),
        ("下一步可以补测试。", False, "end_turn", "normal_end", "terminate"),
        ("已完成", False, "end_turn", "normal_end", "terminate"),
        ("处理了500行数据，已完成", False, "end_turn", "normal_end", "terminate"),
    ],
)
def test_classify_round_table(
    text: str,
    is_error: bool,
    stop: str,
    kind: str,
    verdict: str,
) -> None:
    final = _event("assistant", text, stop=stop)
    final["isApiErrorMessage"] = is_error
    events = [_event("user", "原始题面", prompt_id="p1"), final]
    info = tr.analyze_from_events(events, tolerate_incomplete=True)
    result = session.classify_round(info, None, events)
    assert result["kind"] == kind
    assert result["verdict"] == verdict


def test_terminal_normal_end_is_not_recorded_as_model_interrupt() -> None:
    events = [_event("user", "原始题面", prompt_id="p1"), _event("assistant", "是否提交由你决定。")]
    state = {
        "sessionRun": {
            "status": "running",
            "lastRoundStartedAt": "2026-11-14T01:00:00Z",
            "rounds": [],
            "interrupts": [],
        }
    }
    item = session._record_round(
        state,
        events,
        {
            "kind": "incomplete",
            "verdict": "terminate",
            "detail": "是否提交由你决定。",
        },
    )
    assert item["verdict"] == "terminate"
    assert state["sessionRun"]["interrupts"] == []


def test_continuation_trigger_only_accepts_api_interface_failures() -> None:
    assert session.continuation_trigger({"pendingContinue": "rate_limited"}) == "rate_limited"
    assert session.continuation_trigger({"rounds": [{"kind": "gateway"}]}) == "gateway"
    assert session.continuation_trigger({"rounds": [{"kind": "normal_end"}]}) == ""
    assert session.continuation_trigger({"blocked": {"kind": "auth"}}) == ""


def test_human_continue_rejects_normal_end_without_api_failure(tmp_path: Path) -> None:
    root = _task_root(tmp_path)
    with pytest.raises(Solo2611Error, match="明确 API/接口故障"):
        session.human_continue(root)


def test_round_ended_requires_trace_and_screen_idle() -> None:
    events = [_event("user", "原始题面", prompt_id="p1"), _event("assistant", "已完成")]
    assert session.round_ended(
        events,
        file_size=10,
        previous_size=10,
        last_growth_at=80,
        screen="> ",
        now=100,
    )
    assert not session.round_ended(
        events,
        file_size=10,
        previous_size=10,
        last_growth_at=95,
        screen="> ",
        now=100,
    )
    assert not session.round_ended(
        events,
        file_size=10,
        previous_size=10,
        last_growth_at=80,
        screen="esc to interrupt",
        now=100,
    )
    assert session.round_ended(
        events,
        file_size=10,
        previous_size=10,
        last_growth_at=80,
        screen="nothing to commit, working tree clean",
        now=100,
    )


def test_trace_allows_two_continues_and_rejects_three(tmp_path: Path) -> None:
    events = [
        _event("user", "原始题面", prompt_id="p1"),
        _event("assistant", "先做一半"),
        _event("user", "继续", prompt_id="p2"),
        _event("assistant", "继续做"),
        _event("user", "继续", prompt_id="p3"),
        _event("assistant", "已完成"),
    ]
    path = tmp_path / "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa.jsonl"
    path.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in events), encoding="utf-8")
    info = tr.analyze(path, "原始题面")
    assert info["ok"]
    assert info["continues"] == 2
    events.append(_event("user", "继续", prompt_id="p4"))
    events.append(_event("assistant", "已完成"))
    path.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in events), encoding="utf-8")
    assert not tr.analyze(path, "原始题面")["ok"]


def test_complete_round_prefix_keeps_only_clean_first_round() -> None:
    events = [
        _event("user", "原始题面", prompt_id="p1"),
        _event("assistant", "实例状态为 interrupted，但全部字段、页面和验证已完成。"),
        _event("user", "继续", prompt_id="p2"),
        _event("assistant", "误追加内容"),
    ]
    prefix, cut_at, discarded = session.complete_round_prefix(events, 1)
    assert len(prefix) == 2
    assert cut_at == 2
    assert discarded == ["p2"]
    assert tr.analyze_from_events(prefix, "原始题面")["ok"]


def test_trace_rejects_human_intervention(tmp_path: Path) -> None:
    events = [
        _event("user", "原始题面", prompt_id="p1"),
        _event("assistant", "先做一半"),
        _event("user", "帮我再改改", prompt_id="p2"),
        _event("assistant", "已完成"),
    ]
    path = tmp_path / "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa.jsonl"
    path.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in events), encoding="utf-8")
    info = tr.analyze(path, "原始题面")
    assert not info["ok"]
    assert any("非“继续”" in item for item in info["errors"])


def test_trace_ignores_background_task_notifications(tmp_path: Path) -> None:
    notification = _event(
        "user",
        "<task-notification>\n<task-id>bg-1</task-id>\n<status>completed</status>\n</task-notification>",
        prompt_id="system-p2",
    )
    notification["promptSource"] = "system"
    notification["origin"] = {"kind": "task-notification", "producer": "session-task"}
    events = [
        _event("user", "原始题面", prompt_id="p1"),
        notification,
        _event("assistant", "已完成"),
    ]
    path = tmp_path / "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa.jsonl"
    path.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in events), encoding="utf-8")
    info = tr.analyze(path, "原始题面")
    assert info["ok"]
    assert info["prompts"] == 1


def test_trace_ignores_synthetic_interruption_message(tmp_path: Path) -> None:
    interrupted = _event("user", "[Request interrupted by user]")
    interrupted.pop("promptId", None)
    events = [
        _event("user", "原始题面", prompt_id="p1"),
        interrupted,
        _event("assistant", "已完成"),
    ]
    path = tmp_path / "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa.jsonl"
    path.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in events), encoding="utf-8")
    info = tr.analyze(path, "原始题面")
    assert info["ok"]
    assert info["prompts"] == 1


def test_incremental_trace_reads_only_complete_new_lines(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    path.write_text('{"type":"user","n":1}\n{"type":"assistant"', encoding="utf-8")
    reader = session.IncrementalTrace(path)
    assert [item.get("n") for item in reader.read()] == [1]
    with path.open("a", encoding="utf-8") as handle:
        handle.write(',"n":2}\n')
    assert [item.get("n") for item in reader.read()] == [2]
    assert len(reader.events) == 2
    assert reader.read() == []


def test_continue_hard_limit_and_remark() -> None:
    assert session.CONTINUE_HARD_LIMIT == 2
    assert session.effective_continue_max({"claude": {"continueMax": 0}}) == 0
    assert session.effective_continue_max({"claude": {"continueMax": 9}}) == 2
    remark = tr.submission_remark(
        {"sessionRun": {"continues": {"used": 2}}},
        api_key_remark=tr.PLAINTEXT_API_KEY_REMARK,
    )
    assert remark == (
        '模型运行中途中断，人工输入"继续"2次后完成；'
        "轨迹中有存在明文apikey暴露风险"
    )


def test_managed_names_are_docker_and_tmux_safe() -> None:
    name = session._container_name("特征平台点时间正确性", 1)
    tmux_name = session._tmux_name("特征平台点时间正确性", 1)
    assert re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", name)
    assert name.startswith("solo2611-task-")
    assert re.fullmatch(r"[a-z0-9][a-z0-9_-]*", tmux_name)


def test_continue_marks_only_latest_round() -> None:
    session_state = {
        "rounds": [
            {"no": 1, "continued": False},
            {"no": 2, "continued": False},
        ]
    }
    session._mark_latest_round_continued(session_state)
    assert [item["continued"] for item in session_state["rounds"]] == [False, True]


def test_interactive_prerequisites_accept_current_tmux() -> None:
    session.assert_interactive_prereqs()


def test_bypass_settings_are_preaccepted_for_fresh_home(tmp_path: Path) -> None:
    settings_dir = session._write_bypass_settings(tmp_path)
    path = settings_dir / "managed-settings.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "skipDangerousModePermissionPrompt": True
    }
    assert path.stat().st_mode & 0o777 == 0o444


def test_attempt_runtime_cleanup_removes_secret_launch_files(tmp_path: Path) -> None:
    (tmp_path / "env.sh").write_text("export apikey=secret\n", encoding="utf-8")
    (tmp_path / "launch.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    settings = tmp_path / "claude-managed-settings"
    settings.mkdir()
    (settings / "managed-settings.json").write_text("{}\n", encoding="utf-8")

    session._cleanup_attempt_runtime(tmp_path)

    assert not (tmp_path / "env.sh").exists()
    assert not (tmp_path / "launch.sh").exists()
    assert not settings.exists()


def test_prompt_delivery_recovery_plan_is_bounded() -> None:
    assert session._prompt_recovery_due(7.9, 0) is None
    assert session._prompt_recovery_due(8, 0) == ("submit", 8.0)
    assert session._prompt_recovery_due(20, 1) == ("rewrite", 20.0)
    assert session._prompt_recovery_due(36, 2) == ("submit", 36.0)
    assert session._prompt_recovery_due(50, 3) == ("rewrite", 50.0)
    assert session._prompt_recovery_due(90, 4) is None


def test_prompt_delivery_only_confirms_the_exact_trace_prompt(tmp_path: Path) -> None:
    root = _task_root(tmp_path)
    state = load_state(root)
    session_state = state["sessionRun"]
    live = root / "monitor" / "runs" / "attempt-01" / "live" / "workspace"
    live.mkdir()
    trace = live / "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa.jsonl"
    event = _event("user", "原始题面", prompt_id="p1")
    trace.write_text(json.dumps(event, ensure_ascii=False) + "\n", encoding="utf-8")

    trace_path, prompt_event = session._matching_prompt_event(
        root / "monitor" / "runs" / "attempt-01" / "live",
        "原始题面",
    )
    assert trace_path == trace and prompt_event is not None

    session._confirm_prompt_delivery(
        root,
        state,
        1,
        session_state,
        "原始题面",
        trace,
        prompt_event,
    )
    saved = load_state(root)["sessionRun"]
    assert saved["promptSent"] is True
    assert saved["status"] == "prompt_sent"
    assert saved["promptDelivery"]["status"] == "confirmed"
    assert saved["promptDelivery"]["promptId"] == "p1"
    assert saved["promptDelivery"]["trace"] == str(trace)


def test_prompt_delivery_nudges_then_waits_for_trace_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _task_root(tmp_path)
    state = load_state(root)
    current = state["sessionRun"]
    current["tmuxSession"] = "solo2611-test"
    current["promptDelivery"] = {
        "status": "waiting_trace",
        "startedAt": "2026-11-13T08:59:50Z",
        "literalAttempts": 1,
        "submitAttempts": 0,
        "history": [],
    }
    live = root / "monitor" / "runs" / "attempt-01" / "live"
    trace = live / "workspace" / "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa.jsonl"
    trace.parent.mkdir(parents=True)
    trace.write_text("", encoding="utf-8")
    prompt_event = _event("user", "原始题面", prompt_id="p1")
    matches = iter([(None, None), (trace, prompt_event)])
    submitted: list[str] = []

    monkeypatch.setattr(session, "_matching_prompt_event", lambda *_: next(matches))
    monkeypatch.setattr(session, "tmux_alive", lambda _: True)
    monkeypatch.setattr(session, "capture_pane", lambda *_, **__: "> ")
    monkeypatch.setattr(session, "submit_literal_input", submitted.append)
    monkeypatch.setattr(session.time, "sleep", lambda _: None)

    result = session._wait_for_initial_prompt(
        root,
        state,
        current,
        1,
        live,
        "原始题面",
        timeout=5,
    )

    assert result["ok"] is True
    assert submitted == ["solo2611-test"]
    saved = load_state(root)["sessionRun"]
    assert saved["promptSent"] is True
    assert saved["promptDelivery"]["history"][0]["action"] == "submit"


def test_docker_command_mounts_bypass_settings(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    live = tmp_path / "live"
    settings_dir = session._write_bypass_settings(tmp_path)
    workspace.mkdir()
    live.mkdir()
    command = session._docker_command(
        name="solo2611-test-01-abcd",
        task_root=tmp_path / "task",
        workspace=workspace,
        live=live,
        image="image",
        settings_dir=settings_dir,
    )
    mount = next(
        command[index + 1]
        for index, value in enumerate(command)
        if value == "--mount"
        and command[index + 1].endswith("dst=/etc/claude-code,readonly")
    )
    assert f"src={settings_dir}" in mount


def _fake_tmux(monkeypatch: pytest.MonkeyPatch, screens: list[str]) -> list[str]:
    """Feed screens to wait_for_repl_state and record the keys it sends."""
    feed = iter(screens)
    last = [screens[-1]]
    keys: list[str] = []

    def capture(*_args: object, **_kwargs: object) -> str:
        last[0] = next(feed, last[0])
        return last[0]

    def fake_run(command: list[str], **_: object) -> SimpleNamespace:
        if command[:2] == ["tmux", "send-keys"]:
            keys.append(command[-1])
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(session, "capture_pane", capture)
    monkeypatch.setattr(session.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(session, "run", fake_run)
    return keys


def test_bypass_prompt_verifies_selection_before_enter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys = _fake_tmux(
        monkeypatch,
        [
            "Bypass Permissions mode\n❯ No, exit\n  Yes, I accept",
            "Bypass Permissions mode\n  No, exit\n› Yes, I accept",
            "❯ ",
        ],
    )
    result = session.wait_for_repl_state("tmux-test", timeout=5)

    assert result["ok"] is True
    assert result["confirmationAttempts"] == 1
    assert result["strategies"] == ["selected-no-then-down-enter"]
    assert result["prompts"] == ["bypass"]
    assert keys == ["Down", "C-m"]


def test_bypass_prompt_never_confirms_while_exit_is_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stuck = "Bypass Permissions mode\n❯ No, exit\n  Yes, I accept"
    keys = _fake_tmux(monkeypatch, [stuck] * 12 + ["Bypass Permissions mode\n  No, exit\n❯ Yes, I accept", "❯ "])
    result = session.wait_for_repl_state("tmux-test", timeout=5)

    assert result["ok"] is True
    assert result["strategies"][0] == "moved-unverified"
    assert keys[-1] in {"C-m", "Enter"}
    assert set(keys[:-1]) == {"Down"}


def test_startup_prompt_handles_numbered_trust_dialog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trust = (
        "Do you trust the files in this folder?\n"
        "│ ❯ 1. Yes, proceed            │\n"
        "│   2. No, exit                │\n"
        "Enter to confirm · Esc to cancel"
    )
    keys = _fake_tmux(monkeypatch, [trust, "> "])
    result = session.wait_for_repl_state("tmux-test", timeout=5)

    assert result["ok"] is True
    assert result["prompts"] == ["confirm"]
    assert result["strategies"] == ["selected-yes-enter"]
    assert keys == ["C-m"]


def test_unmarked_yes_first_never_presses_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trust = (
        "Do you trust the files in this folder?\n"
        "Yes, proceed\n"
        "No, exit\n"
        "Enter to confirm · Esc to cancel"
    )
    keys = _fake_tmux(monkeypatch, [trust, trust, "> "])
    result = session.wait_for_repl_state("tmux-test", timeout=5)

    assert result["ok"] is True
    assert "Down" not in keys
    assert keys == ["Up", "Enter"]


def test_unmarked_yes_second_moves_down_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trust = (
        "Do you trust the files in this folder?\n"
        "No, exit\n"
        "Yes, proceed\n"
        "Enter to confirm · Esc to cancel"
    )
    keys = _fake_tmux(monkeypatch, [trust, trust, "> "])
    result = session.wait_for_repl_state("tmux-test", timeout=5)

    assert result["ok"] is True
    assert keys == ["Down", "Enter"]


def test_startup_prompt_handles_chinese_trust_dialog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trust = (
        "是否信任此文件夹中的文件？\n"
        "❯ 1. 是，继续\n"
        "  2. 否，退出\n"
        "按 Enter 确认 · Esc 取消"
    )
    keys = _fake_tmux(monkeypatch, [trust, "> "])
    result = session.wait_for_repl_state("tmux-test", timeout=5)

    assert result["ok"] is True
    assert result["prompts"] == ["confirm"]
    assert keys == ["C-m"]


def test_bypass_prompt_times_out_without_hanging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    monkeypatch.setattr(
        session,
        "capture_pane",
        lambda *_args, **_kwargs: "Bypass Permissions mode\nNo, exit\nYes, I accept",
    )
    monkeypatch.setattr(session.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        session.time,
        "sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + float(seconds)),
    )
    monkeypatch.setattr(
        session,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
    )

    result = session.wait_for_repl_state(
        "tmux-test",
        timeout=8,
        confirmation_timeout=3,
    )

    assert result["ok"] is False
    assert result["reason"] == "confirmation_timeout"
    # 在整个确认时限内持续尝试，而不是按固定 3 次后干等。
    assert result["confirmationAttempts"] >= 3


def test_wait_for_repl_returns_at_once_when_session_exited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    monkeypatch.setattr(session, "capture_pane", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(session, "tmux_alive", lambda _name: False)
    monkeypatch.setattr(session.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        session.time,
        "sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + float(seconds)),
    )
    result = session.wait_for_repl_state("tmux-test", timeout=90)

    assert result["ok"] is False
    assert result["reason"] == "session_exited"
    assert clock[0] < 5


def test_launch_retries_tmux_new_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _task_root(tmp_path)
    state = load_state(root)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state["workspace"] = str(workspace)
    state.setdefault("taskName", "demo-task")
    outcomes = iter([1, 0])
    created: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> SimpleNamespace:
        if command[:2] == ["tmux", "new-session"]:
            created.append(command)
            return SimpleNamespace(returncode=next(outcomes), stdout=b"", stderr=b"server exited")
        return SimpleNamespace(returncode=1, stdout=b"", stderr=b"")

    monkeypatch.setattr(session, "run", fake_run)
    monkeypatch.setattr(session, "container_exists", lambda _name: False)
    monkeypatch.setattr(session, "close_session_terminal", lambda *_a, **_k: {"closed": 0})
    monkeypatch.setattr(session.time, "sleep", lambda _seconds: None)
    slots: list[str] = []
    monkeypatch.setattr(
        session.capacity, "acquire", lambda holder, **kwargs: slots.append(kwargs["container"]) or {}
    )
    launched = session.launch_tmux_container(
        task_root=root, state=state, attempt=1, image="image", key="k", base_url="http://x"
    )
    assert len(created) == 2
    assert launched["tmux"].startswith("solo2611-")
    # 每次受管启动都先过容量闸门，并登记将要启动的容器名。
    assert slots == [launched["container"]]


def test_open_session_terminal_uses_terminal_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripts: list[str] = []
    monkeypatch.setattr(session.sys, "platform", "darwin")
    monkeypatch.setattr(session.terminal_view.sys, "platform", "darwin")
    monkeypatch.setattr(session, "tmux_alive", lambda _: True)
    monkeypatch.setattr(session, "load_config", lambda: {"claude": {}})

    def fake_osascript(script: str, **_: object) -> SimpleNamespace:
        scripts.append(script)
        return SimpleNamespace(returncode=0, stdout="42\x1f/dev/ttys009\n", stderr="")

    monkeypatch.setattr(session.terminal_view, "_osascript", fake_osascript)
    result = session.open_session_terminal("solo2611-safe-name")
    assert result["opened"] is True
    assert result["windowId"] == 42
    assert result["readOnly"] is True
    script = scripts[0]
    assert 'tell application "Terminal"' in script
    assert "terminal_view.py attach-session --tmux solo2611-safe-name --read-only" in script
    # 不能抢键盘焦点，否则用户正在别处敲的键会落进受管会话。
    assert "activate" not in script


def test_open_session_terminal_is_optional(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _task_root(tmp_path)
    monkeypatch.setattr(
        session,
        "load_config",
        lambda: {"claude": {"openTerminalOnRun": False}},
    )
    called = False

    def unexpected(_: str) -> dict:
        nonlocal called
        called = True
        return {"opened": True}

    monkeypatch.setattr(session, "open_session_terminal", unexpected)
    result = session.maybe_open_session_terminal(root, 1, "solo2611-safe-name")
    assert result == {"requested": False, "opened": False}
    assert called is False


def test_open_session_terminal_timeout_does_not_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session.sys, "platform", "darwin")
    monkeypatch.setattr(session.terminal_view.sys, "platform", "darwin")
    monkeypatch.setattr(session, "tmux_alive", lambda _: True)
    monkeypatch.setattr(session, "load_config", lambda: {"claude": {}})
    monkeypatch.setattr(session.terminal_view.time, "sleep", lambda _seconds: None)

    def timeout(script: str, **_: object) -> SimpleNamespace:
        raise session.subprocess.TimeoutExpired(["osascript"], 15)

    monkeypatch.setattr(session.terminal_view, "_osascript", timeout)
    result = session.open_session_terminal("solo2611-safe-name")
    assert result["opened"] is False
    assert "timed out" in result["error"]


def _terminal_rows(monkeypatch: pytest.MonkeyPatch, rows: list[dict]) -> list[int]:
    view = session.terminal_view
    closed: list[int] = []
    monkeypatch.setattr(view.sys, "platform", "darwin")
    monkeypatch.setattr(view, "terminal_running", lambda: True)
    monkeypatch.setattr(view.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        view, "list_tabs", lambda: [row for row in rows if row["windowId"] not in closed]
    )

    def close(row: dict) -> bool:
        closed.append(row["windowId"])
        return True

    monkeypatch.setattr(view, "_close_row", close)
    return closed


def test_terminal_sweep_only_closes_views_of_dead_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {"windowId": 1, "tty": "/dev/ttys001", "busy": False, "tmux": "solo2611-dead"},
        {"windowId": 2, "tty": "/dev/ttys002", "busy": False, "tmux": "solo2611-alive"},
        {"windowId": 3, "tty": "/dev/ttys003", "busy": True, "tmux": "solo2611-dead"},
        {"windowId": 4, "tty": "/dev/ttys004", "busy": False, "tmux": ""},
    ]
    closed = _terminal_rows(monkeypatch, rows)
    result = session.terminal_view.sweep(lambda name: name == "solo2611-alive")
    assert closed == [1]
    assert result == {"closed": 1, "kept": 2, "sessions": ["solo2611-dead"]}


def test_terminal_sweep_closes_busy_owned_title_after_session_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {
            "windowId": 1,
            "tty": "/dev/ttys001",
            "busy": True,
            "tmux": "solo2611-dead",
            "matchedBy": "title",
        },
        {
            "windowId": 2,
            "tty": "/dev/ttys002",
            "busy": True,
            "tmux": "solo2611-dead",
            "matchedBy": "history",
        },
    ]
    closed = _terminal_rows(monkeypatch, rows)
    result = session.terminal_view.sweep(lambda _name: False)
    assert closed == [1]
    assert result["closed"] == 1 and result["kept"] == 1


def test_terminal_close_skips_live_session(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [{"windowId": 1, "tty": "/dev/ttys001", "busy": False, "tmux": "solo2611-live"}]
    closed = _terminal_rows(monkeypatch, rows)
    view = session.terminal_view
    result = view.close_window("solo2611-live", alive=lambda _name: True, wait_seconds=0)
    assert closed == []
    assert result["skipped"] == 1 and result["reason"] == "session_alive"
    result = view.close_window("solo2611-live", alive=lambda _name: False, wait_seconds=0)
    assert closed == [1]
    assert result["closed"] == 1


def test_terminal_tabs_are_claimed_by_title_or_command(monkeypatch: pytest.MonkeyPatch) -> None:
    view = session.terminal_view
    f, r = view.FIELD, view.RECORD
    listing = (
        f"1{f}/dev/ttys001{f}solo2611-view:solo2611-a-01-abcd{f}false{f}{r}"
        f"2{f}/dev/ttys002{f}exec tmux attach-session -r -t solo2611-b-01-abcd{f}false{f}{r}"
        f"3{f}/dev/ttys003{f}zsh{f}false{f}$ tmux attach-session -t solo2611-c-01-abcd\n{r}"
        f"4{f}/dev/ttys004{f}zsh{f}true{f}$ ls\n{r}"
    )
    monkeypatch.setattr(view.sys, "platform", "darwin")
    monkeypatch.setattr(view, "terminal_running", lambda: True)
    monkeypatch.setattr(
        view, "_osascript", lambda *_a, **_k: SimpleNamespace(returncode=0, stdout=listing, stderr="")
    )
    rows = view.list_tabs()
    assert [(row["tmux"], row["matchedBy"]) for row in rows] == [
        ("solo2611-a-01-abcd", "title"),
        ("solo2611-b-01-abcd", "title-command"),
        ("solo2611-c-01-abcd", "history"),
        ("", ""),
    ]


def test_abort_is_terminal_and_writes_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _task_root(tmp_path)
    monkeypatch.setattr(session, "container_exists", lambda _: False)
    monkeypatch.setattr(session, "tmux_alive", lambda _: False)
    monkeypatch.setattr(session, "capture_pane", lambda *_, **__: "")
    result = session.abort(root, "人工放弃", by="human")
    assert result["ok"]
    state = load_state(root)
    assert state["status"] == "session_aborted"
    assert state["sessionRun"]["status"] == "aborted"
    assert state["sessionRun"]["abort"]["reason"] == "人工放弃"
    assert (root / "delivery" / "终止说明.md").is_file()
    assert not session.abort(root, "再次放弃")["ok"]


def test_legacy_state_without_session_run_routes_to_print_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _task_root(tmp_path)
    state = load_state(root)
    state.pop("sessionRun", None)
    save_state(root, state)

    def fake_resume(_: Path) -> dict[str, object]:
        return {"ok": True, "status": "legacy", "source": "print"}

    monkeypatch.setattr(print_resume, "resume_print", fake_resume)
    assert session.resume_session(root)["status"] == "legacy"


def test_print_resume_singleton_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _task_root(tmp_path)
    lock_path = root / "monitor" / ".resume.lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        monkeypatch.setattr(
            print_resume,
            "_resume_print_locked",
            lambda _: {"ok": True, "status": "unexpected"},
        )
        result = print_resume.resume_print(root)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    assert result["status"] == "running"


def test_feedback_caps_model_caused_continue(tmp_path: Path) -> None:
    root = _task_root(tmp_path)
    state = load_state(root)
    state["sessionRun"]["continues"]["used"] = 1
    state["sessionRun"]["rounds"] = [
        {"no": 1, "kind": "truncated", "verdict": "continue", "continued": True}
    ]
    path = root / "delivery" / "feedback.md"
    path.write_text(
        "## 交付完整性\n\n5\n\n## Feedback\n\n过程问题：运行了实现并逐项核对。\n\n产物问题：接口回读正常。\n",
        encoding="utf-8",
    )
    checked = fb.check(path, task_root=root, state=state)
    assert any("模型自身中断过的题不得评 5 分" in item for item in checked["errors"])


def test_doctor_baseline_is_read_only(tmp_path: Path) -> None:
    root = _task_root(tmp_path)
    before = (root / "monitor" / "state.json").read_bytes()
    result = doctor.baseline(tmp_path)
    assert result["attempts"] == 1
    assert result["attemptStatus"]["running"] == 1
    assert (root / "monitor" / "state.json").read_bytes() == before


def test_repl_ready_waits_for_a_stable_screen_before_sending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 输入框一闪而过，随后才弹出确认页：不能在第一眼看到输入框时就判定就绪。
    keys = _fake_tmux(
        monkeypatch,
        ["❯ ", "Bypass Permissions mode\n  No, exit\n❯ Yes, I accept", "❯ ", "❯ "],
    )
    result = session.wait_for_repl_state("tmux-test", timeout=5)

    assert result["ok"] is True
    assert result["prompts"] == ["bypass"]
    assert keys == ["C-m"]


def _delivery_root(tmp_path: Path) -> tuple[Path, dict, dict, Path]:
    root = _task_root(tmp_path)
    state = load_state(root)
    current = state["sessionRun"]
    current["tmuxSession"] = "solo2611-test"
    current["promptDelivery"] = {
        "status": "waiting_trace",
        "startedAt": session.utc_now(),
        "literalAttempts": 1,
        "submitAttempts": 0,
        "history": [],
    }
    return root, state, current, root / "monitor" / "runs" / "attempt-01" / "live"


def test_prompt_delivery_answers_startup_page_then_resends_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, state, current, live = _delivery_root(tmp_path)
    trace = live / "workspace" / "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa.jsonl"
    trace.parent.mkdir(parents=True)
    trace.write_text("", encoding="utf-8")
    resent: list[str] = []
    screens = iter(["Bypass Permissions mode\n  No, exit\n❯ Yes, I accept"])
    keys: list[str] = []

    def matching(*_: object) -> tuple:
        return (trace, _event("user", "原始题面", prompt_id="p1")) if resent else (None, None)

    def fake_run(command: list[str], **_: object) -> SimpleNamespace:
        keys.append(command[-1])
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(session, "_matching_prompt_event", matching)
    monkeypatch.setattr(session, "tmux_alive", lambda _: True)
    monkeypatch.setattr(session, "capture_pane", lambda *_, **__: next(screens, "❯ "))
    monkeypatch.setattr(session, "rewrite_literal_input", lambda name, text: resent.append(text))
    monkeypatch.setattr(session, "run", fake_run)
    monkeypatch.setattr(session.time, "sleep", lambda _: None)

    result = session._wait_for_initial_prompt(root, state, current, 1, live, "原始题面", timeout=5)

    assert result["ok"] is True
    # 确认页只收到确认键，题面在输入框就绪后整段重发一次。
    assert keys == ["C-m"]
    assert resent == ["原始题面"]
    saved = load_state(root)["sessionRun"]["promptDelivery"]
    assert saved["startupPageAttempts"] == 1
    assert saved["startupResends"] == 1
    assert saved["history"] == []
    events = session.supervisor_log(root, 1).read_text(encoding="utf-8")
    assert "prompt.startup_page" in events and "prompt.startup_resend" in events


def test_prompt_delivery_restarts_claude_once_in_a_fresh_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, state, current, live = _delivery_root(tmp_path)
    directory = live.parent
    (live / "stale.jsonl").write_text("{}\n", encoding="utf-8")
    removed: list[tuple[str, str]] = []
    sent: list[tuple[str, str]] = []

    def fake_launch(**_: object) -> dict[str, str]:
        (directory / "live").mkdir(exist_ok=True)
        return {"container": "solo2611-test-new", "tmux": "tmux-new", "env": "", "launch": ""}

    monkeypatch.setattr(session, "capture_pane", lambda *_, **__: "Bypass Permissions mode")
    monkeypatch.setattr(session, "_remove_tmux_and_container", lambda *args: removed.append(args))
    monkeypatch.setattr(session, "launch_tmux_container", fake_launch)
    monkeypatch.setattr(session, "maybe_open_session_terminal", lambda *_: {})
    monkeypatch.setattr(session, "wait_for_repl_state", lambda *_, **__: {"ok": True, "reason": "ready"})
    monkeypatch.setattr(session.capacity, "settle", lambda: None)
    monkeypatch.setattr(session, "send_literal", lambda name, text: sent.append((name, text)))
    monkeypatch.setattr(session, "load_config", lambda: {"claude": {"launchSpacingSeconds": 5}})
    monkeypatch.setattr(session.time, "sleep", lambda _: None)

    outcome = session._restart_for_prompt_delivery(
        root, state, current, 1, directory, "原始题面", "prompt_delivery_timeout"
    )

    assert outcome == "sent"
    assert removed == [("solo2611-test", "solo2611-test")]
    assert sent == [("tmux-new", "原始题面")]
    assert (directory / "live-prompt-restart-1" / "stale.jsonl").exists()
    assert not (directory / "live" / "stale.jsonl").exists()
    saved = load_state(root)["sessionRun"]
    assert saved["container"] == "solo2611-test-new"
    assert saved["status"] == "prompt_delivery"
    assert saved["promptRestarts"]["used"] == 1
    assert saved["promptDelivery"]["restartNo"] == 1
    assert saved["promptDelivery"]["history"] == []
    # 只换一次容器；再失败就按原来的启动失败收尾。
    again = session._restart_for_prompt_delivery(
        root, state, current, 1, directory, "原始题面", "prompt_delivery_timeout"
    )
    assert again == "exhausted"
    assert len(sent) == 1


def test_prompt_delivery_restart_stops_when_task_was_aborted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, state, current, live = _delivery_root(tmp_path)
    launched: list[object] = []

    def aborted_meanwhile(_seconds: float) -> None:
        latest = load_state(root)
        latest["sessionRun"]["status"] = "aborted"
        save_state(root, latest)

    monkeypatch.setattr(session, "capture_pane", lambda *_, **__: "")
    monkeypatch.setattr(session, "_remove_tmux_and_container", lambda *_: None)
    monkeypatch.setattr(session, "launch_tmux_container", lambda **kw: launched.append(kw))
    monkeypatch.setattr(session, "load_config", lambda: {"claude": {}})
    monkeypatch.setattr(session.time, "sleep", aborted_meanwhile)

    assert session._restart_for_prompt_delivery(root, state, current, 1, live.parent, "题面", "x") == "terminal"
    assert launched == []

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
from common import load_state, save_state  # noqa: E402


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
        ("503 overloaded", True, "stop_sequence", "gateway", "continue"),
        ("401 authentication", True, "stop_sequence", "auth", "hold"),
        ("quota exceeded", True, "stop_sequence", "quota", "hold"),
        ("normal answer", False, "max_tokens", "truncated", "continue"),
        ("上下文已满", False, "end_turn", "truncated", "continue"),
        ("", False, "end_turn", "empty", "continue"),
        ("需要我继续吗？", False, "end_turn", "incomplete", "continue"),
        ("已完成", False, "end_turn", "none", "complete"),
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

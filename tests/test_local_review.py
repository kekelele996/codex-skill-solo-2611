"""返修前的两道闸：意见必须能回查到轨迹；与已提交意见出入较大时等待本地核验。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import solo2611 as cli
import submission
from common import Solo2611Error

SUBMITTED = "首轮把订单去重和水位续跑都做完了，重复执行同一批没有新增订单，对账页能看到源库和入库的差异，坏记录被隔离。"
POLISHED = "首轮把订单去重和水位续跑都做完了，重复执行同一批没有新增订单，对账页能看出源库和入库的差异，坏记录已被隔离。"
REVERSED = "质检指出迟到数据仍会覆盖新地址，复查后认为合并逻辑没有生效，续跑从头开始，对账结果缺少差异明细，整体不可用。"

FIELDS = [
    {"field_key": "domain", "label": "所属领域", "field_type": "select", "is_required": True},
    {"field_key": "score_delivery", "label": "交付完整性", "field_type": "number", "is_required": True},
    {"field_key": "feedback", "label": "Feedback", "field_type": "textarea", "is_required": True},
    {"field_key": "trace_file", "label": "轨迹文件", "field_type": "file", "is_required": True},
    {"field_key": "screencast", "label": "运行录屏", "field_type": "file", "is_required": True},
]


class Platform:
    server = "https://solo2.example"

    def __init__(self, *, with_opinion: bool = True) -> None:
        self.updates: list[dict[str, Any]] = []
        self.with_opinion = with_opinion

    def request(self, method: str, path: str, *, payload: Any = None, **_kwargs: Any) -> dict[str, Any]:
        if path == "/api/v1/submissions/form-schema":
            return {"fingerprint": "fp", "fields": FIELDS}
        if method == "GET" and path == "/api/v1/submissions/7":
            detail = {
                "id": 7,
                "status": "PENDING_FIX",
                "editable": True,
                "locked_fields": [],
                "qc_summary": "Feedback 证据不足",
                "domain": "数据工程",
                "trace_file": ["trace"],
                "screencast": "https://example/video.mp4",
            }
            if self.with_opinion:
                detail.update({"score_delivery": 5, "feedback": SUBMITTED})
            return detail
        if method == "PUT" and path == "/api/v1/submissions/7":
            self.updates.append(payload)
            return {"id": 7, "status": "SUBMITTED"}
        raise AssertionError((method, path))


@pytest.fixture
def task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    root = tmp_path / "task"
    (root / "monitor" / "submission").mkdir(parents=True)
    (root / "monitor" / "state.json").write_text("{}", encoding="utf-8")
    trace = root / "trace.jsonl"
    trace.write_text("{}\n", encoding="utf-8")
    row = {"所属领域": "数据工程", "交付完整性": 5, "Feedback": SUBMITTED}
    env: dict[str, Any] = {"root": root, "row": row, "platform": Platform(), "evidence": []}
    monkeypatch.setattr(submission, "ApiClient", lambda _server: env["platform"])
    monkeypatch.setattr(submission, "_read_delivery_row", lambda _root: dict(row))
    monkeypatch.setattr(
        submission, "_payload_from_preview", lambda _root: ({}, {"trace_file": trace, "screencast": trace}, "fp")
    )
    monkeypatch.setattr(submission, "_enforce_domain_lock", lambda **_kwargs: {})
    monkeypatch.setattr(submission, "_load_account_domain_lock", lambda _client: {})
    monkeypatch.setattr(
        submission.fb,
        "check",
        lambda _path, **_kwargs: {
            "ok": not env["evidence"],
            "errors": list(env["evidence"]),
            "score": env.get("mdScore", row["交付完整性"]),
            "feedback": row["Feedback"],
        },
    )
    return env


def _repair(env: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return submission.repair_task(env["root"], 7, comment="按质检意见修订", execute=True, watch=False, **kwargs)


def test_small_revision_goes_straight_through(task: dict[str, Any]) -> None:
    task["row"].update({"交付完整性": 4, "Feedback": POLISHED})
    result = _repair(task)
    assert result["status"] == "fix_submitted"
    assert result["localReview"]["status"] == "not_required"
    assert task["platform"].updates[0]["data"]["score_delivery"] == 4
    assert not submission.local_review_path(task["root"]).exists()


@pytest.mark.parametrize(
    ("score", "text", "reason"),
    [(3, POLISHED, "相差 2 分"), (5, REVERSED, "相似度"), (2, REVERSED, "相差 3 分")],
)
def test_large_change_waits_for_local_review(task: dict[str, Any], score: int, text: str, reason: str) -> None:
    task["row"].update({"交付完整性": score, "Feedback": text})
    held = _repair(task)
    assert held["status"] == "local_review_required" and held["localReviewRequired"] is True
    assert task["platform"].updates == []
    record = json.loads(submission.local_review_path(task["root"]).read_text(encoding="utf-8"))
    assert record["status"] == "pending" and record["qcSummary"] == "Feedback 证据不足"
    assert any(reason in item for item in record["reasons"])
    assert record["before"] == {"score": 5, "feedback": SUBMITTED}
    # 结果文件保留提交编号，工作台据此认定这题已提交，不会被自动清理。
    saved = json.loads((task["root"] / "monitor" / "submission" / "api-result.json").read_text(encoding="utf-8"))
    assert saved["submissionId"] == 7 and saved["status"] == "local_review_required"
    # 没有核验结论时重复执行仍然不提交。
    assert _repair(task)["status"] == "local_review_required"
    assert task["platform"].updates == []


def test_approval_covers_only_the_reviewed_version(task: dict[str, Any]) -> None:
    task["row"].update({"交付完整性": 3, "Feedback": REVERSED})
    assert _repair(task)["status"] == "local_review_required"
    with pytest.raises(Solo2611Error, match="--reviewer"):
        submission.local_review(task["root"], decision="approved")
    approved = submission.local_review(task["root"], decision="approved", reviewer="专家甲", note="已对照轨迹")
    assert approved["ok"] is True
    # 核验之后又改了意见：原结论作废，重新等待核验。
    task["row"]["交付完整性"] = 1
    again = _repair(task)
    assert again["status"] == "local_review_required"
    assert json.loads(submission.local_review_path(task["root"]).read_text(encoding="utf-8"))["status"] == "pending"
    assert task["platform"].updates == []
    submission.local_review(task["root"], decision="approved", reviewer="专家甲")
    done = _repair(task)
    assert done["status"] == "fix_submitted" and done["localReview"]["reviewer"] == "专家甲"
    assert task["platform"].updates[0]["data"]["score_delivery"] == 1


def test_rejected_review_keeps_blocking(task: dict[str, Any]) -> None:
    task["row"].update({"交付完整性": 2, "Feedback": REVERSED})
    _repair(task)
    assert submission.local_review(task["root"], decision="rejected", reviewer="专家甲")["ok"] is False
    assert _repair(task)["localReview"]["status"] == "rejected"
    assert task["platform"].updates == []


def test_opinion_not_backed_by_trace_is_refused(task: dict[str, Any]) -> None:
    task["evidence"].append("Feedback 提到的报错在轨迹里查不到")
    with pytest.raises(Solo2611Error, match="与轨迹证据不一致"):
        _repair(task)
    task["evidence"].clear()
    task["mdScore"] = 4
    with pytest.raises(Solo2611Error, match="与 delivery/feedback.md 不一致"):
        _repair(task)
    assert task["platform"].updates == []


def test_first_submission_snapshot_is_the_fallback_baseline(task: dict[str, Any]) -> None:
    submission._remember_submitted_opinion(task["root"], 7)
    task["platform"] = Platform(with_opinion=False)
    task["row"].update({"交付完整性": 2, "Feedback": REVERSED})
    held = _repair(task)
    assert held["status"] == "local_review_required"
    assert held["localReview"]["baseline"] == "submitted-snapshot"
    # 快照只记首次提交的版本，之后不被覆盖。
    submission._remember_submitted_opinion(task["root"], 7)
    kept = json.loads(submission.submitted_feedback_path(task["root"]).read_text(encoding="utf-8"))
    assert kept["score"] == 5 and kept["feedback"] == SUBMITTED


def test_thresholds_are_configurable(task: dict[str, Any]) -> None:
    task["row"].update({"交付完整性": 4, "Feedback": POLISHED})
    assert _repair(task, review_score_delta=1)["status"] == "local_review_required"


def test_cli_exit_codes(task: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    task["row"].update({"交付完整性": 2, "Feedback": REVERSED})
    argv = ["repair", "--task-root", str(task["root"]), "--submission-id", "7", "--comment", "修订", "--execute"]
    with pytest.raises(SystemExit) as stop:
        cli.main(argv)
    assert stop.value.code == 4
    with pytest.raises(SystemExit) as stop:
        cli.main(["local-review", "--task-root", str(task["root"])])
    assert stop.value.code == 4
    cli.main(["local-review", "--task-root", str(task["root"]), "--approve", "--reviewer", "专家甲"])
    capsys.readouterr()

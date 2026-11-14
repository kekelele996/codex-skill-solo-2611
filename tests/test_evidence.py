"""evidence.py 的独立单元测试：真实运行记录、证据索引、声明回查。

覆盖的是"防幻觉"这条线的边界，不依赖 Docker/网关：
    python3 -m pytest -q tests/test_evidence.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

TMP = Path(tempfile.mkdtemp(prefix="solo2611-evidence-test-"))
os.environ.setdefault("CODEX_HOME", str(TMP / "codex"))
os.environ.setdefault("SOLO2611_CONFIG", str(TMP / "codex" / "solo-2611" / "config.json"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import evidence  # noqa: E402
import feedback as fb  # noqa: E402
from common import Solo2611Error  # noqa: E402

PROMPT = "日批任务要保证重复执行同一批时不新增订单，失败后续跑从已处理水位继续。"
CODE = """\
export function dedupeOrders(rows) {
  return new Map(rows.map((row) => [row.order_id, row]));
}
"""


# ======================================================================= 夹具

def _git(ws: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(ws), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


def _commit(ws: Path, message: str) -> str:
    _git(ws, "add", "-A")
    subprocess.run(
        ["git", "-C", str(ws), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", message],
        check=True, capture_output=True,
    )
    return _git(ws, "rev-parse", "HEAD")


def _events(*, command: str = "python3 checks/verify.py",
            result: str = "重复执行后订单 120 行", final: str = "已完成") -> list[dict]:
    return [
        {"type": "user", "sessionId": "s-1", "promptId": "p-1",
         "message": {"role": "user", "content": PROMPT}},
        {"type": "assistant", "sessionId": "s-1", "message": {"stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": command}}]}},
        {"type": "user", "sessionId": "s-1", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": result}]}},
        {"type": "assistant", "sessionId": "s-1", "message": {"stop_reason": "end_turn",
                                                            "content": [{"type": "text", "text": final}]}},
    ]


def _task(tmp_path: Path, *, files: dict[str, str] | None = None,
          artifact_files: dict[str, str] | None = None,
          events: list[dict] | None = None) -> tuple[Path, dict, Path]:
    root = tmp_path / "task"
    ws = root / "workspace"
    ws.mkdir(parents=True)
    _git(ws, "init", "-q")
    for name, text in (files or {"src/ingest.ts": "export const append = true;\n"}).items():
        target = ws / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    initial = _commit(ws, "initial")
    sha = initial
    if artifact_files:
        for name, text in artifact_files.items():
            target = ws / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        sha = _commit(ws, "artifact")
    prompt_path = root / "delivery" / "提示词.md"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(PROMPT, encoding="utf-8")
    if events is not None:
        project = root / "traces" / "-workspace"
        project.mkdir(parents=True)
        (project / "s-1.jsonl").write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", encoding="utf-8",
        )
    state = {
        "workspace": str(ws),
        "initialSha": initial,
        "artifact": {"sha": sha},
        "promptPath": str(prompt_path),
    }
    (root / "monitor").mkdir(parents=True, exist_ok=True)
    (root / "monitor" / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return root, state, ws


def _record(root: Path, ws: Path, script: str, *, exit_code: int = 0, label: str = "重跑同一批") -> dict:
    (ws / "checks").mkdir(exist_ok=True)
    target = ws / "checks" / "verify.py"
    target.write_text(script, encoding="utf-8")
    return evidence.record_run(root, ws, [sys.executable, "checks/verify.py"], label=label)


def _errors(root: Path, state: dict, feedback: str, score: int = 3) -> list[str]:
    return evidence.check(root, state, feedback, score)["errors"]


# ======================================================================= 真实运行记录

def test_record_run_refuses_synthetic_commands(tmp_path):
    root, state, ws = _task(tmp_path)
    for argv, reason in [
        (["echo", "订单 120 行"], "回显"),
        (["printf", "120\n"], "回显"),
        (["true"], "回显"),
        (["python3", "-c", "print(120)"], "内联"),
        (["bash", "-c", "pytest -q"], "内联"),
        (["node", "-e", "console.log(120)"], "内联"),
    ]:
        with pytest.raises(Solo2611Error, match=reason):
            evidence.record_run(root, ws, argv, label="伪造")
    assert evidence.runs(root) == []

    with pytest.raises(Solo2611Error, match="--label"):
        evidence.record_run(root, ws, [sys.executable, "-V"], label="  ")
    with pytest.raises(Solo2611Error, match="真实执行的命令"):
        evidence.record_run(root, ws, ["--"], label="空命令")


def test_record_run_keeps_exit_code_output_and_log(tmp_path):
    root, state, ws = _task(tmp_path)
    ok = _record(root, ws, "print('订单 120 行')\n")
    assert ok["exitCode"] == 0 and ok["index"] == 1 and not ok["timedOut"]
    assert "订单 120 行" in ok["stdout"]
    assert len(ok["outputSha256"]) == 64
    log = Path(ok["log"])
    assert log.is_file() and "订单 120 行" in log.read_text(encoding="utf-8")

    # 失败的命令同样落记录：它是失败结论的证据，不能因为非 0 就丢掉
    bad = _record(root, ws, "raise SystemExit('boom')\n", exit_code=1, label="故意失败")
    assert bad["exitCode"] == 1
    assert "boom" in Path(bad["log"]).read_text(encoding="utf-8")
    assert [(r["index"], r["exitCode"]) for r in evidence.runs(root)] == [(1, 0), (2, 1)]


# ======================================================================= 数字抽取与派生

def test_claim_numbers_are_extracted_next_to_chinese():
    """中文里写数字是最常见的形态，边界不能按 \\w 判断。"""
    assert evidence._numbers("订单从120行变成240行") == {"120", "240"}
    assert evidence._numbers("耗时 1.5 秒，覆盖 3 个场景") == {"1.5", "3"}
    # 标识符和版本号里的数字不是声明
    assert evidence._numbers("v2.1.303 与 2048x2048") == set()
    assert evidence._numbers("test_late_merge2 与 sha256sum") == set()


def test_derived_numbers_accept_differences_between_real_runs(tmp_path):
    root, state, ws = _task(tmp_path)
    _record(root, ws, "print('orders=120')\n", label="第一次")
    _record(root, ws, "print('orders=200')\n", label="第二次")
    index, _ = evidence.build_index(root, state)
    assert {"80", "40"} <= set(index["derivedNumbers"])
    assert "80" not in evidence._numbers("orders=120 orders=200")
    # 真实输出之间的差不逼着人去改数字
    assert not [e for e in _errors(root, state, "重跑后订单由 120 增到 200，多出 80 条。") if "数值" in e]


# ======================================================================= 声明回查

def test_fabricated_numbers_and_paths_are_blocked(tmp_path):
    root, state, ws = _task(
        tmp_path,
        artifact_files={"src/dedupe.ts": CODE},
        events=_events(),
    )
    _record(root, ws, "print('重复执行后订单 120 行')\n")

    assert _errors(root, state, "重跑后订单仍是 120 行，重复数据被跳过。") == []
    errors = _errors(root, state, "重跑后订单涨到 240 行，重复数据没有被跳过。")
    assert any("240" in item and "数值" in item for item in errors)
    assert _errors(root, state, "改动写在 src/dedupe.ts 里，重跑后订单仍是 120 行。") == []

    errors = _errors(root, state, "改动写在 src/nowhere.ts 里，重跑后订单仍是 120 行。")
    assert any("src/nowhere.ts" in item and "不存在" in item for item in errors)


def test_command_arguments_cannot_launder_a_number(tmp_path):
    """把编造的数字写进命令参数再执行一次，不能让这个数字变成"事实"。"""
    root, state, ws = _task(
        tmp_path,
        artifact_files={"src/dedupe.ts": CODE},
        events=_events(command="python3 checks/verify.py --expect 999", result="重复执行后订单 120 行"),
    )
    _record(root, ws, "print('重复执行后订单 120 行')\n")

    errors = _errors(root, state, "重跑后订单仍是 999 行，重复数据被跳过。")
    assert any("999" in item for item in errors)
    assert _errors(root, state, "重跑后订单仍是 120 行，重复数据被跳过。") == []


def test_backticked_identifiers_must_exist_in_the_artifact(tmp_path):
    root, state, ws = _task(
        tmp_path,
        artifact_files={"src/dedupe.ts": CODE},
        events=_events(),
    )
    _record(root, ws, "print('重复执行后订单 120 行')\n")

    assert _errors(root, state, "`dedupeOrders` 重跑后订单仍是 120 行。") == []
    errors = _errors(root, state, "`dedupeOrdersV2` 重跑后订单仍是 120 行。")
    assert any("dedupeOrdersV2" in item and "代码标识" in item for item in errors)


def test_backticked_errors_must_be_copied_from_real_output(tmp_path):
    root, state, ws = _task(tmp_path, events=_events(result="Error: duplicate order_id 7"))
    _record(root, ws, "import sys; print('orders=120'); print('Error: duplicate order_id 7', file=sys.stderr)\n")

    assert _errors(root, state, "运行时报出 `Error: duplicate order_id 7`，订单仍是 120 行。") == []
    errors = _errors(root, state, "运行时报出 `Error: constraint uq_order violated`，订单仍是 120 行。")
    assert any("uq_order violated" in item for item in errors)


def test_step_reference_cannot_exceed_trace_steps(tmp_path):
    root, state, ws = _task(tmp_path, events=_events())
    _record(root, ws, "print('重复执行后订单 120 行')\n")
    assert _errors(root, state, "第 1 步执行后订单仍是 120 行。") == []
    errors = _errors(root, state, "第 9 步执行后订单仍是 120 行。")
    assert any("第 9 步" in item and "只有 1 个工具步骤" in item for item in errors)


# ======================================================================= 运行前提

def test_feedback_without_any_run_is_blocked(tmp_path):
    root, state, ws = _task(tmp_path, events=_events())
    errors = _errors(root, state, "重跑后订单仍是 120 行。")
    assert any("没有对应的真实执行记录" in item for item in errors)


def test_high_score_requires_a_successful_run(tmp_path):
    root, state, ws = _task(tmp_path)
    _record(root, ws, "raise SystemExit(3)\n", exit_code=3, label="启动失败")
    high = _errors(root, state, "服务在启动阶段报错，无法进入页面。", score=4)
    assert any("没有一次成功退出" in item for item in high)
    low = _errors(root, state, "服务在启动阶段报错，无法进入页面。", score=2)
    assert not any("没有一次成功退出" in item for item in low)


def test_failure_claims_need_real_failure_output(tmp_path):
    root, state, ws = _task(tmp_path, events=_events(result="重复执行后订单 120 行"))
    _record(root, ws, "print('重复执行后订单 120 行')\n")
    errors = _errors(root, state, "重跑同一批时服务崩溃，订单无法推进。", score=2)
    assert any("没有任何失败输出" in item for item in errors)

    _record(root, ws, "raise SystemExit('duplicate key')\n", exit_code=1, label="故意重跑失败")
    state = dict(state)
    errors = _errors(root, state, "重跑同一批时服务崩溃，订单无法推进。", score=2)
    assert not any("没有任何失败输出" in item for item in errors)


def test_failure_evidence_may_come_from_trace(tmp_path):
    """轨迹里真实出现过 is_error 的工具结果，也算失败证据。"""
    events = _events(result="重复执行后订单 120 行")
    events[2]["message"]["content"][0]["is_error"] = True
    root, state, ws = _task(tmp_path, events=events)
    _record(root, ws, "print('重复执行后订单 120 行')\n")
    errors = _errors(root, state, "第一次运行报错，重跑后订单仍是 120 行。", score=2)
    assert not any("没有任何失败输出" in item for item in errors)


def test_feedback_cannot_claim_business_runtime_when_trace_is_static_only(tmp_path):
    """V2 回归：轨迹明确未跑后端时，反馈不能写业务链路“验证了/跑通”。"""
    events = _events(
        result="No module named pip",
        final="前端 vite build 通过。没有 pip 和 Docker，后端无法跑运行时验证。",
    )
    root, state, ws = _task(tmp_path, events=events)
    _record(root, ws, "print('static check complete')\n")

    overclaim = evidence.check(
        root,
        state,
        "验证了规则删除和同名重报后旧报告保持不变。",
        3,
    )
    alignment = overclaim["traceAlignment"]
    assert alignment["status"] == "failed"
    assert alignment["traceCapabilityLevel"] == "runtime-blocked-static-only"
    assert alignment["unsupportedRuntimeClaims"]
    assert any("超出轨迹能证明的验证层级" in item for item in overclaim["errors"])

    bounded = evidence.check(
        root,
        state,
        "代码已经保留历史规则，报告优先读取批次数据；"
        "数据库迁移和同名重报没有运行验证，仍是待补缺口。",
        3,
    )
    assert bounded["traceAlignment"]["status"] == "passed"
    assert bounded["traceAlignment"]["unsupportedRuntimeClaims"] == []
    assert not any("超出轨迹能证明的验证层级" in item for item in bounded["errors"])


def test_high_score_against_negative_trace_waits_for_local_review(tmp_path):
    events = _events(final="未能完成规则更新，关键导入路径仍然缺少校验。")
    root, state, ws = _task(tmp_path, events=events)
    _record(root, ws, "print('本轮检查完成')\n")
    checked = evidence.check(
        root,
        state,
        "逐项核对后规则更新已经完成，导入路径能够正常通过校验。",
        4,
    )
    alignment = checked["traceAlignment"]
    assert alignment["traceOpinion"]["status"] == "negative"
    assert alignment["traceOpinion"]["conflicts"]

    pending = fb._review_consistency(
        root,
        score=4,
        feedback="逐项核对后规则更新已经完成，导入路径能够正常通过校验。",
        alignment=alignment,
    )
    assert pending["required"] is True and pending["status"] == "pending"
    approved = fb.review_consistency(
        root,
        decision="approved",
        reviewer="专家甲",
        note="已对照本机运行结果和原始轨迹",
    )
    assert approved["status"] == "approved"
    again = fb._review_consistency(
        root,
        score=4,
        feedback="逐项核对后规则更新已经完成，导入路径能够正常通过校验。",
        alignment=alignment,
    )
    assert again["status"] == "approved"


def test_feedback_check_returns_local_review_gate_for_trace_disagreement(tmp_path):
    events = _events(final="未能完成规则更新，关键导入路径仍然缺少校验。")
    root, state, ws = _task(tmp_path, events=events)
    _record(root, ws, "print('订单 120 行')\n")
    path = root / "delivery" / "feedback.md"
    path.write_text(
        "## 交付完整性\n\n4\n\n## Feedback\n\n"
        "过程问题：逐项核对后规则更新已经完成，导入路径能够正常通过校验，重复执行同一批保持结果不变。\n\n"
        "产物问题：页面能够展示当前规则，接口回读正常，失败输入会给出明确提示，恢复后仍能继续处理；"
        "错误输入的边界回归仍需在恢复入口补齐。\n",
        encoding="utf-8",
    )
    checked = fb.check(path, task_root=root, state=state)
    assert checked["localReviewRequired"] is True
    assert checked["localReviewStatus"] == "pending"
    assert checked["ok"] is False

    fb.review_consistency(
        root,
        decision="approved",
        reviewer="专家甲",
        note="已对照原始轨迹和本机运行",
    )
    approved = fb.check(path, task_root=root, state=state)
    assert approved["localReviewStatus"] == "approved"
    assert not any("总体结论出入较大" in item for item in approved["errors"])


# ======================================================================= 索引与摘要

def test_index_summary_shape(tmp_path):
    root, state, ws = _task(
        tmp_path,
        artifact_files={"src/dedupe.ts": CODE},
        events=_events(),
    )
    _record(root, ws, "print('重复执行后订单 120 行')\n")
    verdict = evidence.check(root, state, "重跑后订单仍是 120 行。", 3)
    index = verdict["index"]
    assert index["artifactSha"] == state["artifact"]["sha"]
    assert index["steps"] == 1
    assert "src/dedupe.ts" in index["paths"]
    assert index["runtime"] == {"runs": 1, "ok": 1, "failed": 0, "labels": ["重跑同一批"]}
    assert index["failureInTrace"] is False
    assert verdict["summary"]["claims"] >= 1
    assert verdict["summary"]["unverified"] == 0
    assert verdict["ok"] is True

    written = root / "monitor" / "runtime-verification.json"
    assert len(json.loads(written.read_text(encoding="utf-8"))["runs"]) == 1

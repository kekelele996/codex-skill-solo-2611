"""不依赖 Docker/网关的全链路测试：python3 -m pytest -q tests/"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

TMP = Path(tempfile.mkdtemp(prefix="solo2611-test-"))
os.environ["CODEX_HOME"] = str(TMP / "codex")
os.environ["SOLO2611_CONFIG"] = str(TMP / "codex" / "solo-2611" / "config.json")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import excel  # noqa: E402
import feedback as fb  # noqa: E402
import github_repo as gh  # noqa: E402
import prompting  # noqa: E402
import recorder  # noqa: E402
import solo2611 as cli  # noqa: E402
import submission  # noqa: E402
import trace as tr  # noqa: E402
from common import Solo2611Error, container_slug, load_config, load_state, save_config, sha256_bytes  # noqa: E402
from spec import DOMAINS, SUBMIT_COLUMNS, scan_banned, validate_classification  # noqa: E402

PROMPT = (
    "日批任务现在按文件到达顺序追加订单，迟到数据会覆盖同一订单的新地址。希望批次可见前合并重复记录，"
    "按来源时间保留较新版本，重复执行同一批时不能新增订单，失败后续跑要从已处理水位继续；"
    "对账结果要能看出源库和入库的差异，坏记录隔离后不影响整批提交。"
)


def _write_trace(folder: Path, session: str, prompt: str, *, end: str = "end_turn") -> Path:
    project = folder / "-workspace"
    project.mkdir(parents=True)
    events = [
        {"type": "queue-operation", "operation": "enqueue", "sessionId": session},
        {"type": "user", "sessionId": session, "promptId": "p-123", "version": "2.1.303", "entrypoint": "sdk-cli",
         "timestamp": "2026-11-10T02:00:00Z", "message": {"role": "user", "content": prompt}},
        {"type": "assistant", "sessionId": session, "timestamp": "2026-11-10T02:00:05Z",
         "message": {"model": "auto_model/urm", "stop_reason": "tool_use", "content": [
             {"type": "text", "text": "先看一下阴影相关代码"},
             {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "grep -rn shadow src"}}]}},
        {"type": "user", "sessionId": session, "timestamp": "2026-11-10T02:00:06Z",
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "src/shadow.ts:1"}]}},
        {"type": "assistant", "sessionId": session, "timestamp": "2026-11-10T02:01:00Z",
         "message": {"model": "auto_model/urm", "stop_reason": end, "content": [{"type": "text", "text": "已完成"}]}},
    ]
    path = project / f"{session}.jsonl"
    path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", encoding="utf-8")
    return path


def test_banned_topics():
    blocks, _ = scan_banned("做一个烟花粒子效果")
    assert blocks
    blocks, warns = scan_banned("渲染引擎的 GPU 粒子排序，阴影贴图 2048x2048")
    assert not blocks and "粒子" in warns
    assert not scan_banned("float load boat")[0]  # OA 不能误伤
    assert scan_banned("一个 OA 审批系统")[0]
    assert scan_banned("做一个 CSV 看板", "数据工程")[0]


def test_container_slug_is_docker_safe_for_chinese_task_names():
    slug = container_slug("特征平台点时间正确性与回填")
    name = f"solo2611-{slug}-01-a1b2".lower()
    assert re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", name)
    assert len(name) <= 63
    assert container_slug("Feature PIT Backfill") == "feature-pit-backfill"


def test_all_domain_subdirections_are_supported():
    for domain, sub_directions in DOMAINS.items():
        for sub_direction in sub_directions:
            normalized, errors = validate_classification(domain, sub_direction)
            assert not errors
            assert normalized == sub_direction
    _, errors = validate_classification("数据工程", "图形渲染")
    assert errors


def test_non_data_domain_skips_data_engineering_signal_warning(tmp_path, monkeypatch):
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text(
        "管理员发现权限变更后旧会话仍可继续操作，希望立即撤销既有会话，并让审计记录能追踪每次凭证使用。",
        encoding="utf-8",
    )
    monkeypatch.setattr(prompting, "_validate_difficulty", lambda *args, **kwargs: {})
    monkeypatch.setattr(prompting, "validate_ra_renhua_review", lambda *args, **kwargs: [])
    monkeypatch.setattr(prompting, "validate_ra_renhua_external", lambda *args, **kwargs: [])
    monkeypatch.setattr(prompting, "_history_errors", lambda *args, **kwargs: ([], []))
    result = prompting.validate_candidate(
        task_root=tmp_path,
        prompt_path=prompt_path,
        prompt_text=prompt_path.read_text(encoding="utf-8"),
        domain="安全",
        difficulty="困难",
        review_path=tmp_path / "review.json",
        difficulty_review_path=tmp_path / "difficulty.json",
        history=[],
    )
    assert not any("数据工程信号" in item for item in result["warnings"])


def test_legacy_valid_domain_is_preserved():
    path = Path(os.environ["SOLO2611_CONFIG"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "configVersion": 1,
        "defaults": {"domain": "游戏、图形与多媒体", "subDirection": "图形渲染"},
    }, ensure_ascii=False), encoding="utf-8")
    config = load_config()
    assert config["configVersion"] == 3
    assert config["defaults"]["domain"] == "游戏、图形与多媒体"
    assert config["defaults"]["subDirection"] == "图形渲染"


def test_legacy_data_other_default_migrates_to_data_lake():
    path = Path(os.environ["SOLO2611_CONFIG"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "configVersion": 2,
        "defaults": {"domain": "数据工程", "subDirection": "其他"},
    }, ensure_ascii=False), encoding="utf-8")
    config = load_config()
    assert config["configVersion"] == 3
    assert config["defaults"]["domain"] == "数据工程"
    assert config["defaults"]["subDirection"] == "数仓与数据湖"
    path.unlink()


def test_submission_auto_switch_defaults_off():
    config = load_config()
    assert config["submission"]["autoSubmit"] is False
    assert config["submission"]["server"] == "https://solo2.jzxhnh.com"


def test_submission_id_extraction():
    assert submission._extract_submission_id({"id": 7, "status": "SUBMITTED"}) == 7
    assert submission._extract_submission_id({"data": {"submission_id": 9}}) == 9
    assert submission._extract_submission_id({"items": [{"id": 11, "status": "QC_PASSED"}]}) == 11


def test_submission_uses_only_workbench_platform_session(monkeypatch):
    calls = []

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload
            self.status_code = 200
            self.text = json.dumps(payload)

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def fake_get(url, timeout):
        calls.append(("GET", url))
        return FakeResponse(
            {
                "solo2": {
                    "base_url": "https://solo2.example/app",
                    "username": "lixiaoyu",
                    "password": "<set>",
                }
            }
        )

    def fake_post(url, json, timeout):
        calls.append(("POST", url, json))
        return FakeResponse(
            {
                "ok": True,
                "account": "lixiaoyu",
                "server": "https://solo2.example",
                "cookie": "sid=fresh",
                "csrf": "fresh-csrf",
            }
        )

    monkeypatch.setattr(submission.requests, "get", fake_get)
    monkeypatch.setattr(submission.requests, "post", fake_post)
    platform = submission._workbench_platform()

    assert platform["username"] == "lixiaoyu"
    assert platform["server"] == "https://solo2.example"
    assert platform["cookie"] == "sid=fresh"
    assert calls[0][1] == submission.MONITOR_CONFIG_URL
    assert calls[1][1] == submission.MONITOR_SESSION_URL


def test_submission_rejects_workbench_platform_identity_mismatch(monkeypatch):
    class FakeResponse:
        status_code = 200
        text = "{}"

        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    monkeypatch.setattr(
        submission.requests,
        "get",
        lambda url, timeout: FakeResponse(
            {
                "solo2": {
                    "base_url": "https://solo2.example/app",
                    "username": "lixiaoyu",
                    "password": "<set>",
                }
            }
        ),
    )
    monkeypatch.setattr(
        submission.requests,
        "post",
        lambda url, json, timeout: FakeResponse(
            {
                "ok": True,
                "account": "liudong",
                "server": "https://solo2.example",
                "cookie": "sid=stale",
                "csrf": "stale-csrf",
            }
        ),
    )

    with pytest.raises(Solo2611Error, match="身份校验失败"):
        submission._workbench_platform()


def test_submission_source_has_no_direct_keychain_or_cached_session_access():
    source = Path(submission.__file__).read_text(encoding="utf-8")
    assert "_keychain_read" not in source
    assert "_keychain_write" not in source
    assert "find-generic-password" not in source
    assert "SOLO2_COOKIE" not in source
    assert "SOLO2_CSRF" not in source
    assert "SOLO2_PASSWORD" not in source


def test_github_repo_slug_is_readable():
    assert gh.repo_slug("数据脱敏与 k-匿名合规工作台") == (
        "data-masking-k-anonymity-compliance-workbench"
    )
    assert gh.repo_slug("列级数据血缘与影响分析平台") == (
        "column-level-data-lineage-impact-analysis-platform"
    )


def test_github_ensure_repo_uses_ascii_slug(tmp_path, monkeypatch):
    root = tmp_path / "task"
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    (root / "monitor").mkdir()
    captured = {}

    def fake_create(_config, name, private, description):
        captured["name"] = name
        return {
            "full_name": f"owner/{name}-abcd",
            "html_url": f"https://github.com/owner/{name}-abcd",
            "clone_url": f"https://github.com/owner/{name}-abcd.git",
            "private": private,
        }

    monkeypatch.setattr(gh, "_create", fake_create)
    monkeypatch.setattr(gh, "git_env", lambda _config: {})
    monkeypatch.setattr(gh, "push_sha", lambda *args, **kwargs: None)
    monkeypatch.setattr(gh, "verify", lambda *args, **kwargs: True)
    state = {
        "taskName": "数据脱敏与 k-匿名合规工作台",
        "workspace": str(workspace),
        "initialSha": "a" * 40,
    }
    remote = gh.ensure_repo(root, state, {})
    assert captured["name"] == "data-masking-k-anonymity-compliance-workbench"
    assert remote["fullName"].startswith(
        "owner/data-masking-k-anonymity-compliance-workbench-"
    )


def test_account_domain_lock_uses_earliest_submission():
    lock = submission._domain_lock_from_items([
        {"id": 12, "domain": "数据工程", "submitted_at": "2026-11-12T10:00:00"},
        {"id": 3, "domain": "安全", "submitted_at": "2026-11-10T09:00:00"},
    ])
    assert lock["domain"] == "安全"
    assert lock["firstSubmissionId"] == 3


def test_account_domain_comes_only_from_locked_config_and_is_read_only():
    assert cli.account_domain({"submission": {"lockedDomain": "数据工程"}}) == "数据工程"
    with pytest.raises(Solo2611Error, match="缺少 submission.lockedDomain"):
        cli.account_domain({"submission": {"lockedDomain": ""}})
    assert cli.validate_locked_domain_change("", "数据工程") == "数据工程"
    with pytest.raises(Solo2611Error, match="只读大类"):
        cli.validate_locked_domain_change("数据工程", "安全")
    with pytest.raises(Solo2611Error, match="合法大类"):
        cli.validate_locked_domain_change("", "其他")
    with pytest.raises(Solo2611Error, match="缺少 submission.lockedDomain"):
        submission._enforce_domain_lock(
            submitted_domain="数据工程",
            configured_domain="",
            history_lock={"domain": "数据工程", "source": "submission_history"},
        )


def test_submission_payload_never_contains_other_issues(tmp_path, monkeypatch):
    root = tmp_path / "task"
    delivery = root / "delivery"
    delivery.mkdir(parents=True)
    prompt_path = delivery / "提示词.md"
    trace_path = root / "trace.jsonl"
    video_path = root / "video.mp4"
    prompt_path.write_text("补齐列后重新分析影响范围。", encoding="utf-8")
    trace_path.write_text("{}\n", encoding="utf-8")
    video_path.write_bytes(b"video")
    state = {
        "promptPath": str(prompt_path),
        "session": {"trace": str(trace_path)},
        "recording": {"video": str(video_path)},
    }
    (root / "monitor").mkdir()
    (root / "monitor" / "state.json").write_text(json.dumps(state), encoding="utf-8")

    fields = [
        {"field_key": "domain", "label": "所属领域", "field_type": "select", "is_required": True},
        {"field_key": "sub_direction", "label": "子方向", "field_type": "select", "is_required": True},
        {"field_key": "user_prompt", "label": "User Prompt", "field_type": "text", "is_required": True},
        {"field_key": "session_id", "label": "SessionID", "field_type": "text", "is_required": True},
        {"field_key": "turn_id", "label": "TurnID/PromptID", "field_type": "text", "is_required": True},
        {"field_key": "score_delivery", "label": "交付完整性", "field_type": "number", "is_required": True},
        {"field_key": "feedback", "label": "Feedback", "field_type": "textarea", "is_required": True},
        {"field_key": "other_issues", "label": "备注", "field_type": "textarea", "is_required": False},
        {"field_key": "trace_file", "label": "轨迹文件", "field_type": "file", "is_required": True},
        {"field_key": "screencast", "label": "运行录屏", "field_type": "file", "is_required": True},
    ]
    row = {
        "所属领域": "数据工程",
        "子方向": "数仓与数据湖",
        "User Prompt": prompt_path.read_text(encoding="utf-8"),
        "SessionID": "session-1",
        "TurnID/PromptID": "prompt-1",
        "交付完整性": 5,
        "Feedback": "过程与产物均已核对。",
        "备注": "不得提交的备注",
    }

    class FakeApi:
        server = "https://solo2.example"
        username = "tester"

        def __init__(self):
            self.create_payload = None
            self.update_payload = None

        def request(self, method, path, *, payload=None, files=None, data=None, timeout=120):
            if path == "/api/v1/submissions/form-schema":
                return {"fingerprint": "fp-test", "fields": fields}
            if path.startswith("/api/v1/submissions?"):
                return {"items": [], "meta": {"total": 0}}
            if method == "POST" and path == "/api/v1/submissions":
                self.create_payload = payload
                return {"id": 7, "status": "SUBMITTED"}
            if method == "GET" and path == "/api/v1/submissions/7":
                return {
                    "id": 7,
                    "status": "PENDING_FIX",
                    "editable": True,
                    "locked_fields": [],
                    "trace_file": ["existing-trace"],
                    "screencast": "https://example/video.mp4",
                    "other_issues": "服务端旧备注",
                    "session_id": "session-1",
                    "turn_id": "prompt-1",
                    "user_prompt": prompt_path.read_text(encoding="utf-8"),
                    "score_delivery": 5,
                    "feedback": "过程与产物均已核对。",
                }
            if method == "PUT" and path == "/api/v1/submissions/7":
                self.update_payload = payload
                return {"id": 7, "status": "SUBMITTED"}
            raise AssertionError((method, path))

        def upload(self, path, kind):
            if kind == "video":
                return {"url": "https://example/video.mp4"}
            return {"name": path.name, "path": "trace/path.jsonl", "size": path.stat().st_size}

    client = FakeApi()
    monkeypatch.setattr(submission, "ApiClient", lambda _server: client)
    monkeypatch.setattr(submission, "_read_delivery_row", lambda _root: row)
    monkeypatch.setattr(submission.tr, "analyze", lambda _path, _prompt: {"ok": True, "errors": []})
    monkeypatch.setattr(
        submission,
        "_probe_video",
        lambda _path: {"width": 1280, "height": 720, "duration": 1.0, "size": 5},
    )
    monkeypatch.setattr(submission.fb, "check", lambda _path: {"ok": True, "errors": [], "warnings": []})

    prepared = submission.preflight(root, server=client.server, locked_domain="数据工程")
    preview = json.loads((root / "monitor" / "submission" / "payload-preview.json").read_text(encoding="utf-8"))
    assert prepared["fieldCount"] == 9
    assert prepared["accountDomainLock"]["effectiveDomain"] == "数据工程"
    assert "other_issues" not in preview["data"]

    with pytest.raises(Solo2611Error, match="账号锁定领域"):
        submission.preflight(root, server=client.server, locked_domain="安全")

    result = submission.submit_task(
        root,
        execute=True,
        watch=False,
        server=client.server,
        locked_domain="数据工程",
    )
    assert result["submissionId"] == 7
    assert "other_issues" not in client.create_payload["data"]

    payload_path = root / "monitor" / "submission" / "payload-preview.json"
    stale = json.loads(payload_path.read_text(encoding="utf-8"))
    stale["data"]["other_issues"] = "陈旧预览里的备注"
    payload_path.write_text(json.dumps(stale, ensure_ascii=False), encoding="utf-8")
    repaired = submission.repair_task(
        root,
        7,
        comment="按评审要求更新反馈描述",
        execute=True,
        watch=False,
        server=client.server,
        locked_domain="数据工程",
    )
    assert repaired["status"] == "fix_submitted"
    assert "other_issues" not in client.update_payload["data"]


def test_existing_submission_keeps_qc_passed_status(tmp_path, monkeypatch):
    saved = []
    monkeypatch.setattr(
        submission,
        "preflight",
        lambda _root, server=submission.DEFAULT_SERVER, locked_domain="": {
            "existingSubmission": {
                "id": 7,
                "status": "QC_PASSED",
                "status_label": "质检通过",
                "qc_conclusion": "PASS",
            }
        },
    )
    monkeypatch.setattr(submission, "_save_result", lambda _root, result: saved.append(result))
    result = submission.submit_task(tmp_path, execute=False, watch=False)
    assert result["ok"] is True
    assert result["status"] == "qc_passed"
    assert result["statusLabel"] == "质检通过"
    assert result["submissionId"] == 7
    assert saved == [result]


def test_prompt_hashes_distinguish_review_text_from_file_bytes():
    text = PROMPT + "\n"
    assert prompting.prompt_sha256(text) == prompting.prompt_sha256(PROMPT)
    assert prompting.prompt_file_sha256(text) == sha256_bytes(text.encode("utf-8"))
    assert prompting.prompt_file_sha256(text) != prompting.prompt_sha256(text)


def _fill_prompt_evidence(root: Path, prompt: str) -> tuple[Path, Path]:
    templates = prompting.ensure_templates(root)
    diff_path = Path(templates["difficultyReview"])
    review_path = Path(templates["raRenhuaReview"])
    data = json.loads(diff_path.read_text(encoding="utf-8"))
    data.update({
        "difficulty": "困难",
        "verdict": "困难",
        "reviewedBy": "test",
        "mediumShapeDefense": "",
        "signals": {
            "multiModule": {
                "passed": True,
                "modules": ["采集入口", "批次状态", "入库结果"],
                "crossModuleInvariant": "批次状态和入库行数必须一起推进，不能出现批次已完成但订单未入库。",
                "evidence": "src/ingest.ts 负责批次推进，src/reconcile.ts 负责入库差异读取。",
            },
            "designTradeoff": {
                "passed": True,
                "conflict": "迟到数据要保留新地址，但同一订单不能因重复执行产生多条入库记录。",
                "decision": "按来源时间裁决版本，并以批次和订单主键共同保证重跑幂等。",
                "whyNotRoutine": "这不是补一个状态字段，重跑与迟到数据同时作用时会产生相互覆盖。",
                "evidence": "现有 src/ingest.ts 按文件顺序追加，src/reconcile.ts 对账时会暴露重复行。",
            },
            "complexConcern": {
                "passed": True,
                "kinds": ["idempotency", "failure-recovery", "late-data"],
                "technicalRisk": "同一批次重跑和迟到数据同时出现时，水位与订单版本可能互相覆盖。",
                "observableFailure": "重复执行后订单数翻倍，或旧地址覆盖掉已经入库的新地址。",
                "evidence": "现有文件顺序追加逻辑没有同一主键去重，也没有记录处理水位。",
            },
        },
        "platformSignals": {
            "multiModule": {
                "answer": "任务同时涉及文件采集、批次状态、订单入库和对账读取，跨多个既有数据边界。",
                "promptQuote": "迟到数据会覆盖同一订单的新地址",
                "ifViolated": "旧地址会覆盖新地址，对账结果与源库长期不一致。",
            },
            "designTradeoff": {
                "answer": "必须在保留迟到数据的新版本和重跑不新增记录之间确定版本裁决与幂等边界。",
                "promptQuote": "重复执行同一批时不能新增订单",
                "ifViolated": "重跑会产生重复订单，下游计数和对账结果都会失真。",
            },
            "complexConcern": {
                "answer": "失败恢复要求水位持久化，重跑时还要避免重复处理已经提交的记录。",
                "promptQuote": "失败后续跑要从已处理水位继续",
                "ifViolated": "失败后从头重跑，订单重复写入或批次状态倒退。",
            },
        },
        "crossObjectInvariant": {
            "objects": ["订单", "批次"],
            "invariant": "同一个批次完成后，源数据中每个订单只能对应一条当前有效入库记录。",
            "divergenceFailure": "批次显示完成但订单仍重复，或者旧版本把新版本覆盖掉。",
        },
        "projectEvidence": {
            "files": ["src/ingest.ts", "src/reconcile.ts"],
            "existingFlow": "现有流程从文件读取订单后顺序追加，再由对账代码汇总源库与入库差异。",
            "whyNotRoutine": "题目同时改变追加写入、水位推进和重复执行结果，不是补一个筛选条件。",
        },
        "substantiveComplexity": {
            "kinds": ["idempotency", "failure-recovery", "late-data"],
            "promptQuote": "失败后续跑要从已处理水位继续",
            "whyHard": "恢复点、订单版本和批次可见性必须同时成立，任何一处覆盖都会污染最终数据。",
            "visibleFailure": "重跑后订单数翻倍，或者批次完成后仍读到旧地址。",
        },
    })
    diff_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review.update({
        "textSha256": prompting.prompt_sha256(prompt),
        "passed": True,
    })
    review_path.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
    return diff_path, review_path


def test_trace_analyze(tmp_path):
    good = _write_trace(tmp_path / "a", "s-1", PROMPT)
    info = tr.analyze(good, PROMPT)
    assert info["ok"], info["errors"]
    assert info["promptId"] == "p-123" and info["harnessVersion"] == "2.1.303" and info["toolUses"] == 1
    bad = _write_trace(tmp_path / "b", "s-2", PROMPT, end="max_tokens")
    assert not tr.analyze(bad, PROMPT)["ok"]
    assert not tr.analyze(good, PROMPT + "改过")["ok"]


def _trace_with_final(tmp_path: Path, name: str, final: str, *, drop_result: bool = False) -> Path:
    folder = tmp_path / name
    project = folder / "-workspace"
    project.mkdir(parents=True)
    events = [
        {"type": "user", "sessionId": "s-x", "promptId": "p-1", "version": "2.1.303", "entrypoint": "sdk-cli",
         "timestamp": "2026-11-10T02:00:00Z", "message": {"role": "user", "content": PROMPT}},
        {"type": "assistant", "sessionId": "s-x", "timestamp": "2026-11-10T02:00:05Z",
         "message": {"model": "auto_model/urm", "stop_reason": "tool_use", "content": [
             {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]}},
    ]
    if not drop_result:
        events.append({"type": "user", "sessionId": "s-x", "timestamp": "2026-11-10T02:00:06Z",
                       "message": {"role": "user", "content": [
                           {"type": "tool_result", "tool_use_id": "t1", "content": "a b c"}]}})
    events.append({"type": "assistant", "sessionId": "s-x", "timestamp": "2026-11-10T02:01:00Z",
                   "message": {"model": "auto_model/urm", "stop_reason": "end_turn",
                               "content": [{"type": "text", "text": final}]}})
    path = project / "s-x.jsonl"
    path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", encoding="utf-8")
    return path


def test_completion_review_rejects_unfinished_turns(tmp_path):
    """结束标志是 end_turn，但最后还在问用户、许诺下一步、或者断在半路，都不算真的做完。"""
    ok = _trace_with_final(tmp_path, "ok", "合并规则已实现，重跑同一批没有新增记录。")
    assert tr.completion_review(ok, tr.analyze(ok, PROMPT))["ok"]

    asking = _trace_with_final(tmp_path, "ask", "去重逻辑已经写好，需要我继续补充失败重试吗？")
    review = tr.completion_review(asking, tr.analyze(asking, PROMPT))
    assert not review["ok"] and any("提问" in e for e in review["errors"])

    promising = _trace_with_final(tmp_path, "promise", "已定位问题。接下来我会修改入库逻辑。")
    assert not tr.completion_review(promising, tr.analyze(promising, PROMPT))["ok"]

    dangling = _trace_with_final(tmp_path, "dangling", "改动完成。", drop_result=True)
    review = tr.completion_review(dangling, tr.analyze(dangling, PROMPT))
    assert not review["ok"] and any("工具调用" in e for e in review["errors"])

    cut = _trace_with_final(tmp_path, "cut", "由于上下文限制，剩下的改动没有继续执行。")
    assert not tr.completion_review(cut, tr.analyze(cut, PROMPT))["ok"]

    empty = _trace_with_final(tmp_path, "empty", "")
    assert not tr.completion_review(empty, tr.analyze(empty, PROMPT))["ok"]

    good = _trace_with_final(tmp_path, "ok2", "合并规则已实现。")
    assert not tr.completion_review(good, tr.analyze(good, PROMPT), {"is_error": True, "subtype": "error"})["ok"]


def test_recording_plan_terminal_and_web(tmp_path):
    terminal = recorder.write_plan(tmp_path, "terminal")
    plan_data = json.loads(terminal.read_text(encoding="utf-8"))
    plan_data["startCommand"] = "python3 job.py"
    terminal.write_text(json.dumps(plan_data, ensure_ascii=False), encoding="utf-8")
    plan = recorder.check_plan(terminal)
    assert plan["ok"] and plan["mode"] == "terminal"
    assert recorder.check_plan(terminal)["ok"]

    web = recorder.write_plan(tmp_path, "web", force=True)
    web_data = json.loads(web.read_text(encoding="utf-8"))
    web_data["targetApps"] = ["Terminal"]
    web.write_text(json.dumps(web_data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(Solo2611Error, match="targetApps"):
        recorder.check_plan(web)

    web_data["targetApps"] = ["Terminal", "Chrome"]
    web_data["chromeUrl"] = "http://127.0.0.1:3000"
    web_data["pageCommand"] = "node scenario.mjs {chromeDebugPort}"
    web.write_text(json.dumps(web_data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(Solo2611Error, match="pageSteps"):
        recorder.check_plan(web)
    web_data["startCommand"] = "../scripts/start-demo.sh"
    web_data["pageSteps"] = ["登录后进入影响分析页", "发起分析并核对缺口与迁移清单结果"]
    web.write_text(json.dumps(web_data, ensure_ascii=False), encoding="utf-8")
    assert recorder.check_plan(web)["ok"]


def test_recording_privacy_terms_and_plan(tmp_path, monkeypatch):
    terms = {"登录账号": ["alice"], "家目录路径": ["/Users/alice"], "主机名": ["Pi", "alice-mbp"]}
    # 窗口标题、提示符、绝对路径三种泄露都要认出来，且返回的行不再带原文
    hits = recorder.find_sensitive(
        "alice — alice@Pi — ~ — -zsh — 80×24\nrootdir: /Users/alice/claude-runs/x\nssh ALICE-MBP", terms)
    assert [hit["kinds"] for hit in hits] == ["登录账号、主机名", "登录账号、家目录路径", "主机名"]
    assert all("alice" not in hit["line"].lower() for hit in hits)
    # 短主机名只认 user@host；普通单词、别的账号、干净终端的提示符不能误伤
    clean = "workspace $ pip install -q . && pytest -q\nAPI Pipeline ok\nmalice@example.com\nsolo-2611 录屏 — workspace — zsh -i"
    assert recorder.find_sensitive(clean, terms) == []

    monkeypatch.setattr(recorder, "sensitive_terms", lambda: terms)
    plan_path = recorder.write_plan(tmp_path, "terminal")
    plan_data = json.loads(plan_path.read_text(encoding="utf-8"))
    plan_data["startCommand"] = "python3 /Users/alice/claude-runs/x/workspace/job.py"
    plan_path.write_text(json.dumps(plan_data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(Solo2611Error, match="相对路径"):
        recorder.check_plan(plan_path)
    # 不进视频的准备命令可以用绝对路径
    plan_data["startCommand"] = "python3 job.py"
    plan_data["preflightCommands"] = ["rm -rf /Users/alice/claude-runs/x/workspace/out"]
    plan_path.write_text(json.dumps(plan_data, ensure_ascii=False), encoding="utf-8")
    assert recorder.check_plan(plan_path)["ok"]

    # 开录前：标题带账号的窗口直接拒绝，不提供跳过参数
    window = {"windowId": 7, "bundleId": recorder.TERMINAL_BUNDLE_ID, "width": 800, "height": 600,
              "title": "alice — alice@Pi — ~ — -zsh"}
    with pytest.raises(Solo2611Error, match="窗口标题"):
        recorder.start(tmp_path, window)
    # 停止时：录下来的内容带家目录路径，片段作废且不转码
    window["title"] = recorder.CLEAN_TERMINAL_TITLE
    monkeypatch.setattr(recorder, "terminal_text", lambda _id: "workspace $ pwd\n/Users/alice/claude-runs/x/workspace")
    raw = tmp_path / "recording" / "raw.mov"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(b"x")
    recorder.write_json(recorder._meta_path(tmp_path), {"status": "recording", "pid": 0, "raw": str(raw), "window": window})
    with pytest.raises(Solo2611Error, match="已作废"):
        recorder.stop(tmp_path)
    assert not raw.exists() and not (tmp_path / "recording" / recorder.VIDEO_NAME).exists()
    assert recorder.read_json(recorder._meta_path(tmp_path))["status"] == "failed"


def test_terminal_cleanup_removes_session_and_records_evidence(tmp_path, monkeypatch):
    session_dir = Path(tempfile.mkdtemp(prefix="solo2611-rec-", dir="/tmp"))
    (tmp_path / "monitor").mkdir()
    (tmp_path / "monitor" / "record-terminal.json").write_text(
        json.dumps({"windowId": 9876, "sessionDir": str(session_dir)}),
        encoding="utf-8",
    )
    monkeypatch.setattr(recorder, "_terminal_window_exists", lambda _window_id: False)
    report = recorder.close_terminal_session(tmp_path)
    assert report["status"] == "removed"
    assert report["windowExistsAfter"] is False
    assert report["sessionExistsAfter"] is False
    assert not session_dir.exists()
    assert (tmp_path / "monitor" / "terminal-cleanup.json").is_file()


def test_terminal_cleanup_waits_for_async_window_disappearance(tmp_path, monkeypatch):
    session_dir = Path(tempfile.mkdtemp(prefix="solo2611-rec-", dir="/tmp"))
    (tmp_path / "monitor").mkdir()
    (tmp_path / "monitor" / "record-terminal.json").write_text(
        json.dumps({"windowId": 9876, "sessionDir": str(session_dir)}),
        encoding="utf-8",
    )
    snapshots = iter([True, True, False])
    monkeypatch.setattr(recorder, "_terminal_window_exists", lambda _window_id: next(snapshots, False))
    monkeypatch.setattr(recorder, "_close_terminal", lambda *_args: None)
    monkeypatch.setattr(recorder.time, "sleep", lambda _seconds: None)
    report = recorder.close_terminal_session(tmp_path)
    assert report["status"] == "removed"
    assert report["windowExistsAfter"] is False
    assert report["sessionExistsAfter"] is False


def test_recorder_liveness_reaps_finished_child():
    child = subprocess.Popen(["/usr/bin/true"])
    time.sleep(0.1)
    assert recorder._alive(child.pid) is False
    assert child.poll() == 0


def test_feedback_check(tmp_path):
    p = tmp_path / "feedback.md"
    p.write_text(fb.TEMPLATE, encoding="utf-8")
    r = fb.check(p)
    assert not r["ok"]
    p.write_text(fb.TEMPLATE.replace("（填 1–5 的整数）", "5").replace("过程问题：", "过程问题：一次性完美跑通"), encoding="utf-8")
    r = fb.check(p)
    assert any("照抄" in e for e in r["errors"])

    p.write_text(
        fb.TEMPLATE.replace("（填 1–5 的整数）", "5").replace(
            "过程问题：",
            "过程问题：第 2 步运行批次后回读订单数仍为 240 行，重复执行造成新增。",
        ).replace(
            "产物问题：",
            "产物问题：src/dedupe.ts 没有去重，接口回读与源数据不一致，导致对账失败。",
        ),
        encoding="utf-8",
    )
    r = fb.check(p)
    assert not r["ok"] and any("5 分" in e for e in r["errors"])

    concise = (
        fb.TEMPLATE.replace("（填 1–5 的整数）", "4")
        .replace(
            "过程问题：",
            "过程问题：第一次比较来源时间和目标表时间时发现解析有细微偏差，修正后，"
            "会员和门店单品两组迟到补传都得到了预期结果。测试覆盖了迟到行保留、旧版本忽略、重跑幂等，"
            "以及读取数等于写入、忽略和拒绝之和的对账，入库结果也与业务预期一致。",
        )
        .replace(
            "产物问题：",
            "产物问题：逐项核对并运行后，门店补传批次只写入了符合最新版本的记录，"
            "其余因为早已入库或版本更旧被忽略，目标表保留了较新的商品记录，读取、写入和忽略数能够对上。"
                "问题是补传导入功能每次增量运行仍从头扫描整批源数据，没有只处理补传的对象，源数据越多耗时越长。",
        )
    )
    p.write_text(concise, encoding="utf-8")
    result = fb.check(p)
    assert result["ok"] and not result["warnings"]

    necessary_code = concise.replace(
        "问题是每次增量运行", "问题是 `incremental_scan` 每次增量运行"
    )
    p.write_text(necessary_code, encoding="utf-8")
    result = fb.check(p)
    assert result["ok"] and not result["warnings"], "必要时保留一两处代码信息不提醒"

    with_code_and_numbers = concise.replace(
        "测试覆盖了", "test_late_merge.py 和 test_replay.py 覆盖了"
    ).replace(
        "门店补传批次只写入了符合最新版本的记录", "门店补传批次 batch_0912 共 203 条，只写入了 2 条，忽略 201 条"
    )
    p.write_text(with_code_and_numbers, encoding="utf-8")
    result = fb.check(p)
    assert result["ok"]
    assert any("代码、文件名或字段名" in item for item in result["warnings"])
    assert any("具体数值" in item for item in result["warnings"])

    plain_negative = (
        fb.TEMPLATE.replace("（填 1–5 的整数）", "2")
        .replace(
            "过程问题：",
            "过程问题：设计回填规则时，只要新数据改动了已经入库的事件，就判定结果对不上，"
            "所以需求里的更正和撤销都会让任务失败。更正一笔金额后，处理进度停在原处，重跑多少次都无法推进。"
            "入库这一步从没实际运行过，服务流程的测试写好后又被删掉，只验证了计算部分就报告完成。",
        )
        .replace(
            "产物问题：",
            "产物问题：已有样本可以按创建时的特征定义重新计算，之后修改定义再重跑，结果也不会变；"
            "迟到的新事件能够补进来，受影响的样本也会列出来。重复的数据在入库时被直接跳过，"
            "报告里却没有任何提示，读者看不出有记录被忽略了。"
            "回填页面上同时显示上一批结果一致、本批结果对不上，影响列表也没有更新，"
            "无法判断现在的结果能不能用。",
        )
    )
    p.write_text(plain_negative, encoding="utf-8")
    result = fb.check(p)
    assert result["ok"] and not result["warnings"], result

    p.write_text(plain_negative.replace("按创建时的特征定义", "按创建时的口径").replace("需求里的", "题面里的"), encoding="utf-8")
    result = fb.check(p)
    assert result["ok"] and any("行话" in item and "口径" in item and "题面" in item for item in result["warnings"])

    functional = (
        fb.TEMPLATE.replace("（填 1–5 的整数）", "5")
        .replace(
            "过程问题：",
            "过程问题：先围绕字段删除、重算失败、同批次重试和旧数据升级逐项核对，"
            "再走真实接口回读与页面操作，接口、页面与数据库检查通过。"
            "验证覆盖失败批次保留上一生效版本、列变化记录和同批重复行被拒绝；"
            "新批次切换成功后，报告、规则建议和执行报告均使用同一生效版本。",
        )
        .replace(
            "产物问题：",
            "产物问题：成功批次完成时能统一切换可见版本，失败时也只清理该批次产生的半成品；"
            "页面能清楚展示当前生效批次、最近处理状态和列结构变化。"
            "移走源文件让批次失败后，原有的列结构没有被替换，恢复后再次重算可成功切换；"
            "同一批次重试前后行数不变，没有新增记录。",
        )
    )
    p.write_text(functional, encoding="utf-8")
    result = fb.check(p)
    assert result["ok"] and not result["warnings"]

    unresolved_tone = (
        functional
        .replace("成功批次完成时能统一切换可见版本", "成功批次完成时统一切换可见版本")
        .replace("页面能清楚展示", "页面清楚展示")
        .replace("可成功切换", "成功切换")
    )
    p.write_text(unresolved_tone, encoding="utf-8")
    result = fb.check(p)
    assert any("确定表达" in item for item in result["errors"])

    leaked = concise.replace(
        "过程问题：",
        "过程问题：录屏里可以看到终端中的模型改用临时容器继续处理，",
    )
    p.write_text(leaked, encoding="utf-8")
    result = fb.check(p)
    assert any("运行流程信息" in item for item in result["errors"])

    with_remark = concise + "\n## 备注\n\n环境说明\n"
    p.write_text(with_remark, encoding="utf-8")
    result = fb.check(p)
    assert any("备注必须留空" in item for item in result["errors"])

    generic_test_count = concise.replace(
        "会员和门店单品两组迟到补传都得到了预期结果",
        "16/16 通过",
    )
    p.write_text(generic_test_count, encoding="utf-8")
    result = fb.check(p)
    assert result["ok"] and any("总套件通过数" in item for item in result["warnings"])

    too_long = concise.replace(
        "产物问题：",
        "产物问题：" + "逐项核对后仍能复现同一个数据问题，" * 20,
    )
    p.write_text(too_long, encoding="utf-8")
    assert any("字上限" in item for item in fb.check(p)["errors"])

    with_run_id = concise.replace(
        "产物问题：",
        "产物问题：运行 12345678-1234-1234-1234-123456789abc 后，"
        "src/dedupe.ts 没有按主键去重，导致重跑后的数据翻倍。",
    )
    p.write_text(with_run_id, encoding="utf-8")
    assert any("完整运行 ID" in item for item in fb.check(p)["errors"])


def test_score_four_requires_a_named_residual_gap(tmp_path):
    p = tmp_path / "feedback.md"
    p.write_text(
        fb.TEMPLATE.replace("（填 1–5 的整数）", "4")
        .replace(
            "过程问题：",
            "过程问题：逐项运行并核对后，迟到记录补入、重复执行和断点续跑均得到预期结果，"
            "目标表与批次统计能够对上。",
        )
        .replace(
            "产物问题：",
            "产物问题：迟到记录能够写入，部分成功时只重试未完成记录，"
            "页面能够展示逐条写入、跳过和待处理状态。",
        ),
        encoding="utf-8",
    )
    result = fb.check(p)
    assert any("没有写明" in item and "残余缺口" in item for item in result["errors"])


def test_full_score_review_gate_requires_strict_evidence(tmp_path):
    root = tmp_path / "task"
    review_path = fb.ensure_full_score_review_template(root)
    template = json.loads(review_path.read_text(encoding="utf-8"))
    assert all(value is False for value in template["checklist"].values())
    errors, summary = fb.validate_full_score_review(root, 5)
    assert summary["status"] == "failed"
    assert any("explicitRequirements" in item for item in errors)

    runtime_path = root / "monitor" / "runtime-verification.json"
    runtime_path.write_text(
        json.dumps(
            {
                "runs": [
                    {"index": 1, "exitCode": 0, "label": "成功路径"},
                    {"index": 2, "exitCode": 0, "label": "边界与回读"},
                ]
            }
        ),
        encoding="utf-8",
    )
    review = fb.full_score_review_template()
    review["checklist"] = {key: True for key in fb.FULL_SCORE_CHECK_KEYS}
    review["explicitRequirements"] = [
        {
            "requirement": f"需求{i}",
            "verified": True,
            "evidence": f"独立运行结果逐项覆盖需求编号{i}",
        }
        for i in range(1, 4)
    ]
    review["edgeCases"] = [
        {"case": "重复运行", "verified": True, "evidence": "重复运行后目标表行数保持不变"},
        {"case": "部分成功", "verified": True, "evidence": "未完成记录被保留并在下一批重新处理"},
    ]
    review["runtimeEvidence"] = [
        {"runIndex": 1, "covers": ["success"], "observation": "成功路径真实退出"},
        {
            "runIndex": 2,
            "covers": ["negative", "regression", "cross-layer"],
            "observation": "边界、回归和页面回读一致",
        },
    ]
    review["knownGaps"] = []
    review["maxScoreJustification"] = (
        "所有明示需求、关键边界、失败恢复、重复运行和跨层回读均有真实执行证据，且没有已知缺口。"
    )
    review["reviewedBy"] = "reviewer"
    review_path.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
    errors, summary = fb.validate_full_score_review(root, 5)
    assert errors == []
    assert summary["status"] == "passed"
    assert summary["runtimeRuns"] == [1, 2]

    review["knownGaps"] = ["大批次分页仍未覆盖"]
    review["checklist"]["noKnownGaps"] = False
    review_path.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
    errors, summary = fb.validate_full_score_review(root, 5)
    assert summary["status"] == "failed"
    assert any("knownGaps" in item or "noKnownGaps" in item for item in errors)


def _run(*argv: str) -> int:
    return cli.main(list(argv))


def _set_account_domain(domain: str) -> None:
    config = load_config()
    config["submission"]["lockedDomain"] = domain
    save_config(config)


def test_init_accepts_all_data_engineering_subdirections(tmp_path):
    _set_account_domain("数据工程")
    for index, sub_direction in enumerate(DOMAINS["数据工程"]):
        source = tmp_path / f"source-{index}"
        source.mkdir()
        (source / "README.md").write_text("# fixture\n", encoding="utf-8")
        tasks = tmp_path / f"tasks-{index}"
        assert _run(
            "init",
            "--task-name",
            f"data-{index}",
            "--source",
            str(source),
            "--tasks-dir",
            str(tasks),
            "--domain",
            "数据工程",
            "--sub-direction",
            sub_direction,
        ) == 0
        root = next(tasks.iterdir())
        state = load_state(root)
        assert state["fields"]["所属领域"] == "数据工程"
        assert state["fields"]["子方向"] == sub_direction


def test_init_rejects_cross_domain_or_unmatched_subdirection(tmp_path):
    _set_account_domain("数据工程")
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("# fixture\n", encoding="utf-8")
    assert _run(
        "init",
        "--task-name",
        "cross-domain",
        "--source",
        str(source),
        "--tasks-dir",
        str(tmp_path / "cross"),
        "--domain",
        "安全",
        "--sub-direction",
        "系统安全",
    ) == 1
    assert _run(
        "init",
        "--task-name",
        "wrong-subdirection",
        "--source",
        str(source),
        "--tasks-dir",
        str(tmp_path / "wrong"),
        "--domain",
        "数据工程",
        "--sub-direction",
        "图形渲染",
    ) == 1


def test_end_to_end(tmp_path, capsys):
    _set_account_domain("数据工程")
    src = tmp_path / "viewer"
    (src / "src").mkdir(parents=True)
    (src / "src" / "ingest.ts").write_text("export const append = true;\n")
    (src / "src" / "reconcile.ts").write_text("export const reconcile = true;\n")
    (src / ".env").write_text("SECRET=1\n")  # 未跟踪的 .env 会被 .gitignore 挡住
    tasks = tmp_path / "tasks"
    assert _run(
        "init",
        "--task-name",
        "csm",
        "--source",
        str(src),
        "--tasks-dir",
        str(tasks),
        "--domain",
        "数据工程",
        "--sub-direction",
        "数据分析与数据应用",
    ) == 0
    root = next(tasks.iterdir())
    state = load_state(root)
    ws = Path(state["workspace"])
    assert len(state["initialSha"]) == 40
    assert state["fields"]["所属领域"] == "数据工程"
    assert state["fields"]["子方向"] == "数据分析与数据应用"
    assert ".env" not in subprocess.run(["git", "-C", str(ws), "ls-files"], capture_output=True, text=True).stdout

    assert _run("prompt", "--task-root", str(root), "--init") == 0
    assert (root / "monitor" / "prompt" / "difficulty-review.json").is_file()
    assert (root / "monitor" / "prompt" / "ra-renhua-review.json").is_file()
    pf = tmp_path / "p.md"
    pf.write_text(PROMPT, encoding="utf-8")
    _fill_prompt_evidence(root, PROMPT)
    assert _run("prompt", "--task-root", str(root), "--file", str(pf), "--task-type", "功能开发", "--difficulty", "困难",
                "--lang", "TypeScript, PostgreSQL", "--sub-direction", "数据分析与数据应用") == 0
    assert load_state(root)["fields"]["子方向"] == "数据分析与数据应用"
    with pytest.raises(SystemExit):  # 同一提示词在另一题里会被查重拦下
        _run("init", "--task-name", "dup", "--source", str(src), "--tasks-dir", str(tasks))
        other = sorted(tasks.iterdir())[-1]
        _run("prompt", "--task-root", str(other), "--file", str(pf), "--task-type", "功能开发", "--difficulty", "困难",
             "--lang", "TS")

    # 模拟一次被接受的 print 运行：模型提交了一次，又留下未提交改动
    (ws / "src" / "ingest.ts").write_text("export const append = false;\n")
    subprocess.run(["git", "-C", str(ws), "-c", "user.name=m", "-c", "user.email=m@m", "commit", "-qam", "model"], check=True)
    (ws / "src" / "dedupe.ts").write_text("export const dedupe = true;\n")
    attempt_dir = root / "monitor" / "runs" / "attempt-01"
    _write_trace(attempt_dir / "traces", "s-e2e", PROMPT)
    state = load_state(root)
    attempt = {"index": 1, "container": "x", "mode": "print", "status": "running", "dir": str(attempt_dir)}
    state["attempts"].append(attempt)
    info = tr.analyze(tr.main_trace_files(attempt_dir / "traces")[0], PROMPT) | {"traces": str(attempt_dir / "traces")}
    cli._accept_attempt(root, state, attempt, info)
    state = load_state(root)
    art = state["artifact"]
    assert len(art["modelCommits"]) == 1 and art["sha"] != art["modelCommits"][0]
    assert subprocess.run(["git", "-C", str(ws), "merge-base", "--is-ancestor", state["initialSha"], art["sha"]]).returncode == 0
    assert (root / "delivery" / "轨迹时间线.md").read_text().count("grep -rn shadow src") == 1

    # 导出前缺录屏和 feedback
    with pytest.raises(SystemExit):
        _run("export", "--task-root", str(root))

    if shutil.which("ffmpeg"):
        mov = tmp_path / "rec.mov"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=1440x900:rate=30", "-t", "3", str(mov)],
                       check=True)
        assert _run("record", "import", "--task-root", str(root), "--file", str(mov)) == 0
    else:
        pytest.skip("缺少 ffmpeg")
        capture = json.loads((root / "recording" / "运行录屏-window-capture.json").read_text(encoding="utf-8"))
        guard = json.loads((root / "recording" / "运行录屏-cursor-guard.json").read_text(encoding="utf-8"))
        assert capture["captureKind"] == "manual-import" and capture["status"] == "manual-import"
        assert guard["pointerStrategy"] == "none" and guard["status"] == "manual-import"

    assert _run("feedback", "--task-root", str(root), "--init") == 0
    fpath = root / "delivery" / "feedback.md"
    text = fpath.read_text().replace("（填 1–5 的整数）", "3").replace(
        "过程问题：", "过程问题：第 1 步只查看了 src/ingest.ts，没有读取批次水位，结束前没有运行批次。").replace(
        "产物问题：", "产物问题：src/dedupe.ts 没有按订单主键去重，重复执行后订单从120行变成240行，直接导致对账失败。")
    fpath.write_text(text)
    # 没有真实执行记录时，光看结束标志和自述不能通过
    with pytest.raises(SystemExit):
        _run("feedback", "--task-root", str(root), "--check")

    check_script = ws / "checks" / "verify.py"
    check_script.parent.mkdir()
    check_script.write_text(
        "print('重复执行后订单 120 行，第二次运行被拒绝')\n", encoding="utf-8",
    )
    assert _run("verify", "--task-root", str(root), "--label", "重跑同一批", "--",
                sys.executable, "checks/verify.py") == 0
    with pytest.raises(SystemExit):  # 反馈里写的 240 行、报错原文在真实输出里没有
        _run("feedback", "--task-root", str(root), "--check")

    fpath.write_text(
        text.replace("订单从120行变成240行，直接导致对账失败", "第二次运行时订单仍为 120 行，重复数据被直接跳过")
    )
    assert _run("feedback", "--task-root", str(root), "--check") == 0
    with pytest.raises(SystemExit):  # 快照没推到远端，不能导出可提交的表
        _run("export", "--task-root", str(root))

    # 用本地裸仓库代替 GitHub，验证快进推送和 permalink 拼接
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    state = load_state(root)
    gh.push_sha(ws, str(bare), state["initialSha"], "main", None)
    gh.push_sha(ws, str(bare), state["artifact"]["sha"], "main", None)
    remote_head = subprocess.run(["git", "-C", str(bare), "rev-parse", "main"], capture_output=True, text=True).stdout.strip()
    assert remote_head == state["artifact"]["sha"]
    state["remote"] = {"fullName": "t/csm-abcd", "web": "https://github.com/t/csm-abcd", "url": str(bare),
                       "initialPushed": "x", "artifactSha": state["artifact"]["sha"]}
    from common import save_state
    save_state(root, state)
    assert _run("export", "--task-root", str(root)) == 0
    rows = excel.read_back(root / "delivery" / "交付表.xlsx")
    assert len(rows) == 1
    row = rows[0]
    assert list(row) == SUBMIT_COLUMNS
    assert row["SessionID"] == "s-e2e" and row["TurnID/PromptID"] == "p-123" and row["交付完整性"] == 3
    assert row["User Prompt"] == PROMPT and row["Harness 版本"] == "2.1.303"
    assert row["初始环境快照"] == f"https://github.com/t/csm-abcd/commit/{state['initialSha']}"
    assert row["产物快照"].endswith(state["artifact"]["sha"]) and row["轨迹文件"].startswith("/")
    assert row["运行录屏"].startswith("/") and row["运行录屏"].endswith(".mp4")
    assert "第 1 步只查看了 src/ingest.ts" in row["Feedback"]
    assert row["备注"] in (None, "")
    assert _run("export-batch", "--tasks-dir", str(tasks), "--out", str(tmp_path / "batch.xlsx")) == 0
    assert len(excel.read_back(tmp_path / "batch.xlsx")) == 1

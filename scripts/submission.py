"""SOLO2 submission for one completed solo-2611 task."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests
from openpyxl import load_workbook

import feedback as fb
import trace as tr
from common import Solo2611Error, load_state, read_json, save_state, sha256_file, utc_now, write_json
from spec import DOMAINS

DEFAULT_SERVER = "https://solo2.jzxhnh.com"
MONITOR_CONFIG_URL = "http://127.0.0.1:8099/api/system/config"
MONITOR_SESSION_URL = "http://127.0.0.1:8099/api/system/solo2/session"
TRACE_MAX_BYTES = 27 * 1024 * 1024
VIDEO_MAX_BYTES = 500 * 1024 * 1024
GITHUB_COMMIT_RE = re.compile(r"^https://github\.com/[^/\s]+/[^/\s]+/commit/[0-9a-fA-F]{40}/?$")
ACTIVE_STATUSES = {"SUBMITTED", "QC_WAITING", "QC_RUNNING", "PENDING", "WAITING"}
PASS_STATUSES = {"QC_PASSED", "PASSED"}
FIX_STATUSES = {"PENDING_FIX"}
FAILED_STATUSES = {"QC_REJECTED", "QC_DISCARDED", "QC_FAILED", "DISCARDED", "REJECTED", "INVALID", "FAILED"}
EXCLUDED_SUBMISSION_FIELDS = {"other_issues"}
EXCLUDED_SUBMISSION_LABELS = {"备注"}
PLAINTEXT_API_KEY_REMARK = tr.PLAINTEXT_API_KEY_REMARK


def _origin(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise Solo2611Error(f"SOLO2 地址无效: {value!r}")
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")


def _workbench_platform(*, server_hint: str = "") -> dict[str, str]:
    """Load the authoritative platform identity and one fresh session.

    The skill must never read the system keychain, reuse browser cookies or
    trust a cached login. The workbench owns the configured username, platform
    URL and password; it returns a fresh session only after the platform reports
    the same account name.
    """
    try:
        config_response = requests.get(MONITOR_CONFIG_URL, timeout=20)
        config_response.raise_for_status()
        monitor = config_response.json()
    except (requests.RequestException, ValueError) as exc:
        raise Solo2611Error(f"读取本地工作台配置失败: {exc}") from exc
    solo2 = monitor.get("solo2") if isinstance(monitor.get("solo2"), dict) else {}
    username = str(solo2.get("username") or "").strip()
    if not username:
        raise Solo2611Error("本地工作台未配置 SOLO2 账号，禁止回退到缓存会话或 Keychain")
    if str(solo2.get("password") or "") != "<set>":
        raise Solo2611Error("本地工作台未保存 SOLO2 密码；请在 /system 中配置后重试")
    configured_server = _origin(str(solo2.get("base_url") or ""))
    if server_hint and _origin(server_hint) != configured_server:
        raise Solo2611Error(
            f"提交地址必须来自工作台：工作台为 {configured_server}，当前调用为 {_origin(server_hint)}"
        )
    try:
        session_response = requests.post(
            MONITOR_SESSION_URL,
            json={"expected_account": username, "expected_server": configured_server},
            timeout=30,
        )
        session_response.raise_for_status()
        session = session_response.json()
    except (requests.RequestException, ValueError) as exc:
        text = ""
        if isinstance(exc, requests.HTTPError) and exc.response is not None:
            text = exc.response.text[:600]
        raise Solo2611Error(f"从本地工作台获取 SOLO2 会话失败: {text or exc}") from exc
    if session.get("ok") is not True:
        raise Solo2611Error(f"工作台拒绝建立 SOLO2 会话: {session.get('message') or '未知错误'}")
    account = str(session.get("account") or "").strip()
    if account != username:
        raise Solo2611Error(f"SOLO2 身份校验失败：工作台配置 {username}，平台返回 {account or '空'}")
    server = _origin(str(session.get("server") or ""))
    if server != configured_server:
        raise Solo2611Error(
            f"SOLO2 平台地址不一致：工作台配置 {configured_server}，会话返回 {server}"
        )
    cookie = str(session.get("cookie") or "").strip()
    csrf = str(session.get("csrf") or "").strip()
    if not cookie or not csrf:
        raise Solo2611Error("工作台返回的 SOLO2 会话缺少 Cookie 或 CSRF")
    return {
        "username": username,
        "account": account,
        "server": server,
        "cookie": cookie,
        "csrf": csrf,
    }


class ApiClient:
    def __init__(self, server: str = ""):
        self.server = ""
        self.cookie = ""
        self.csrf = ""
        self.username = ""
        self._authenticate_from_workbench(server)

    def _authenticate_from_workbench(self, server_hint: str = "") -> None:
        platform = _workbench_platform(server_hint=server_hint)
        self.server = platform["server"]
        self.cookie = platform["cookie"]
        self.csrf = platform["csrf"]
        self.username = platform["account"]

    def _headers(self, *, json_body: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Cookie": self.cookie,
            "Origin": self.server,
            "Referer": self.server + "/app/submit",
            "User-Agent": "solo-2611-submit/1.0",
            "X-CSRF-Token": self.csrf,
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        timeout: int = 120,
    ) -> dict[str, Any]:
        url = self.server + path
        for attempt in range(2):
            if not self.cookie or not self.csrf:
                self._login_from_monitor()
            try:
                response = requests.request(
                    method,
                    url,
                    headers=self._headers(json_body=payload is not None and files is None),
                    json=payload,
                    files=files,
                    data=data,
                    timeout=timeout,
                )
            except requests.RequestException as exc:
                raise Solo2611Error(f"{method} {url} 网络失败: {exc}") from exc
            if response.status_code == 401 and attempt == 0:
                self._authenticate_from_workbench(self.server)
                continue
            if response.status_code >= 400:
                raise Solo2611Error(f"{method} {url} 返回 HTTP {response.status_code}: {response.text[:2000]}")
            try:
                value = response.json()
            except ValueError as exc:
                raise Solo2611Error(f"{method} {url} 未返回 JSON: {response.text[:1000]}") from exc
            if not isinstance(value, dict):
                raise Solo2611Error(f"{method} {url} 返回不是 JSON 对象")
            return value
        raise Solo2611Error("SOLO2 认证刷新后仍未通过")

    def upload(self, path: Path, kind: str) -> dict[str, Any]:
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with path.open("rb") as stream:
            response = self.request(
                "POST",
                "/api/v1/submissions/upload",
                files={"file": (path.name, stream, content_type)},
                data={"kind": kind},
                timeout=1800,
            )
        value = response.get("data") if isinstance(response.get("data"), dict) else response
        if kind == "video":
            url = str(value.get("url") or value.get("path") or response.get("url") or "").strip()
            if not url:
                raise Solo2611Error("视频上传响应缺少 url")
            return {"url": url, "size": value.get("size") or response.get("size")}
        if not str(value.get("path") or "").strip():
            raise Solo2611Error("轨迹上传响应缺少 path")
        return {
            "name": str(value.get("name") or path.name),
            "path": str(value.get("path")),
            "size": int(value.get("size") or path.stat().st_size),
        }


def _safe_json(path: Path) -> dict[str, Any]:
    return read_json(path, {}) or {}


def _read_delivery_row(task_root: Path) -> dict[str, Any]:
    path = task_root / "delivery" / "交付表.xlsx"
    if not path.is_file():
        raise Solo2611Error(f"缺少交付表: {path}")
    workbook = load_workbook(path, data_only=True, read_only=True)
    if "提交表" not in workbook.sheetnames:
        raise Solo2611Error("交付表缺少「提交表」工作表")
    rows = list(workbook["提交表"].iter_rows(values_only=True))
    if len(rows) < 3 or rows[1] is None:
        raise Solo2611Error("交付表缺少第二行官方字段头或数据行")
    headers = [str(item or "").strip() for item in rows[1]]
    data_rows = [row for row in rows[2:] if any(value not in (None, "") for value in row)]
    if len(data_rows) != 1:
        raise Solo2611Error(f"solo-2611 必须恰好有一个提交行，当前为 {len(data_rows)}")
    return dict(zip(headers, data_rows[0]))


def _coerce_value(field: dict[str, Any], value: Any) -> Any:
    field_type = str(field.get("field_type") or "")
    if field_type == "number":
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise Solo2611Error(f"{field.get('label')} 必须是整数") from exc
    return "" if value is None else str(value).strip()


def _is_remark_field(field: dict[str, Any]) -> bool:
    key = str(field.get("field_key") or "").strip()
    label = str(field.get("label") or "").strip()
    return key in EXCLUDED_SUBMISSION_FIELDS or label in EXCLUDED_SUBMISSION_LABELS


def _is_excluded_submission_field(field: dict[str, Any], *, include_remark: bool = False) -> bool:
    return _is_remark_field(field) and not include_remark


def _sanitize_submission_data(data: dict[str, Any], *, include_remark: bool = False) -> dict[str, Any]:
    if include_remark:
        return dict(data)
    return {key: value for key, value in data.items() if key not in EXCLUDED_SUBMISSION_FIELDS}


def _validate_field(field: dict[str, Any], value: Any, data: dict[str, Any]) -> None:
    label = str(field.get("label") or field.get("field_key"))
    required = bool(field.get("is_required"))
    if required and value in (None, "", []):
        raise Solo2611Error(f"提交字段为空: {label}")
    if not value:
        return
    options = [str(item) for item in field.get("options") or []]
    if options and str(value) not in options:
        raise Solo2611Error(f"{label} 不在平台选项中: {value}")
    validation = field.get("validation") or {}
    pattern = validation.get("pattern")
    if pattern and not re.fullmatch(str(pattern), str(value)):
        raise Solo2611Error(f"{label} 格式不符合平台要求")
    if field.get("field_type") == "number":
        minimum = validation.get("min")
        maximum = validation.get("max")
        if minimum is not None and int(value) < int(minimum):
            raise Solo2611Error(f"{label} 小于平台下限 {minimum}")
        if maximum is not None and int(value) > int(maximum):
            raise Solo2611Error(f"{label} 超过平台上限 {maximum}")
    options_by = validation.get("options_by") or {}
    if options_by:
        parent_key = str(options_by.get("field") or "")
        allowed = ((options_by.get("map") or {}).get(str(data.get(parent_key))) or [])
        if allowed and str(value) not in allowed:
            raise Solo2611Error(f"{label} 与父字段取值不匹配")


def _probe_video(path: Path) -> dict[str, Any]:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,codec_name:format=duration,size",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        raise Solo2611Error(f"录屏无法读取: {proc.stderr[:300]}")
    payload = json.loads(proc.stdout)
    stream = (payload.get("streams") or [{}])[0]
    fmt = payload.get("format") or {}
    if (int(stream.get("width") or 0), int(stream.get("height") or 0)) != (1280, 720):
        raise Solo2611Error("录屏不是 1280x720")
    duration = float(fmt.get("duration") or 0)
    if duration <= 0 or duration > 90:
        raise Solo2611Error(f"录屏时长异常: {duration}")
    return {"width": 1280, "height": 720, "duration": round(duration, 2), "size": int(fmt.get("size") or path.stat().st_size)}


def _find_existing(client: ApiClient, session_id: str, turn_id: str) -> dict[str, Any] | None:
    page = 1
    while True:
        result = client.request("GET", f"/api/v1/submissions?page={page}&page_size=100")
        items = result.get("items") or []
        for item in items:
            if str(item.get("session_id") or "") == session_id or str(item.get("turn_id") or "") == turn_id:
                return item
        meta = result.get("meta") or {}
        total = int(meta.get("total") or 0)
        if not items or page * 100 >= total:
            return None
        page += 1


def _domain_lock_from_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Infer the account's locked domain from the earliest submitted record."""
    rows = [item for item in items if str(item.get("domain") or "").strip()]
    if not rows:
        return {
            "domain": "",
            "source": "unknown",
            "submissionCount": len(items),
            "firstSubmissionId": None,
        }
    ordered = sorted(
        rows,
        key=lambda item: (
            str(item.get("submitted_at") or item.get("submitted_date") or item.get("created_at") or ""),
            int(item.get("id") or 0),
        ),
    )
    first = ordered[0]
    return {
        "domain": str(first.get("domain") or "").strip(),
        "source": "submission_history",
        "submissionCount": len(items),
        "firstSubmissionId": first.get("id"),
    }


def _load_account_domain_lock(client: ApiClient) -> dict[str, Any]:
    page = 1
    items: list[dict[str, Any]] = []
    while True:
        result = client.request("GET", f"/api/v1/submissions?page={page}&page_size=100")
        batch = result.get("items") or []
        items.extend(item for item in batch if isinstance(item, dict))
        meta = result.get("meta") or {}
        total = int(meta.get("total") or 0)
        if not batch or page * 100 >= total:
            break
        page += 1
    return _domain_lock_from_items(items)


def _enforce_domain_lock(
    *,
    submitted_domain: str,
    configured_domain: str,
    history_lock: dict[str, Any],
) -> dict[str, Any]:
    """以配置大类为唯一授权，并用提交历史交叉核验。"""
    configured = configured_domain.strip()
    if not configured:
        raise Solo2611Error(
            "配置缺少 submission.lockedDomain；账号大类必须来自账号配置，"
            "缺失时停止并联系管理员，不能改用历史记录或题目标签自行推断"
        )
    if configured and configured not in DOMAINS:
        raise Solo2611Error(
            f"submission.lockedDomain「{configured}」不是合法领域；可选：{'、'.join(DOMAINS)}"
        )
    historical = str(history_lock.get("domain") or "").strip()
    if configured and historical and configured != historical:
        raise Solo2611Error(
            f"本地锁定领域「{configured}」与账号历史首条提交领域「{historical}」不一致；"
            "先核实账号领域后再提交"
        )
    locked = configured
    if submitted_domain and locked and submitted_domain != locked:
        raise Solo2611Error(
            f"账号锁定领域为「{locked}」，当前分类为「{submitted_domain}」。"
            f"只能在「{locked}」下选择真实匹配的子方向；无法匹配时拒绝提交，"
            "不能为单题修改账号大类"
        )
    return {
        **history_lock,
        "configuredDomain": configured or None,
        "effectiveDomain": locked or None,
    }


def preflight(
    task_root: Path,
    *,
    server: str = DEFAULT_SERVER,
    locked_domain: str = "",
) -> dict[str, Any]:
    state = load_state(task_root)
    session = state.get("session") or {}
    artifact = state.get("artifact") or {}
    prompt_path = Path(str(state.get("promptPath") or ""))
    if not prompt_path.is_file():
        raise Solo2611Error("提示词文件不存在")
    prompt = prompt_path.read_text(encoding="utf-8").strip()
    trace_path = Path(str(session.get("trace") or ""))
    if not trace_path.is_file():
        raise Solo2611Error("轨迹文件不存在")
    trace_info = tr.analyze(trace_path, prompt)
    if not trace_info.get("ok"):
        raise Solo2611Error("轨迹结构校验未通过: " + "；".join(trace_info.get("errors") or []))
    trace_api_key_risk = tr.detect_plaintext_api_key_risk(trace_path)
    video_path = Path(str((state.get("recording") or {}).get("video") or ""))
    if not video_path.is_file():
        raise Solo2611Error("录屏文件不存在")
    if trace_path.suffix.lower() != ".jsonl" or trace_path.stat().st_size > TRACE_MAX_BYTES:
        raise Solo2611Error("轨迹必须是 27MB 以内的 .jsonl")
    if video_path.suffix.lower() not in {".mp4", ".mov", ".webm", ".m4v"} or video_path.stat().st_size > VIDEO_MAX_BYTES:
        raise Solo2611Error("录屏格式或大小不符合平台要求")
    video_info = _probe_video(video_path)
    feedback_path = task_root / "delivery" / "feedback.md"
    feedback_result = fb.check(feedback_path)
    if not feedback_result.get("ok"):
        raise Solo2611Error("Feedback 检查未通过: " + "；".join(feedback_result.get("errors") or []))
    row = _read_delivery_row(task_root)
    client = ApiClient(server)
    schema = client.request("GET", "/api/v1/submissions/form-schema")
    fields = [
        item
        for item in schema.get("fields") or []
        if item.get("is_enabled", True)
        and not _is_excluded_submission_field(item, include_remark=trace_api_key_risk["risk"])
    ]
    if trace_api_key_risk["risk"] and not any(_is_remark_field(item) for item in fields):
        raise Solo2611Error("轨迹存在明文 API Key 风险，但平台表单没有可用的「备注」字段，禁止提交")
    data: dict[str, Any] = {}
    file_values: dict[str, Path] = {}
    for field in fields:
        key = str(field.get("field_key") or "")
        label = str(field.get("label") or "")
        if key == "trace_file":
            file_values[key] = trace_path
            continue
        if key == "screencast":
            file_values[key] = video_path
            continue
        if _is_remark_field(field):
            value = trace_api_key_risk["remark"]
        else:
            value = prompt if key == "user_prompt" else row.get(label)
        value = _coerce_value(field, value)
        _validate_field(field, value, data)
        data[key] = value
    if str(data.get("user_prompt") or "").strip() != prompt:
        raise Solo2611Error("交付表 User Prompt 与提示词原文不一致")
    history_lock = _load_account_domain_lock(client)
    domain_lock = _enforce_domain_lock(
        submitted_domain=str(data.get("domain") or "").strip(),
        configured_domain=locked_domain,
        history_lock=history_lock,
    )
    existing = _find_existing(client, str(data.get("session_id") or ""), str(data.get("turn_id") or ""))
    result = {
        "schemaVersion": 1,
        "ok": True,
        "server": client.server,
        "account": client.username,
        "schemaFingerprint": schema.get("fingerprint"),
        "fieldCount": len(fields),
        "existingSubmission": existing,
        "trace": {"path": str(trace_path), "size": trace_path.stat().st_size, "sha256": sha256_file(trace_path)},
        "plaintextApiKeyRisk": trace_api_key_risk,
        "video": {"path": str(video_path), **video_info, "sha256": sha256_file(video_path)},
        "accountDomainLock": domain_lock,
        "scoreDelivery": int(data.get("score_delivery") or 0),
        "sessionId": data.get("session_id"),
        "turnId": data.get("turn_id"),
        "checkedAt": utc_now(),
    }
    out_dir = task_root / "monitor" / "submission"
    write_json(out_dir / "preflight.json", result, mode=0o600)
    data = _sanitize_submission_data(data, include_remark=trace_api_key_risk["risk"])
    write_json(
        out_dir / "payload-preview.json",
        {
            "data": data,
            "fileFields": {k: str(v) for k, v in file_values.items()},
            "plaintextApiKeyRisk": trace_api_key_risk,
        },
        mode=0o600,
    )
    state["submission"] = {"status": "preflight_ok" if not existing else "already_submitted", "preflight": str(out_dir / "preflight.json"), "checkedAt": utc_now()}
    save_state(task_root, state)
    return result


def _extract_submission_id(value: Any) -> Any:
    if isinstance(value, dict):
        if value.get("id") is not None and any(
            key in value for key in ("session_id", "turn_id", "status", "status_label", "current_version")
        ):
            return value.get("id")
        if value.get("submission_id") is not None:
            return value.get("submission_id")
        for key in ("data", "item", "submission", "result"):
            found = _extract_submission_id(value.get(key))
            if found is not None:
                return found
        for child in value.values():
            if isinstance(child, (dict, list)):
                found = _extract_submission_id(child)
                if found is not None:
                    return found
    elif isinstance(value, list):
        for child in value:
            found = _extract_submission_id(child)
            if found is not None:
                return found
    return None


def _save_result(task_root: Path, result: dict[str, Any]) -> None:
    path = task_root / "monitor" / "submission" / "api-result.json"
    write_json(path, result, mode=0o600)
    state = load_state(task_root)
    state["submission"] = {
        "status": result.get("status"),
        "submissionId": result.get("submissionId"),
        "statusLabel": result.get("statusLabel"),
        "qcConclusion": result.get("qcConclusion"),
        "qcSummary": result.get("qcSummary"),
        "result": str(path),
        "updatedAt": utc_now(),
    }
    save_state(task_root, state)


def _payload_from_preview(task_root: Path) -> tuple[dict[str, Any], dict[str, Path], str]:
    payload = _safe_json(task_root / "monitor" / "submission" / "payload-preview.json")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    data = _sanitize_submission_data(data)
    files = {
        key: Path(str(value))
        for key, value in (payload.get("fileFields") or {}).items()
    }
    schema = ApiClient(_safe_json(task_root / "monitor" / "submission" / "preflight.json").get("server") or DEFAULT_SERVER).request(
        "GET", "/api/v1/submissions/form-schema"
    )
    return data, files, str(schema.get("fingerprint") or "")


def submit_task(
    task_root: Path,
    *,
    execute: bool,
    watch: bool,
    server: str = DEFAULT_SERVER,
    poll_interval: float = 300,
    poll_timeout: float = 86400,
    locked_domain: str = "",
) -> dict[str, Any]:
    prepared = preflight(task_root, server=server, locked_domain=locked_domain)
    existing = prepared.get("existingSubmission")
    if existing:
        status = str(existing.get("status") or "").upper()
        result = {
            "ok": status in PASS_STATUSES,
            "status": status.lower() if status else "already_submitted",
            "statusLabel": existing.get("status_label") or existing.get("status"),
            "submissionId": existing.get("id"),
            "qcConclusion": existing.get("qc_conclusion"),
            "qcSummary": existing.get("qc_summary"),
            "currentVersion": existing.get("current_version"),
            "editable": existing.get("editable"),
            "lockedFields": existing.get("locked_fields") or [],
            "repairRequired": status in FIX_STATUSES,
            "plaintextApiKeyRisk": prepared.get("plaintextApiKeyRisk"),
            "existing": existing,
        }
        _save_result(task_root, result)
        return result
    data, files, fingerprint = _payload_from_preview(task_root)
    trace_api_key_risk = tr.detect_plaintext_api_key_risk(files["trace_file"])
    if trace_api_key_risk["risk"]:
        data["other_issues"] = trace_api_key_risk["remark"]
    else:
        data.pop("other_issues", None)
    if not execute:
        result = {
            "ok": True,
            "status": "dry_run",
            "schemaFingerprint": fingerprint,
            "fields": sorted(data),
            "files": {key: {"path": str(path), "size": path.stat().st_size} for key, path in files.items()},
            "plaintextApiKeyRisk": trace_api_key_risk,
            "preflight": str(task_root / "monitor" / "submission" / "preflight.json"),
        }
        _save_result(task_root, result)
        return result
    client = ApiClient(server)
    uploaded_trace = client.upload(files["trace_file"], "trace")
    uploaded_video = client.upload(files["screencast"], "video")
    data["trace_file"] = [uploaded_trace]
    data["screencast"] = uploaded_video["url"]
    submitted = client.request(
        "POST",
        "/api/v1/submissions",
        payload={"data": data, "schema_fingerprint": fingerprint},
        timeout=180,
    )
    submission_id = _extract_submission_id(submitted)
    result = {
        "ok": submission_id is not None,
        "status": "submitted",
        "submissionId": submission_id,
        "statusLabel": submitted.get("status_label") or submitted.get("status"),
        "response": submitted,
        "uploadedTrace": uploaded_trace,
        "uploadedVideo": uploaded_video,
        "submittedAt": utc_now(),
    }
    _save_result(task_root, result)
    if not watch:
        return result
    if submission_id is None:
        raise Solo2611Error("提交成功响应里没有 submission id，无法轮询")
    watched = poll_submission(
        task_root,
        submission_id,
        server=server,
        interval=poll_interval,
        timeout=poll_timeout,
    )
    watched["submitResponse"] = result
    _save_result(task_root, watched)
    return watched


def poll_submission(
    task_root: Path,
    submission_id: Any,
    *,
    server: str = DEFAULT_SERVER,
    interval: float = 300,
    timeout: float = 86400,
) -> dict[str, Any]:
    client = ApiClient(server)
    deadline = time.monotonic() + timeout
    history: list[dict[str, Any]] = []
    while True:
        detail = client.request("GET", f"/api/v1/submissions/{submission_id}")
        status = str(detail.get("status") or "").upper()
        history.append({"status": status, "checkedAt": utc_now()})
        write_json(
            task_root / "monitor" / "submission" / "qc-history.json",
            {"submissionId": submission_id, "history": history, "latest": detail},
            mode=0o600,
        )
        base = {
            "submissionId": submission_id,
            "status": status.lower() if status else "unknown",
            "statusLabel": detail.get("status_label"),
            "qcConclusion": detail.get("qc_conclusion"),
            "qcSummary": detail.get("qc_summary"),
            "currentVersion": detail.get("current_version"),
            "editable": detail.get("editable"),
            "lockedFields": detail.get("locked_fields") or [],
            "detail": detail,
        }
        if status in PASS_STATUSES or str(detail.get("qc_conclusion") or "").upper() in PASS_STATUSES:
            return {**base, "ok": True}
        if status in FIX_STATUSES:
            write_json(
                task_root / "monitor" / "submission" / "fix-request.json",
                base,
                mode=0o600,
            )
            return {**base, "ok": False, "repairRequired": True}
        if status in FAILED_STATUSES:
            return {**base, "ok": False}
        if time.monotonic() >= deadline:
            return {**base, "ok": False, "pollTimedOut": True}
        time.sleep(max(1.0, interval))


def repair_task(
    task_root: Path,
    submission_id: Any,
    *,
    comment: str,
    execute: bool,
    watch: bool,
    refresh_video: bool = False,
    server: str = DEFAULT_SERVER,
    poll_interval: float = 300,
    poll_timeout: float = 86400,
    locked_domain: str = "",
) -> dict[str, Any]:
    if not comment.strip():
        raise Solo2611Error("返修必须提供 --comment")
    client = ApiClient(server)
    detail = client.request("GET", f"/api/v1/submissions/{submission_id}")
    if str(detail.get("status") or "").upper() != "PENDING_FIX" or detail.get("editable") is False:
        raise Solo2611Error(f"提交 {submission_id} 不是可返修的 PENDING_FIX")
    _, files, fingerprint = _payload_from_preview(task_root)
    trace_api_key_risk = tr.detect_plaintext_api_key_risk(files["trace_file"])
    row = _read_delivery_row(task_root)
    schema = client.request("GET", "/api/v1/submissions/form-schema")
    current = detail.get("data") if isinstance(detail.get("data"), dict) else detail
    data: dict[str, Any] = {}
    locked = {str(item) for item in detail.get("locked_fields") or []}
    if trace_api_key_risk["risk"] and not any(
        item.get("is_enabled", True) and _is_remark_field(item)
        for item in schema.get("fields") or []
    ):
        raise Solo2611Error("轨迹存在明文 API Key 风险，但平台表单没有可用的「备注」字段，禁止返修提交")
    for field in schema.get("fields") or []:
        if not field.get("is_enabled", True):
            continue
        if _is_excluded_submission_field(field, include_remark=trace_api_key_risk["risk"]):
            continue
        key = str(field.get("field_key") or "")
        label = str(field.get("label") or "")
        if _is_remark_field(field):
            data[key] = trace_api_key_risk["remark"]
        elif key == "trace_file":
            data[key] = current.get(key) or detail.get("trace_file")
        elif key == "screencast":
            data[key] = current.get(key) or detail.get("screencast")
        elif key in locked and current.get(key) is not None:
            data[key] = current[key]
        else:
            value = _coerce_value(field, row.get(label))
            _validate_field(field, value, data)
            data[key] = value
    for key in ("trace_file", "screencast"):
        if not data.get(key):
            raise Solo2611Error(f"返修详情缺少锁定字段 {key}")
    domain_lock = _enforce_domain_lock(
        submitted_domain=str(data.get("domain") or "").strip(),
        configured_domain=locked_domain,
        history_lock=_load_account_domain_lock(client),
    )
    if refresh_video:
        uploaded = client.upload(files["screencast"], "video")
        data["screencast"] = uploaded["url"]
    data = _sanitize_submission_data(data, include_remark=trace_api_key_risk["risk"])
    preview = {
        "submissionId": submission_id,
        "schemaFingerprint": fingerprint,
        "accountDomainLock": domain_lock,
        "plaintextApiKeyRisk": trace_api_key_risk,
        "data": data,
        "comment": comment,
    }
    write_json(task_root / "monitor" / "submission" / "repair-payload.json", preview, mode=0o600)
    if not execute:
        return {"ok": True, "status": "repair_dry_run", "submissionId": submission_id, "preview": str(task_root / "monitor" / "submission" / "repair-payload.json")}
    response = client.request(
        "PUT",
        f"/api/v1/submissions/{submission_id}",
        payload=preview,
        timeout=180,
    )
    result = {"ok": True, "status": "fix_submitted", "submissionId": submission_id, "response": response}
    _save_result(task_root, result)
    if watch:
        watched = poll_submission(
            task_root,
            submission_id,
            server=server,
            interval=poll_interval,
            timeout=poll_timeout,
        )
        watched["repairResponse"] = response
        _save_result(task_root, watched)
        return watched
    return result

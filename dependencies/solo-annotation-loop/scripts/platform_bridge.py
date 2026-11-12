#!/usr/bin/env python3
"""Bridge a Solo Manager source package into solo-annotation-loop.

Subcommands:
  select         Select an available Solo Manager project, download its package,
                 create the platform task, and initialize PROJECT_DIR.
  bootstrap      Expand a downloaded local task package and initialize PROJECT_DIR.
  check-prompt   Check character similarity and validate the semantic review.
  prompt-semantic-init  Create the per-round semantic review scaffold.
  submit         Publish Git state, append delivery Excel, upload platform bundle,
                 then submit the same round to solo2 through solo-push.

Credentials:
  SOLO_MANAGER_TOKEN, macOS Keychain service `solo-manager-token`, or
  ~/.codex/solo-manager.json with a `token` field.
"""
from __future__ import annotations

import argparse
import base64
import collections
import datetime as dt


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
import difflib
import fcntl
import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parents[1]
SKILL_SCRIPTS = SKILL_DIR / "scripts"
if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))
from high_score_policy import (  # noqa: E402
    SOLO2_MAX_AVERAGE,
    score_average,
    validate_high_score_review,
)
SOLO_PUSH_SCRIPTS = Path("/Users/gaobo/.codex/skills/solo-push/scripts")
GITHUB_CONTEXT = Path.home() / ".codex" / "pg-code" / "github-context.json"
MANAGER_CONFIG = Path.home() / ".codex" / "solo-manager.json"
KEYCHAIN_SERVICE = "solo-manager-token"
# 平台地址统一走这里；可用 SOLO_MANAGER_BASE_URL 覆盖。
DEFAULT_BASE_URL = os.environ.get("SOLO_MANAGER_BASE_URL", "http://192.168.31.26:8080").rstrip("/")
SOLO2_SERVER = "https://solo2.jzxhnh.com"
SOLO2_KEYCHAIN_SERVICE = "solo2-jzxhnh"
SOLO2_HISTORY_MAX_ITEMS = 500
SEMANTIC_DECISIONS = {"OK", "DUPLICATE", "REVIEW_REQUIRED"}
PROMPT_CACHE_SCHEMA_VERSION = 1
PROMPT_CACHE_ROOT = Path(os.environ.get(
    "SOLO2_PROMPT_CACHE_ROOT",
    Path.home() / ".codex" / "solo2-auto" / "prompt-cache",
))
# 平台下发的任务包元数据会把服务端自己的地址写成 localhost，这里识别需要改写的占位地址。
LOCAL_BASE_URL_RE = re.compile(r"^https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0)(?::\d+)?", re.I)
PLATFORM_CLAIM_ROOT = Path(os.environ.get(
    "SOLO2_PLATFORM_CLAIM_ROOT",
    Path.home() / ".codex" / "solo2-auto" / "locks" / "platform-claims",
))

TASK_SLUGS = {
    "Bug修复": "Bug修复",
    "0-1代码生成": "代码生成",
    "feature迭代": "功能迭代",
    "代码理解": "代码理解",
    "代码重构": "代码重构",
    "工程化": "工程化",
    "代码测试": "代码测试",
}
TASK_ALIASES = {
    "代码生成": "0-1代码生成",
    "0-1": "0-1代码生成",
    "开发": "0-1代码生成",
    "功能迭代": "feature迭代",
}
ALLOWED_DIFFICULTIES = {"困难", "地狱"}
MEDIUM_DIFFICULTY_MIGRATION = {"中等": "困难"}
DIFFICULTY_DEFINITIONS = {
    "困难": "需要整合多个模块或系统约束，做关键设计取舍，并处理复杂状态、兼容性、权限、并发、性能或异常链路。",
    "地狱": "需要极高决策复杂度。需求通常开放或约束隐蔽，依赖深度项目理解、架构级判断、多步复杂调试验证，或大量边界场景；即使 SOTA 模型也大概率失败。",
}
DIFFICULTY_DESIGN_CHECKLIST = [
    "困难题必须至少落实一项真实复杂度：多模块或系统约束、关键设计取舍、复杂状态、兼容性、权限、并发、性能或异常链路。",
    "只增加测试、验收或文字长度，不能把中等题升级为困难。",
    "普通单点 Bug、常规 CRUD、简单页面调整不能标为困难或地狱。",
    "地狱题必须说明开放或隐蔽约束、架构级决策、多步复杂调试验证或大量边界场景，并确认仍可客观验证。",
    "地狱题不能靠缺信息、不可复现环境、含糊验收或故意模糊需求制造失败。",
]

DELIVERY_HEADERS = [
    "User Prompt", "SessionID", "TurnID/PromptID", "轮次", "初始环境快照", "提交地址", "轨迹文件",
    "环境可复现等级", "Harness", "Harness 版本", "操作系统", "任务类型", "任务难度", "语言/框架",
    "交付完整性", "交付完整性 - 描述", "指令遵循", "指令遵循 - 描述", "任务规划", "任务规划 - 描述",
    "推理能力", "推理能力 - 描述", "执行能力", "执行能力 - 描述", "其他问题", "提交人", "提交时间", "父记录",
]


def run(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None,
        capture: bool = True, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env, text=True,
                            capture_output=capture)
    if check and result.returncode != 0:
        raise RuntimeError(f"命令失败: {' '.join(cmd)}\n{result.stderr or result.stdout}")
    return result


def read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_task_type(value: str) -> str:
    value = TASK_ALIASES.get(value.strip(), value.strip())
    if value not in TASK_SLUGS:
        raise RuntimeError(f"未知任务类型: {value}")
    return value


def normalize_required_difficulty(value: Any) -> tuple[str, str]:
    """Return the effective difficulty and any migrated source value."""
    raw = str(value or "").strip()
    if not raw:
        return "困难", ""
    if raw in MEDIUM_DIFFICULTY_MIGRATION:
        return MEDIUM_DIFFICULTY_MIGRATION[raw], raw
    return raw, ""


def default_round_difficulty(round_no: int) -> str:
    """All rounds use hard difficulty or above."""
    return "困难"


def ensure_required_difficulty(project_dir: Path, round_no: int) -> dict[str, Any]:
    """Migrate historical medium values and hard-block lower or unknown values."""
    paths = [
        ("round-score", project_dir / "rounds" / f"r{round_no:02d}" / "round-score.json"),
        ("round-locator", project_dir / "rounds" / f"r{round_no:02d}" / "round-locator.json"),
        ("state", project_dir / "state.json"),
    ]
    changes: list[dict[str, str]] = []
    for source, path in paths:
        if not path.is_file():
            continue
        data = read_json(path, {})
        if not isinstance(data, dict):
            continue
        raw = str(data.get("difficulty") or "").strip()
        effective, migrated_from = normalize_required_difficulty(raw)
        if raw and not migrated_from:
            continue
        data["difficulty"] = effective
        write_json(path, data)
        changes.append({
            "source": source,
            "path": str(path),
            "from": migrated_from,
            "to": effective,
        })

    score = read_json(paths[0][1], {})
    locator = read_json(paths[1][1], {})
    state = read_json(paths[2][1], {})
    source_difficulties: dict[str, str] = {}
    invalid_sources: list[str] = []
    for source, document in (
        ("round-score", score),
        ("round-locator", locator),
        ("state", state),
    ):
        if not isinstance(document, dict):
            continue
        raw = str(document.get("difficulty") or "").strip()
        if not raw:
            continue
        effective, _migrated_from = normalize_required_difficulty(raw)
        source_difficulties[source] = effective
        if effective not in ALLOWED_DIFFICULTIES:
            invalid_sources.append(f"{source}={effective}")

    values = sorted(set(source_difficulties.values()))
    inconsistent = len(values) > 1
    effective = values[0] if values else "困难"
    ok = not invalid_sources and not inconsistent
    if invalid_sources:
        message = (
            "任务难度存在不允许的值 "
            + "，".join(invalid_sources)
            + "，只允许困难或地狱"
        )
    elif inconsistent:
        message = "任务难度在评分、轨迹和状态之间不一致: " + "，".join(
            f"{source}={value}" for source, value in source_difficulties.items()
        )
    elif changes:
        message = "历史难度已迁移为 " + effective
    else:
        message = f"任务难度为 {effective}，符合要求"
    review = {
        "ok": ok,
        "status": (
            "difficulty_conflict"
            if not ok
            else "migrated"
            if changes
            else "ok"
            if source_difficulties
            else "defaulted"
        ),
        "allowedDifficulties": sorted(ALLOWED_DIFFICULTIES),
        "definitions": DIFFICULTY_DEFINITIONS,
        "designChecklist": DIFFICULTY_DESIGN_CHECKLIST,
        "effectiveDifficulty": effective,
        "sourceDifficulties": source_difficulties,
        "migratedFrom": changes[0]["from"] if changes else "",
        "migrationChanges": changes,
        "rule": "所有轮次任务难度必须为困难或地狱；历史中等迁移为困难，简单、未知值和不一致值阻断",
        "message": message,
    }
    if not ok:
        raise RuntimeError(
            f"SOLO2 提交难度审核未通过: {review['message']}；"
            "把评分、轨迹和状态里的任务难度统一为困难或地狱后重试。"
        )
    return review


def normalize_prompt(text: str) -> tuple[str, str]:
    normalized = unicodedata.normalize("NFKC", text).lower()
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized, sha256_bytes(normalized.encode("utf-8"))


def comparison_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).lower()
    return "".join(char for char in normalized if char.isalnum())


def ngrams(text: str, size: int) -> list[str]:
    if len(text) < size:
        return [text] if text else []
    return [text[index:index + size] for index in range(len(text) - size + 1)]


REVISION_FAMILY_PATTERN = re.compile(
    r"(修复|修正|排查|定位|解决|回填|补回|兼容|错误结果|异常|失败|不一致)"
)
REVISION_FAMILY_DICE_THRESHOLD = 0.42
REVISION_FAMILY_JACCARD_THRESHOLD = 0.34


def prompt_assessment(candidate: str, history: str) -> dict[str, Any]:
    candidate_exact, _ = normalize_prompt(candidate)
    history_exact, _ = normalize_prompt(history)
    if candidate_exact and candidate_exact == history_exact:
        return {"blocked": True, "decision": "EXACT", "reasons": ["规范化后提示词完全一致"],
                "similarity": 1.0, "longestCommonSubstringLength": len(candidate_exact),
                "matchedText": candidate, "dice": 1.0, "jaccard": 1.0,
                "revisionFamily": False}
    left, right = comparison_text(candidate), comparison_text(history)
    matcher = difflib.SequenceMatcher(None, left, right)
    match = matcher.find_longest_match(0, len(left), 0, len(right))
    longest = match.size
    matched_text = left[match.a:match.a + match.size]
    left_grams = collections.Counter(ngrams(left, 2))
    right_grams = collections.Counter(ngrams(right, 2))
    overlap = sum((left_grams & right_grams).values())
    total = sum(left_grams.values()) + sum(right_grams.values())
    dice = (2.0 * overlap / total) if total else 0.0
    left_four, right_four = set(ngrams(left, 4)), set(ngrams(right, 4))
    union = left_four | right_four
    jaccard = len(left_four & right_four) / len(union) if union else 0.0
    revision_family = bool(
        REVISION_FAMILY_PATTERN.search(candidate_exact)
        and REVISION_FAMILY_PATTERN.search(history_exact)
    )
    reasons: list[str] = []
    if longest >= 18:
        reasons.append(f"存在连续 {longest} 个有效字符重复")
    if dice >= 0.88:
        reasons.append(f"整体字符相似度为 {dice:.2f}，达到雷同阈值 0.88")
    if jaccard >= 0.82:
        reasons.append(f"四字片段重合度为 {jaccard:.2f}，达到雷同阈值 0.82")
    if revision_family and dice >= REVISION_FAMILY_DICE_THRESHOLD:
        reasons.append(
            f"两轮都含修订动作且整体字符相似度为 {dice:.2f}，达到同仓库缺陷族阈值 "
            f"{REVISION_FAMILY_DICE_THRESHOLD:.2f}；触发时机或边界变化不构成新主题"
        )
    if revision_family and jaccard >= REVISION_FAMILY_JACCARD_THRESHOLD:
        reasons.append(
            f"两轮都含修订动作且四字片段重合度为 {jaccard:.2f}，达到同仓库缺陷族阈值 "
            f"{REVISION_FAMILY_JACCARD_THRESHOLD:.2f}；触发时机或边界变化不构成新主题"
        )
    return {
        "blocked": bool(reasons),
        "decision": "SIMILAR" if reasons else "OK",
        "reasons": reasons,
        "similarity": max(dice, jaccard),
        "dice": round(dice, 4),
        "jaccard": round(jaccard, 4),
        "revisionFamily": revision_family,
        "longestCommonSubstringLength": longest,
        "matchedText": matched_text,
    }



def semantic_review_path(project_dir: Path, round_no: int) -> Path:
    return project_dir / "checks" / "semantic-dedup" / f"r{round_no:02d}.json"


def semantic_review_errors(
    review: Any,
    *,
    round_no: int,
    prompt_text: str,
    local_count: int,
    solo2_count: int,
) -> list[str]:
    if not isinstance(review, dict):
        return ["语义审核记录不是 JSON 对象"]
    errors: list[str] = []
    try:
        schema_version = int(review.get("schemaVersion") or 0)
    except (TypeError, ValueError):
        schema_version = 0
    if schema_version != 1:
        errors.append("semantic schemaVersion 必须为 1")
    try:
        review_round = int(review.get("roundNo") or 0)
    except (TypeError, ValueError):
        review_round = 0
    if review_round != round_no:
        errors.append(f"semantic roundNo 必须为 {round_no}")
    expected_hash = normalize_prompt(prompt_text)[1]
    if str(review.get("promptSha256") or "") != expected_hash:
        errors.append("semantic promptSha256 与当前提示词不一致，必须重新审核")
    decision = str(review.get("decision") or "").upper()
    if decision not in SEMANTIC_DECISIONS:
        errors.append("semantic decision 必须是 OK、DUPLICATE 或 REVIEW_REQUIRED")
    if not str(review.get("summary") or "").strip():
        errors.append("semantic summary 不能为空")
    counts = review.get("reviewedSourceCounts")
    if not isinstance(counts, dict):
        errors.append("semantic reviewedSourceCounts 必须是对象")
    else:
        for key, expected in (("local", local_count), ("solo2", solo2_count)):
            try:
                actual = int(counts.get(key))
            except (TypeError, ValueError):
                errors.append(f"semantic reviewedSourceCounts.{key} 必须是整数")
                continue
            if actual != expected:
                errors.append(
                    f"semantic reviewedSourceCounts.{key}={actual}，当前历史数量为 {expected}，必须重新审核"
                )
    matches = review.get("matches")
    if not isinstance(matches, list):
        errors.append("semantic matches 必须是数组")
        matches = []
    if decision == "OK" and matches:
        errors.append("semantic decision=OK 时 matches 必须为空")
    if decision == "DUPLICATE" and not matches:
        errors.append("semantic decision=DUPLICATE 时至少需要一条 matches")
    for index, match in enumerate(matches):
        label = f"semantic matches[{index}]"
        if not isinstance(match, dict):
            errors.append(f"{label} 必须是对象")
            continue
        if str(match.get("source") or "") not in {"local", "solo2"}:
            errors.append(f"{label}.source 必须是 local 或 solo2")
        if not str(match.get("reference") or "").strip():
            errors.append(f"{label}.reference 不能为空")
        overlap = match.get("overlap")
        if not isinstance(overlap, list) or not any(str(item).strip() for item in overlap):
            errors.append(f"{label}.overlap 必须是非空数组")
        if not str(match.get("reason") or "").strip():
            errors.append(f"{label}.reason 不能为空")
    return errors


def load_solo2_push_module() -> Any:
    scripts = str(SOLO_PUSH_SCRIPTS)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import push_delivery  # type: ignore

    return push_delivery


def history_match_keys(meta: dict[str, Any], project_code: str = "") -> list[str]:
    keys: set[str] = set()
    for value in (
        project_code,
        meta.get("projectCode"),
        meta.get("projectId"),
        meta.get("repoName"),
        meta.get("taskNo"),
    ):
        text = str(value or "").strip().casefold()
        if not text:
            continue
        keys.add(text)
        keys.add(Path(text).name)
    return sorted(keys, key=lambda item: (-len(item), item))


def repo_id_matches(repo_id: str, keys: list[str]) -> bool:
    value = str(repo_id or "").strip().casefold()
    if not value:
        return False
    for key in keys:
        pattern = r"(?<![a-z0-9])" + re.escape(key) + r"(?![0-9])"
        if re.search(pattern, value):
            return True
    return False


def prompt_cache_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def prompt_cache_global_path(keys: list[str]) -> Path:
    digest = sha256_bytes("\n".join(sorted(keys)).encode("utf-8"))[:24]
    return PROMPT_CACHE_ROOT / f"{digest}.json"


def prompt_cache_project_path(project_dir: Path) -> Path:
    return project_dir / "checks" / "platform" / "prompt-history-cache.json"


def make_prompt_cache_record(
    *,
    source: str,
    user_prompt: str,
    **metadata: Any,
) -> dict[str, Any]:
    normalized, digest = normalize_prompt(user_prompt)
    comparison = comparison_text(user_prompt)
    identity = (
        metadata.get("submissionId")
        or metadata.get("promptId")
        or metadata.get("historyFile")
        or digest
    )
    record = {
        "cacheId": f"{source}:{identity}",
        "source": source,
        "userPrompt": user_prompt,
        "promptSha256": digest,
        "normalizedText": normalized,
        "comparisonText": comparison,
        "terms4": sorted(set(ngrams(comparison, 4))),
        "cachedAt": prompt_cache_now(),
    }
    for key, value in metadata.items():
        if value not in (None, ""):
            record[key] = value
    return record


def build_prompt_cache_index(records: list[dict[str, Any]]) -> dict[str, Any]:
    dimensions = {
        "byProjectCode": "projectCode",
        "byRepoId": "repoId",
        "byTaskNo": "taskNo",
        "byRoundNo": "roundNo",
        "byTaskType": "taskType",
        "byPromptSha256": "promptSha256",
    }
    index: dict[str, Any] = {
        key: collections.defaultdict(set)
        for key in (*dimensions, "byKeyword")
    }
    for record in records:
        if not isinstance(record, dict):
            continue
        cache_id = str(record.get("cacheId") or "")
        if not cache_id:
            continue
        for index_key, field in dimensions.items():
            value = str(record.get(field) or "").strip()
            if value:
                index[index_key][value].add(cache_id)
        for term in record.get("terms4") or []:
            term_text = str(term).strip()
            if term_text:
                index["byKeyword"][term_text].add(cache_id)
    return {
        key: {name: sorted(values) for name, values in buckets.items()}
        for key, buckets in index.items()
    }


def read_prompt_cache_document(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        document = read_json(path, {})
    except (OSError, json.JSONDecodeError):
        return {}
    return document if isinstance(document, dict) else {}


def merge_prompt_cache_records(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for document in documents:
        for record in document.get("records") or []:
            if not isinstance(record, dict):
                continue
            cache_id = str(record.get("cacheId") or "").strip()
            if not cache_id:
                cache_id = f"{record.get('source') or 'unknown'}:{record.get('promptSha256') or ''}"
                record = {**record, "cacheId": cache_id}
            records[cache_id] = record
    return list(records.values())


def prompt_record_matches_project(record: dict[str, Any], keys: list[str]) -> bool:
    for field in ("repoId", "projectCode", "taskNo", "variantName"):
        if repo_id_matches(str(record.get(field) or ""), keys):
            return True
    return False


def load_prompt_cache_documents(project_dir: Path, keys: list[str]) -> list[dict[str, Any]]:
    paths: list[Path] = [prompt_cache_project_path(project_dir), prompt_cache_global_path(keys)]
    if PROMPT_CACHE_ROOT.is_dir():
        paths.extend(sorted(PROMPT_CACHE_ROOT.glob("*.json"))[:2000])
    seen: set[Path] = set()
    documents: list[dict[str, Any]] = []
    for path in paths:
        resolved = path.expanduser().resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        document = read_prompt_cache_document(resolved)
        if document:
            documents.append(document)
    return documents


def load_cached_prompt_history(project_dir: Path, keys: list[str]) -> dict[str, Any] | None:
    documents = load_prompt_cache_documents(project_dir, keys)
    matching = [
        record
        for document in documents
        for record in (document.get("records") or [])
        if isinstance(record, dict) and prompt_record_matches_project(record, keys)
    ]
    if not matching:
        return None
    remote_records = [record for record in matching if record.get("source") == "solo2"]
    selected_records = remote_records or matching
    by_hash: dict[str, dict[str, Any]] = {}
    for record in selected_records:
        prompt_text = str(record.get("userPrompt") or "").strip()
        digest = str(record.get("promptSha256") or "").strip()
        if not digest and prompt_text:
            digest = normalize_prompt(prompt_text)[1]
        identity = digest or str(record.get("cacheId") or "")
        existing = by_hash.get(identity)
        if existing is None or (
            existing.get("source") != "solo2" and record.get("source") == "solo2"
        ):
            by_hash[identity] = record
    records = sorted(
        by_hash.values(),
        key=lambda item: (
            str(item.get("repoId") or ""),
            int(item.get("roundNo") or 0),
            int(item.get("submissionId") or item.get("id") or 0),
        ),
    )
    prompts: list[dict[str, Any]] = []
    for record in records:
        prompt_text = str(record.get("userPrompt") or "").strip()
        prompts.append(
            {
                "id": record.get("submissionId") or record.get("id"),
                "repoId": record.get("repoId") or "",
                "sessionId": record.get("sessionId") or "",
                "promptId": record.get("promptId") or "",
                "roundNo": record.get("roundNo"),
                "taskType": record.get("taskType") or "",
                "status": record.get("status") or "cached",
                "submittedAt": record.get("submittedAt") or "",
                "userPrompt": prompt_text,
                "promptSha256": record.get("promptSha256") or "",
                "cacheSource": record.get("source") or "",
                "cachedAt": record.get("cachedAt") or "",
            }
        )
    missing_prompt_count = sum(1 for item in prompts if not item["userPrompt"])
    updated_values = [
        str(document.get("updatedAt") or "")
        for document in documents
        if document.get("updatedAt")
    ]
    source_counts = collections.Counter(
        str(record.get("source") or "unknown") for record in records
    )
    all_cache_ids = {
        str(record.get("cacheId") or "")
        for document in documents
        for record in (document.get("records") or [])
        if isinstance(record, dict) and record.get("cacheId")
    }
    project_path = prompt_cache_project_path(project_dir)
    global_path = prompt_cache_global_path(keys)
    return {
        "status": "incomplete" if missing_prompt_count else "ready",
        "server": next(
            (str(document.get("server") or "") for document in documents if document.get("server")),
            SOLO2_SERVER,
        ),
        "source": "local_cache",
        "fallbackReason": "",
        "projectMatchKeys": keys,
        "submissionTotal": len(prompts),
        "matchedSubmissionCount": len(prompts),
        "missingPromptCount": missing_prompt_count,
        "historyTruncated": False,
        "cacheStale": True,
        "cacheDocumentCount": len(documents),
        "cacheRecordCount": len(all_cache_ids),
        "sourceCounts": dict(source_counts),
        "cacheUpdatedAt": max(updated_values, default=""),
        "cachePath": str(project_path if project_path.is_file() else global_path),
        "prompts": prompts,
        "fetchedAt": prompt_cache_now(),
    }


def write_prompt_cache(
    project_dir: Path,
    keys: list[str],
    *,
    server: str,
    meta: dict[str, Any],
    local_entries: list[tuple[Path, str]],
    remote_prompts: list[dict[str, Any]],
) -> tuple[dict[str, Any], Path, Path]:
    history_index: dict[str, dict[str, Any]] = {}
    index_path = project_dir / "history_prompt" / "index.json"
    if index_path.is_file():
        try:
            raw_index = read_json(index_path, [])
        except (OSError, json.JSONDecodeError):
            raw_index = []
        if isinstance(raw_index, list):
            history_index = {
                str(item.get("fileName") or ""): item
                for item in raw_index
                if isinstance(item, dict) and item.get("fileName")
            }
    new_records: list[dict[str, Any]] = []
    for history_path, history_text in local_entries:
        if not history_text.strip():
            continue
        round_match = re.search(r"-r(\d{2})\.txt$", history_path.name)
        indexed = history_index.get(history_path.name) or {}
        new_records.append(
            make_prompt_cache_record(
                source="local",
                user_prompt=history_text,
                historyFile=str(history_path),
                repoId=str(meta.get("repoName") or ""),
                projectCode=str(meta.get("projectCode") or ""),
                taskNo=str(indexed.get("taskNo") or meta.get("taskNo") or ""),
                roundNo=indexed.get("roundNo")
                or (int(round_match.group(1)) if round_match else None),
                taskType=str(indexed.get("taskType") or ""),
                promptExcerpt=str(indexed.get("promptExcerpt") or ""),
            )
        )
    for item in remote_prompts:
        prompt_text = str(item.get("userPrompt") or "").strip()
        if not prompt_text:
            continue
        new_records.append(
            make_prompt_cache_record(
                source=str(item.get("cacheSource") or "solo2"),
                user_prompt=prompt_text,
                submissionId=item.get("id"),
                repoId=str(item.get("repoId") or ""),
                sessionId=str(item.get("sessionId") or ""),
                promptId=str(item.get("promptId") or ""),
                roundNo=item.get("roundNo"),
                taskType=str(item.get("taskType") or ""),
                status=str(item.get("status") or ""),
                submittedAt=str(item.get("submittedAt") or ""),
            )
        )
    project_path = prompt_cache_project_path(project_dir)
    global_path = prompt_cache_global_path(keys)
    existing = [
        read_prompt_cache_document(project_path),
        read_prompt_cache_document(global_path),
    ]
    records_by_id = {
        str(record.get("cacheId")): record
        for record in merge_prompt_cache_records(existing)
        if record.get("cacheId")
    }
    for record in new_records:
        records_by_id[str(record["cacheId"])] = record
    records = sorted(
        records_by_id.values(),
        key=lambda item: (
            str(item.get("source") or ""),
            str(item.get("repoId") or ""),
            int(item.get("roundNo") or 0),
            int(item.get("submissionId") or item.get("id") or 0),
            str(item.get("cacheId") or ""),
        ),
    )
    document = {
        "schemaVersion": PROMPT_CACHE_SCHEMA_VERSION,
        "updatedAt": prompt_cache_now(),
        "server": server,
        "projectMatchKeys": keys,
        "recordCount": len(records),
        "sourceCounts": dict(collections.Counter(
            str(record.get("source") or "unknown") for record in records
        )),
        "records": records,
        "index": build_prompt_cache_index(records),
    }
    write_json(project_path, document)
    write_json(global_path, document)
    return document, global_path, project_path


def fetch_solo2_prompt_history_remote(keys: list[str]) -> dict[str, Any]:
    push_delivery = load_solo2_push_module()
    credentials_args = argparse.Namespace(
        config="~/.codex/solo2-push.json",
        keychain_service=SOLO2_KEYCHAIN_SERVICE,
    )
    cookie, csrf = push_delivery.load_credentials(credentials_args)
    server = SOLO2_SERVER.rstrip("/")
    listed: list[dict[str, Any]] = []
    page = 1
    total = 0
    while page <= 100:
        result = push_delivery.request(
            f"{server}/api/v1/submissions?page={page}&page_size=100",
            "GET",
            cookie,
            csrf,
        )
        items = [item for item in (result.get("items") or []) if isinstance(item, dict)]
        total = max(total, int((result.get("meta") or {}).get("total") or 0))
        for item in items:
            if repo_id_matches(str(item.get("repo_id") or ""), keys):
                listed.append(item)
        if not items or page * 100 >= total:
            break
        page += 1
    listed.sort(
        key=lambda item: (
            str(item.get("repo_id") or ""),
            int(item.get("round_no") or 0),
            int(item.get("id") or 0),
        )
    )
    prompts: list[dict[str, Any]] = []
    for item in listed[:SOLO2_HISTORY_MAX_ITEMS]:
        detail = push_delivery.request(
            f"{server}/api/v1/submissions/{item['id']}",
            "GET",
            cookie,
            csrf,
        )
        prompt_text = str(detail.get("user_prompt") or "").strip()
        prompts.append(
            {
                "id": detail.get("id", item.get("id")),
                "repoId": detail.get("repo_id") or item.get("repo_id") or "",
                "sessionId": detail.get("session_id") or item.get("session_id") or "",
                "promptId": detail.get("turn_id") or item.get("turn_id") or "",
                "roundNo": detail.get("round_no") or item.get("round_no"),
                "taskType": detail.get("question_type") or item.get("question_type") or "",
                "status": detail.get("status") or item.get("status") or "",
                "submittedAt": detail.get("submitted_at") or item.get("submitted_at") or "",
                "userPrompt": prompt_text,
                "promptSha256": normalize_prompt(prompt_text)[1] if prompt_text else "",
            }
        )
    history_truncated = len(listed) > SOLO2_HISTORY_MAX_ITEMS
    missing_prompt_count = sum(1 for item in prompts if not item["userPrompt"])
    return {
        "status": "incomplete" if missing_prompt_count or history_truncated else "ready",
        "server": server,
        "source": "solo2",
        "fallbackReason": "",
        "projectMatchKeys": keys,
        "submissionTotal": total,
        "matchedSubmissionCount": len(prompts),
        "missingPromptCount": missing_prompt_count,
        "historyTruncated": history_truncated,
        "prompts": prompts,
        "fetchedAt": prompt_cache_now(),
    }


def fetch_solo2_prompt_history(
    project_dir: Path,
    meta: dict[str, Any],
    *,
    project_code: str = "",
) -> dict[str, Any]:
    keys = history_match_keys(meta, project_code)
    if not keys:
        raise RuntimeError("无法确定 SOLO2 项目匹配键，不能执行历史提示词去重")
    local_entries = local_history_entries(project_dir)
    remote_error: Exception | None = None
    try:
        result = fetch_solo2_prompt_history_remote(keys)
    except Exception as exc:
        remote_error = exc
        cached = load_cached_prompt_history(project_dir, keys)
        if cached is None:
            raise RuntimeError(
                f"SOLO2 历史获取失败，且本地没有可用的当前项目提示词缓存: {exc}"
            ) from exc
        cached["fallbackReason"] = f"{type(exc).__name__}: {exc}"
        try:
            _document, global_path, project_path = write_prompt_cache(
                project_dir,
                keys,
                server=str(cached.get("server") or SOLO2_SERVER),
                meta=meta,
                local_entries=local_entries,
                remote_prompts=list(cached.get("prompts") or []),
            )
            cached["cachePath"] = str(project_path)
            cached["globalCachePath"] = str(global_path)
        except Exception as cache_exc:
            cached["cacheError"] = str(cache_exc)
        result = cached
    else:
        try:
            _document, global_path, project_path = write_prompt_cache(
                project_dir,
                keys,
                server=str(result.get("server") or SOLO2_SERVER),
                meta=meta,
                local_entries=local_entries,
                remote_prompts=list(result.get("prompts") or []),
            )
        except Exception as exc:
            raise RuntimeError(f"SOLO2 历史获取成功，但本地提示词缓存写入失败: {exc}") from exc
        result["cachePath"] = str(project_path)
        result["globalCachePath"] = str(global_path)

    result["cacheStale"] = bool(remote_error is not None)
    result["fetchedAt"] = prompt_cache_now()
    history_path = project_dir / "checks" / "platform" / "solo2-prompt-history.json"
    result["historyPath"] = str(history_path)
    write_json(history_path, result)
    return result


def solo2_history(args: argparse.Namespace) -> int:
    project_dir = args.project_dir.expanduser().resolve()
    meta = platform_meta(project_dir)
    result = fetch_solo2_prompt_history(
        project_dir,
        meta,
        project_code=str(args.project_code or "").strip(),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def load_manager_token() -> str:
    token = os.environ.get("SOLO_MANAGER_TOKEN", "").strip()
    if token:
        return token
    account = os.environ.get("USER", "")
    proc = subprocess.run(
        ["security", "find-generic-password", "-a", account, "-s", KEYCHAIN_SERVICE, "-w"],
        text=True, capture_output=True,
    )
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    if MANAGER_CONFIG.is_file():
        token = str(read_json(MANAGER_CONFIG, {}).get("token", "")).strip()
        if token:
            return token
    raise RuntimeError(
        "缺少 Solo Manager 访问令牌。请设置 SOLO_MANAGER_TOKEN，或执行 "
        f"security add-generic-password -a \"$USER\" -s {KEYCHAIN_SERVICE} -U -w"
    )


def api_json(meta: dict[str, Any], path: str, method: str = "GET",
             payload: dict[str, Any] | None = None, token: str | None = None) -> dict[str, Any]:
    base = str(meta.get("apiBaseUrl") or meta["baseUrl"].rstrip("/") + "/api/v1").rstrip("/")
    url = base + path
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            try:
                raw = response.read()
            except http.client.IncompleteRead as exc:
                raw = exc.partial
            return json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except http.client.IncompleteRead as incomplete:
            raw = incomplete.partial
        body = raw.decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} 返回 HTTP {exc.code}: {body[:2000]}") from exc


def put_presigned_upload(upload_url: str, meta: dict[str, Any], payload: bytes,
                         headers: dict[str, Any] | None = None) -> int:
    """Upload to a presigned URL without breaking a server-side loopback Host.

    MinIO signs the Host header. Connecting to a local port-forward while
    preserving the original Host keeps the signature valid when the manager
    serializes the upload URL with localhost from its own network namespace.
    """
    parsed = urllib.parse.urlsplit(upload_url)
    loopback = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
    manager = urllib.parse.urlsplit(str(meta.get("baseUrl") or ""))
    manager_host = manager.hostname or ""
    if ((parsed.hostname or "").lower() in loopback
            and manager_host
            and manager_host.lower() not in loopback):
        connection_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        conn = connection_cls(manager_host, parsed.port or (443 if parsed.scheme == "https" else 80), timeout=300)
        path = parsed.path + (("?" + parsed.query) if parsed.query else "")
        conn.putrequest("PUT", path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", parsed.netloc)
        for key, value in (headers or {}).items():
            conn.putheader(str(key), str(value))
        conn.putheader("Content-Length", str(len(payload)))
        conn.endheaders()
        conn.send(payload)
        response = conn.getresponse()
        response.read()
        return response.status
    request = urllib.request.Request(upload_url, data=payload, method="PUT")
    for key, value in (headers or {}).items():
        request.add_header(str(key), str(value))
    with urllib.request.urlopen(request, timeout=300) as response:
        return response.status


def api_download(meta: dict[str, Any], path: str, destination: Path, token: str) -> None:
    base = str(meta.get("apiBaseUrl") or meta["baseUrl"].rstrip("/") + "/api/v1").rstrip("/")
    url = base + path
    request = urllib.request.Request(url, method="GET")
    request.add_header("Accept", "application/zip")
    request.add_header("Authorization", "Bearer " + token)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(request, timeout=300) as response, destination.open("wb") as output:
            shutil.copyfileobj(response, output)
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except http.client.IncompleteRead as incomplete:
            raw = incomplete.partial
        body = raw.decode("utf-8", errors="replace")
        raise RuntimeError(f"GET {url} 返回 HTTP {exc.code}: {body[:2000]}") from exc


def parse_last_json(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    candidates: list[tuple[int, dict[str, Any]]] = []
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            candidates.append((index + end, value))
    if not candidates:
        raise RuntimeError(f"命令没有返回 JSON:\n{text[-4000:]}")
    return max(candidates, key=lambda item: item[0])[1]


def project_eligibility(project: dict[str, Any], task_type: str) -> tuple[dict[str, Any] | None, list[str]]:
    reasons: list[str] = []
    if project.get("disabled"):
        reasons.append("项目已禁用")
    if str(project.get("readinessStatus") or "").upper() != "RUNNABLE":
        reasons.append("项目不是 RUNNABLE")
    quota = next((item for item in project.get("quotas") or [] if item.get("taskType") == task_type), None)
    if not quota or int(quota.get("remaining") or 0) <= 0:
        reasons.append(f"任务类型 {task_type} 剩余配额为 0")
    variants = [
        variant for variant in project.get("variants") or []
        if variant.get("sourceAvailable") and variant.get("sourceAsset")
    ]
    if not variants:
        reasons.append("没有带源码快照的可用变体")
        return None, reasons
    variants.sort(key=lambda item: str(item.get("directoryName") or item.get("id") or ""))
    if reasons:
        return None, reasons
    return variants[0], []


CONTAINER_NAME_PREFIX = "claude-"
CONTAINER_TIMESTAMP_RE = re.compile(r"-\d{8,}$")


def container_name_project_code(name: str) -> str:
    """从 `claude-<项目编号>-<时间戳>` 容器名解析项目编号。"""
    if not name.startswith(CONTAINER_NAME_PREFIX):
        return ""
    body = CONTAINER_TIMESTAMP_RE.sub("", name[len(CONTAINER_NAME_PREFIX):])
    return body.strip().strip("-")


def local_active_project_codes(workdir: Path | None) -> set[str]:
    """docker 无法判定时的兜底：作业目录里记录着未清理容器的项目按「可能仍在执行」处理。"""
    codes: set[str] = set()
    if not workdir or not workdir.is_dir():
        return codes
    for state_path in sorted(list(workdir.glob("*/auto/state.json")) + list(workdir.glob("*/state.json"))):
        auto = read_json(state_path, {}) or {}
        container = auto.get("container") or {}
        if not isinstance(container, dict) or not container:
            continue
        if str(container.get("removedAt") or "").strip():
            continue
        status = str(container.get("status") or "").strip().casefold()
        if status in {"", "removed"}:
            continue
        code = container_name_project_code(str(container.get("name") or ""))
        if not code:
            code = str(auto.get("projectCode") or auto.get("projectId") or "").strip()
        if code:
            codes.add(code.casefold())
    return codes


def running_container_project_codes(workdir: Path | None = None) -> tuple[set[str], str]:
    """返回 (正在容器中执行的项目编号集合, 判定来源)。"""
    proc = None
    try:
        proc = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"], text=True, capture_output=True,
        )
    except (OSError, subprocess.SubprocessError):
        proc = None
    if proc is not None and proc.returncode == 0:
        codes = {container_name_project_code(name) for name in proc.stdout.split()}
        return {code.casefold() for code in codes if code}, "docker ps"
    return local_active_project_codes(workdir), "本地状态兜底"


def claimed_project_codes(base_url: str) -> set[str]:
    """Return project codes whose cross-process claim lock is currently held."""
    base_digest = hashlib.sha256(base_url.rstrip("/").encode("utf-8")).hexdigest()[:16]
    claim_dir = PLATFORM_CLAIM_ROOT / base_digest
    if not claim_dir.is_dir():
        return set()
    codes: set[str] = set()
    for metadata_path in claim_dir.glob("*.json"):
        metadata = read_json(metadata_path, {})
        if not isinstance(metadata, dict):
            continue
        code = str(metadata.get("projectCode") or "").strip()
        lock_path = Path(str(metadata.get("lockPath") or ""))
        if not code or not lock_path.is_file():
            continue
        try:
            with lock_path.open("a+", encoding="utf-8") as handle:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except BlockingIOError:
                    codes.add(code.casefold())
        except OSError:
            continue
    return codes


def list_projects(meta: dict[str, Any], token: str, path: str, project_code: str = "",
                  keyword: str = "", domain: str = "", category: str = "",
                  username: str = "") -> list[dict[str, Any]]:
    projects: list[dict[str, Any]] = []
    page = 1
    while page <= 50:
        params: dict[str, Any] = {"page": page, "size": 100}
        if keyword or project_code:
            params["keyword"] = project_code or keyword
        if domain:
            params["domain"] = domain
        if category:
            params["category"] = category
        if username:
            params["username"] = username
        response = api_json(meta, path + "?" + urllib.parse.urlencode(params), token=token)
        items = response.get("items") or response.get("content") or []
        projects.extend(item for item in items if isinstance(item, dict))
        total_pages = int(response.get("totalPages") or page)
        if page >= total_pages or not items:
            break
        page += 1
    return projects


def current_project_quota(meta: dict[str, Any], token: str, project_id: str,
                          task_type: str) -> dict[str, Any] | None:
    """Fresh quota for one project, used to record the effect of a deduction."""
    try:
        project = api_json(meta, "/projects/" + urllib.parse.quote(str(project_id), safe=""), token=token)
    except Exception:
        return None
    for quota in project.get("quotas") or []:
        if isinstance(quota, dict) and quota.get("taskType") == task_type:
            return quota
    return None


def find_available_project(meta: dict[str, Any], token: str, task_type: str,
                           project_code: str = "", project_id: str = "",
                           keyword: str = "", domain: str = "", category: str = "",
                           workdir: Path | None = None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """按「我的项目 → 项目池」顺序选项目，容器中正在执行的项目一律跳过。"""
    running_codes, running_source = running_container_project_codes(workdir)
    claimed_codes = claimed_project_codes(str(meta.get("baseUrl") or DEFAULT_BASE_URL))
    if claimed_codes:
        running_codes |= claimed_codes
        running_source = f"{running_source} + 项目占用锁"
    running_skipped: list[str] = []

    if project_id:
        project = api_json(meta, "/projects/" + urllib.parse.quote(project_id, safe=""), token=token)
        code = str(project.get("code") or project_id)
        if code.casefold() in running_codes:
            raise RuntimeError(f"项目 {code} 正在容器中执行（{running_source} 判定），拒绝选取")
        variant, reasons = project_eligibility(project, task_type)
        if not variant:
            raise RuntimeError(f"项目 {project_id} 不可选用: {'; '.join(reasons)}")
        return project, variant, {"stage": "指定项目", "runningSource": running_source,
                                  "excludedRunning": []}

    checked: list[str] = []
    owned_waiting: list[str] = []
    username = str(os.environ.get("SOLO_MANAGER_USERNAME") or "").strip()
    for stage_name, path in (("我的项目", "/projects/mine"), ("项目池", "/projects")):
        try:
            stage_projects = list_projects(
                meta,
                token,
                path,
                project_code,
                keyword,
                domain,
                category,
                username if stage_name == "我的项目" else "",
            )
        except RuntimeError as exc:
            # 老版本平台没有 /projects/mine 时，退化为只查公共项目池，不直接失败。
            if stage_name == "我的项目" and ("HTTP 404" in str(exc) or "HTTP 405" in str(exc)):
                continue
            raise
        for project in stage_projects:
            code = str(project.get("code") or "")
            if project_code and code.casefold() != project_code.casefold():
                continue
            if code.casefold() in running_codes:
                running_skipped.append(code)
                if project_code:
                    raise RuntimeError(
                        f"项目 {project_code} 正在容器中执行（{running_source} 判定），拒绝选取"
                    )
                # 我的项目里本来能选、只是暂忙时不能掉到公共池抢别的任务。
                # 等当前容器或占用锁释放后，下一次自动选择仍会优先命中它。
                if stage_name == "我的项目":
                    variant, _ = project_eligibility(project, task_type)
                    if variant:
                        owned_waiting.append(code)
                continue
            checked.append(code)
            variant, reasons = project_eligibility(project, task_type)
            if variant:
                return project, variant, {
                    "stage": stage_name,
                    "runningSource": running_source,
                    "excludedRunning": running_skipped,
                }
            if project_code:
                raise RuntimeError(f"项目 {project_code} 不可选用: {'; '.join(reasons)}")
        if stage_name == "我的项目" and stage_projects and not project_code and owned_waiting:
            waiting = ", ".join(owned_waiting[:20])
            raise RuntimeError(
                "我的项目中有符合条件但正在执行的任务，暂不回退项目池: "
                f"{waiting}（{running_source} 判定）"
            )
    target = project_code or keyword or "当前筛选条件"
    sample = ", ".join(checked[:20]) if checked else "无"
    detail = f"; 已跳过容器中执行的项目: {', '.join(running_skipped[:20])}" if running_skipped else ""
    raise RuntimeError(f"没有找到可选用项目: {target}; 已检查: {sample}{detail}")


def platform_meta(project_dir: Path) -> dict[str, Any]:
    meta = read_json(project_dir / ".solo-platform.json")
    if not meta:
        raise RuntimeError(f"缺少平台任务元数据: {project_dir / '.solo-platform.json'}")
    return meta


def extract_zip(zip_path: Path, target: Path) -> None:
    with zipfile.ZipFile(zip_path) as archive:
        total = 0
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or "../" in Path(name).parts:
                raise RuntimeError(f"ZIP 包含非法路径: {name}")
            total += info.file_size
            if total > 2 * 1024 * 1024 * 1024:
                raise RuntimeError("ZIP 解压后大小超过两吉字节")
            destination = (target / name).resolve()
            if target.resolve() not in destination.parents and destination != target.resolve():
                raise RuntimeError(f"ZIP 路径越界: {name}")
            if info.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, destination.open("wb") as output:
                shutil.copyfileobj(source, output)


def copy_tree(src: Path, dst: Path, overwrite: bool) -> None:
    if not src.exists():
        raise RuntimeError(f"任务包缺少目录: {src.name}")
    for source in src.rglob("*"):
        if not source.is_file():
            continue
        relative = source.relative_to(src)
        target = dst / relative
        if target.exists() and not overwrite:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def initialize_git(workspace: Path) -> None:
    if not (workspace / ".git").exists():
        run(["git", "init", "-q"], cwd=workspace)
    head = run(["git", "rev-parse", "--verify", "HEAD"], cwd=workspace, check=False)
    if head.returncode == 0:
        return
    author = read_json(GITHUB_CONTEXT, {}).get("gitAuthor") or {}
    env = os.environ.copy()
    if author.get("name"):
        env["GIT_AUTHOR_NAME"] = author["name"]
        env["GIT_COMMITTER_NAME"] = author["name"]
        run(["git", "config", "user.name", author["name"]], cwd=workspace)
    if author.get("email"):
        env["GIT_AUTHOR_EMAIL"] = author["email"]
        env["GIT_COMMITTER_EMAIL"] = author["email"]
        run(["git", "config", "user.email", author["email"]], cwd=workspace)
    run(["git", "add", "-A"], cwd=workspace)
    run(["git", "commit", "-m", "chore: initial environment snapshot"], cwd=workspace, env=env)


def create_delivery_if_missing(project_dir: Path, project_id: str) -> Path:
    from openpyxl import Workbook
    delivery = project_dir / "delivery" / f"{project_id}-用户满意度交付.xlsx"
    if delivery.is_file():
        return delivery
    delivery.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "交付记录"
    sheet.append(DELIVERY_HEADERS)
    workbook.save(delivery)
    return delivery


def select_project(args: argparse.Namespace) -> int:
    task_type = canonical_task_type(args.task_type)
    base_url = str(args.base_url).rstrip("/")
    meta = {"baseUrl": base_url, "apiBaseUrl": base_url + "/api/v1"}
    token = load_manager_token()
    workdir = args.workdir.expanduser().resolve()
    project, variant, origin = find_available_project(
        meta,
        token,
        task_type,
        project_code=str(args.project_code or "").strip(),
        project_id=str(args.project_id or "").strip(),
        keyword=str(args.keyword or "").strip(),
        domain=str(args.domain or "").strip(),
        category=str(args.category or "").strip(),
        workdir=workdir,
    )
    quota = next(item for item in project.get("quotas") or [] if item.get("taskType") == task_type)
    selection = {
        "projectId": project["id"],
        "projectCode": project["code"],
        "projectName": project["name"],
        "businessDomain": project.get("businessDomain", ""),
        "category": project.get("category", ""),
        "readinessStatus": project.get("readinessStatus", ""),
        "variantId": variant["id"],
        "variantName": variant.get("directoryName", ""),
        "sourceAssetId": (variant.get("sourceAsset") or {}).get("id", ""),
        "sourceAssetSha256": (variant.get("sourceAsset") or {}).get("sha256", ""),
        "taskType": task_type,
        "quotaBefore": quota,
        "candidateStage": origin.get("stage", ""),
        "excludedRunningProjects": origin.get("excludedRunning") or [],
        "runningContainerSource": origin.get("runningSource", ""),
        "baseUrl": base_url,
        "apiBaseUrl": meta["apiBaseUrl"],
    }
    if args.dry_run:
        print(json.dumps({"status": "dry-run", "selection": selection}, ensure_ascii=False, indent=2))
        return 0

    temporary_package = args.package_output is None
    if temporary_package:
        descriptor, package_name = tempfile.mkstemp(prefix="solo-manager-", suffix="-source-package.zip")
        os.close(descriptor)
        package_path = Path(package_name)
    else:
        package_path = args.package_output.expanduser().resolve()
        package_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        package_request = "/projects/" + urllib.parse.quote(str(project["id"]), safe="") + \
            "/variants/" + urllib.parse.quote(str(variant["id"]), safe="") + \
            "/source-package?" + urllib.parse.urlencode({"rootTaskType": task_type})
        api_download(meta, package_request, package_path, token)
        bootstrap_command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "bootstrap",
            "--package",
            str(package_path),
            "--workdir",
            str(workdir),
            "--base-url",
            base_url,
        ]
        if args.force_source:
            bootstrap_command.append("--force-source")
        if args.platform_task_id:
            bootstrap_command.extend([
                "--platform-task-id", str(args.platform_task_id),
                "--platform-task-no", str(args.platform_task_no or ""),
            ])
        prepared = parse_last_json(run(bootstrap_command).stdout)
        project_dir = Path(prepared["projectDir"]).resolve()
        platform_meta_value = read_json(project_dir / ".solo-platform.json", {})
        selection.update({
            "taskId": platform_meta_value.get("taskId", ""),
            "taskNo": platform_meta_value.get("taskNo", ""),
            "roundId": platform_meta_value.get("roundId", ""),
            "roundNo": platform_meta_value.get("roundNo", 1),
            # Quota bookkeeping for the scheduler monitor: it pre-deducts the
            # attempt before launching the worker and settles or refunds it from
            # this record, so the executor must say which task it actually used.
            "quotaSource": platform_meta_value.get("quotaSource", ""),
            "quotaRecordedAt": platform_meta_value.get("quotaRecordedAt", ""),
        })
        quota_after = current_project_quota(meta, token, project["id"], task_type)
        if quota_after is not None:
            selection["quotaAfter"] = quota_after
            before = selection.get("quotaBefore") or {}
            if isinstance(before.get("remaining"), (int, float)) and isinstance(
                quota_after.get("remaining"), (int, float)
            ):
                selection["quotaDelta"] = quota_after["remaining"] - before["remaining"]
        retained = project_dir / "platform" / "packages" / (
            f"{project['code']}-{selection['taskNo']}-source-package.zip"
        )
        if package_path.resolve() != retained.resolve():
            retained.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(package_path, retained)
        retained_meta = normalize_retained_package(retained, base_url)
        if retained_meta:
            selection["packageBaseUrl"] = retained_meta["packageBaseUrl"]
            selection["packageMetadataRewritten"] = retained_meta["packageMetadataRewritten"]
        package_sha256 = sha256_file(retained)
        write_json(project_dir / "platform" / "selection.json", {
            "status": "ready",
            "selection": selection,
            "packagePath": str(retained),
            "packageSha256": package_sha256,
        })
        # Repeat a compact bootstrap summary so callers can consume one top-level JSON object.
        print(json.dumps({
            "status": "ready",
            "selection": selection,
            "projectDir": str(project_dir),
            "sourcePath": str(project_dir / "source"),
            "historyPromptPath": str(project_dir / "history_prompt"),
            "statePath": str(project_dir / "state.json"),
            "delivery": str(next((project_dir / "delivery").glob("*用户满意度交付.xlsx"), "")),
            "taskNo": selection["taskNo"],
            "taskId": selection["taskId"],
            "roundId": selection["roundId"],
            "firstTaskType": task_type,
            "packagePath": str(retained),
            "packageSha256": package_sha256,
        }, ensure_ascii=False, indent=2))
        return 0
    finally:
        if temporary_package:
            package_path.unlink(missing_ok=True)


def resolve_package_base_url(package_base: str, explicit_base: str | None) -> tuple[str, str, str]:
    """决定元数据里最终使用的平台地址。

    优先级：显式 --base-url > 任务包里的正常地址 > 默认地址。
    任务包里的 localhost / 127.0.0.1 / 0.0.0.0 视为服务端占位地址，一律改写，
    否则下载下来的元数据会把后续接口调用指回不可达的本机地址。
    """
    explicit = str(explicit_base or "").strip().rstrip("/")
    if explicit:
        return explicit, explicit + "/api/v1", "explicit"
    package_value = str(package_base or "").strip().rstrip("/")
    if package_value and not LOCAL_BASE_URL_RE.match(package_value):
        return package_value, package_value + "/api/v1", "package"
    return DEFAULT_BASE_URL, DEFAULT_BASE_URL + "/api/v1", "default"


def zip_member_name(name: str) -> str:
    """去掉 zip 成员名前缀的 ./，保留真实文件名（不能用 lstrip，那会连开头的点一起剥掉）。"""
    return name[2:] if name.startswith("./") else name


def rewrite_zip_member(zip_path: Path, member: str, payload: bytes) -> None:
    """替换 zip 内某个成员的内容，其余成员原样保留。"""
    with zipfile.ZipFile(zip_path) as source:
        entries = [(info, source.read(info.filename)) for info in source.infolist()]
    temp_path = zip_path.with_name(zip_path.name + ".normalize.tmp")
    try:
        with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as target:
            for info, data in entries:
                if zip_member_name(info.filename) == member:
                    data = payload
                target.writestr(info, data)
        temp_path.replace(zip_path)
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


def normalize_retained_package(zip_path: Path, base_url: str) -> dict[str, Any]:
    """把保留包里的 .solo-platform.json 地址改写成可用地址，避免人工查看时仍是 localhost。"""
    if not zip_path.is_file():
        return {}
    with zipfile.ZipFile(zip_path) as archive:
        names = [name for name in archive.namelist() if zip_member_name(name) == ".solo-platform.json"]
        if not names:
            return {}
        raw = archive.read(names[0])
    try:
        meta = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(meta, dict):
        return {}
    before = str(meta.get("baseUrl") or "").rstrip("/")
    base, api, source = resolve_package_base_url(before, base_url)
    changed = before != base or str(meta.get("apiBaseUrl") or "").rstrip("/") != api
    if changed:
        meta["baseUrl"] = base
        meta["apiBaseUrl"] = api
        rewrite_zip_member(zip_path, ".solo-platform.json",
                           (json.dumps(meta, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return {"packageBaseUrl": before, "baseUrl": base, "apiBaseUrl": api,
            "baseUrlSource": source, "packageMetadataRewritten": changed}


def bootstrap(args: argparse.Namespace) -> int:
    package_zip = args.package_zip.expanduser().resolve()
    workdir = args.workdir.expanduser().resolve()
    if not package_zip.is_file():
        raise RuntimeError(f"任务包不存在: {package_zip}")
    workdir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="solo-platform-package-") as temp:
        extracted = Path(temp)
        extract_zip(package_zip, extracted)
        meta = read_json(extracted / ".solo-platform.json")
        if not meta:
            raise RuntimeError("任务包缺少 .solo-platform.json，请从新平台重新下载")
        package_base = str(meta.get("baseUrl") or "")
        base_url, api_base_url, base_url_source = resolve_package_base_url(
            package_base, getattr(args, "base_url", None)
        )
        meta["baseUrl"] = base_url
        meta["apiBaseUrl"] = api_base_url
        info_md = (extracted / "info.md").read_text(encoding="utf-8") if (extracted / "info.md").is_file() else ""
        project_code = str(meta.get("projectCode") or meta.get("projectId") or "").strip()
        task_type = canonical_task_type(str(meta.get("firstTaskType") or ""))
        if not project_code:
            raise RuntimeError("任务包缺少项目编号")

        if not meta.get("taskId"):
            token = load_manager_token()
            if args.platform_task_id:
                # The scheduler monitor already reserved this attempt, so adopt
                # its task instead of spending a second one.
                meta["taskId"] = args.platform_task_id
                meta["taskNo"] = args.platform_task_no or ""
                meta["quotaSource"] = "monitor-prededuct"
                meta["quotaRecordedAt"] = utc_now()
                write_json(extracted / ".solo-platform.json", meta)
            else:
                created = api_json(meta, "/tasks", "POST", {
                    "variantId": meta["variantId"],
                    "rootTaskType": task_type,
                }, token)
                meta["taskId"] = created["id"]
                meta["taskNo"] = created["taskNo"]
                meta["roundId"] = created["rounds"][0]["id"]
                meta["roundNo"] = 1
                meta["quotaSource"] = "executor"
                meta["quotaRecordedAt"] = utc_now()
                write_json(extracted / ".solo-platform.json", meta)

        sequence_match = re.search(r"-(\d+)$", str(meta.get("taskNo") or ""))
        sequence = int(sequence_match.group(1)) if sequence_match else 1
        meta["repoName"] = safe_repo_name(
            str(meta.get("repoName") or f"solo-{project_code}-{sequence}")
        )
        write_json(extracted / ".solo-platform.json", meta)
        init_result = run([
            sys.executable, str(SKILL_SCRIPTS / "init-project-workspace.py"),
            "--workdir", str(workdir), "--project-id", project_code,
            "--task-type", task_type, "--sequence", str(sequence), "--reuse", "--json",
        ])
        init_data = json.loads(init_result.stdout)
        project_dir = Path(init_data["projectDir"]).resolve()
        source_dir = project_dir / "source"
        history_dir = project_dir / "history_prompt"
        source_dir.mkdir(parents=True, exist_ok=True)
        history_dir.mkdir(parents=True, exist_ok=True)
        copy_tree(extracted / "source", source_dir, args.force_source)
        copy_tree(extracted / "history_prompt", history_dir, True)
        if (extracted / "info.md").is_file():
            info_text = (extracted / "info.md").read_text(encoding="utf-8")
            if meta.get("taskNo"):
                info_text = info_text.replace("任务编号：preview", f"任务编号：{meta['taskNo']}")
            (project_dir / "info.md").write_text(info_text, encoding="utf-8")
        write_json(project_dir / ".solo-platform.json", meta)
        initialize_git(source_dir)
        delivery = create_delivery_if_missing(project_dir, project_code)

        state_path = project_dir / "state.json"
        if not state_path.exists():
            run([
                sys.executable, str(SKILL_SCRIPTS / "init-round.py"), str(source_dir),
                "--project-id", project_code, "--task-type", task_type,
                "--delivery", str(delivery), "--output", str(state_path),
            ])
        state = read_json(state_path, {})
        state.update({
            "projectId": project_code,
            "projectPath": str(source_dir),
            "sourcePath": str(source_dir),
            "taskType": task_type,
            "deliveryPath": str(delivery),
            "platform": meta,
        })
        write_json(state_path, state)

        workspace_path = project_dir / "workspace.json"
        workspace = read_json(workspace_path, {})
        workspace.update({
            "sourcePath": str(source_dir),
            "historyPromptPath": str(history_dir),
            "platformTaskId": meta.get("taskId", ""),
            "platformRoundId": meta.get("roundId", ""),
            "platformBaseUrl": meta.get("baseUrl", ""),
            "platform": meta,
        })
        write_json(workspace_path, workspace)

    result = {
        "status": "ready",
        "projectDir": str(project_dir),
        "sourcePath": str(source_dir),
        "historyPromptPath": str(history_dir),
        "statePath": str(state_path),
        "delivery": str(delivery),
        "taskNo": meta.get("taskNo"),
        "taskId": meta.get("taskId"),
        "roundId": meta.get("roundId"),
        "firstTaskType": task_type,
        "baseUrl": base_url,
        "apiBaseUrl": api_base_url,
        "packageBaseUrl": package_base,
        "baseUrlSource": base_url_source,
        "baseUrlRewritten": str(package_base).rstrip("/") != base_url,
        "infoSummary": info_md.splitlines()[:8],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def local_history_entries(project_dir: Path) -> list[tuple[Path, str]]:
    history = project_dir / "history_prompt"
    if not history.is_dir():
        return []
    return [(path, path.read_text(encoding="utf-8")) for path in sorted(history.glob("*.txt"))]


def local_history_texts(project_dir: Path) -> list[str]:
    return [text for _, text in local_history_entries(project_dir)]


def check_prompt(args: argparse.Namespace) -> int:
    project_dir = args.project_dir.expanduser().resolve()
    prompt = args.prompt.expanduser().resolve() if args.prompt else None
    if prompt is None:
        raise RuntimeError("提示词必须先保存为 PROJECT_DIR/prompts/00N.txt 并完成 ra-人话 审核，不能直接用临时文本检查")
    match = re.fullmatch(r"(\d+)", prompt.stem)
    if not match:
        raise RuntimeError("提示词文件名必须为 00N.txt，以便定位对应轮次的 ra-人话 审核")
    round_no = int(match.group(1))
    review = project_dir / "checks" / "human-writing" / f"r{round_no:02d}-prompt.json"
    if not review.is_file():
        raise RuntimeError(f"缺少 ra-人话 审核记录: {review}")
    run([sys.executable, str(SKILL_SCRIPTS / "validate-prompt.py"), str(prompt),
         "--review", str(review)])
    text = prompt.read_text(encoding="utf-8").strip()
    if not text:
        raise RuntimeError("提示词为空")

    _, digest = normalize_prompt(text)
    local_entries = local_history_entries(project_dir)
    local_matches = []
    for history_path, history in local_entries:
        assessment = prompt_assessment(text, history)
        if assessment["blocked"]:
            local_matches.append({"historyFile": str(history_path), **assessment})
            break

    meta = platform_meta(project_dir)
    solo2_history_document = fetch_solo2_prompt_history(project_dir, meta)
    if int(solo2_history_document.get("missingPromptCount") or 0) > 0:
        raise RuntimeError("SOLO2 历史记录存在无法读取的 user_prompt，拒绝在查重不完整时继续")
    if solo2_history_document.get("historyTruncated"):
        raise RuntimeError(
            f"SOLO2 同项目历史超过 {SOLO2_HISTORY_MAX_ITEMS} 条，拒绝在截断列表上查重"
        )

    solo2_matches: list[dict[str, Any]] = []
    solo2_prompts = list(solo2_history_document.get("prompts") or [])
    for item in solo2_prompts:
        history_text = str(item.get("userPrompt") or "").strip()
        if not history_text:
            continue
        assessment = prompt_assessment(text, history_text)
        if assessment["blocked"]:
            solo2_matches.append(
                {
                    "submissionId": item.get("id"),
                    "repoId": item.get("repoId"),
                    "sessionId": item.get("sessionId"),
                    "promptId": item.get("promptId"),
                    "roundNo": item.get("roundNo"),
                    "taskType": item.get("taskType"),
                    **assessment,
                }
            )

    local_match = local_matches[0] if local_matches else None
    solo2_match = solo2_matches[0] if solo2_matches else None
    matches = [*local_matches, *solo2_matches]
    character_duplicate = bool(matches)
    semantic_path = semantic_review_path(project_dir, round_no)
    semantic_check: dict[str, Any] = {
        "path": str(semantic_path),
        "decision": "NOT_CHECKED_CHARACTER_BLOCKED" if character_duplicate else "MISSING",
        "summary": "",
        "matches": [],
        "errors": [],
    }
    semantic_errors: list[str] = []
    semantic_duplicate = False
    if not character_duplicate:
        if not semantic_path.is_file():
            semantic_errors = [f"缺少语义审核记录: {semantic_path}"]
        else:
            try:
                semantic_review = read_json(semantic_path, {})
            except (OSError, json.JSONDecodeError) as exc:
                semantic_errors = [f"语义审核记录不可读: {exc}"]
                semantic_check["decision"] = "INVALID"
            else:
                semantic_errors = semantic_review_errors(
                    semantic_review,
                    round_no=round_no,
                    prompt_text=text,
                    local_count=len(local_entries),
                    solo2_count=len(solo2_prompts),
                )
                semantic_check.update(
                    {
                        "decision": str((semantic_review or {}).get("decision") or "").upper()
                        if isinstance(semantic_review, dict)
                        else "INVALID",
                        "summary": (semantic_review or {}).get("summary", "")
                        if isinstance(semantic_review, dict)
                        else "",
                        "matches": (semantic_review or {}).get("matches", [])
                        if isinstance(semantic_review, dict)
                        else [],
                    }
                )
        semantic_check["errors"] = semantic_errors
        if semantic_errors:
            semantic_check["decision"] = "INVALID"
        else:
            semantic_duplicate = semantic_check["decision"] == "DUPLICATE"

    semantic_review_required = (
        not character_duplicate
        and not semantic_duplicate
        and semantic_check["decision"] != "OK"
    )
    duplicate = character_duplicate or semantic_duplicate
    blocked = duplicate or semantic_review_required
    decisions = [
        decision
        for decision in ("EXACT", "SIMILAR")
        if any(item.get("decision") == decision for item in matches)
    ]
    if "EXACT" in decisions:
        final_decision = "EXACT"
    elif "SIMILAR" in decisions:
        final_decision = "SIMILAR"
    elif semantic_duplicate:
        final_decision = "SEMANTIC_DUPLICATE"
    elif semantic_review_required:
        final_decision = "REVIEW_REQUIRED"
    else:
        final_decision = "OK"

    reasons = list(dict.fromkeys(
        reason
        for item in matches
        for reason in (item.get("reasons") or [])
    ))
    if semantic_duplicate:
        reasons.extend(
            str(item.get("reason"))
            for item in semantic_check.get("matches") or []
            if isinstance(item, dict) and str(item.get("reason") or "").strip()
        )
    reasons.extend(semantic_errors)
    reasons = list(dict.fromkeys(reasons))

    result = {
        "status": "duplicate" if duplicate else "review_required" if semantic_review_required else "ok",
        "blocked": blocked,
        "duplicate": duplicate,
        "decision": final_decision,
        "roundNo": round_no,
        "similarity": max(
            [float(item.get("similarity") or 0) for item in matches],
            default=0.0,
        ),
        "longestCommonSubstringLength": max(
            [int(item.get("longestCommonSubstringLength") or 0) for item in matches],
            default=0,
        ),
        "reasons": reasons,
        "matchedText": (local_match or solo2_match or {}).get("matchedText") or "",
        "normalizedSha256": digest,
        "localDuplicate": bool(local_matches),
        "localMatch": local_match,
        "solo2Duplicate": bool(solo2_matches),
        "solo2Matches": solo2_matches,
        "semanticDuplicate": semantic_duplicate,
        "semanticReviewRequired": semantic_review_required,
        "semanticCheck": semantic_check,
        "solo2History": {
            "source": solo2_history_document.get("source") or "solo2",
            "fallback": (solo2_history_document.get("source") == "local_cache"),
            "fallbackReason": solo2_history_document.get("fallbackReason") or "",
            "cacheStale": bool(solo2_history_document.get("cacheStale")),
            "historyPath": solo2_history_document.get("historyPath")
            or str(project_dir / "checks" / "platform" / "solo2-prompt-history.json"),
            "cachePath": solo2_history_document.get("cachePath")
            or str(prompt_cache_project_path(project_dir)),
            "promptCachePath": str(prompt_cache_project_path(project_dir)),
            "matchedSubmissionCount": solo2_history_document.get("matchedSubmissionCount", 0),
            "submissionTotal": solo2_history_document.get("submissionTotal", 0),
        },
        "promptPath": str(prompt),
    }
    output = project_dir / "checks" / "platform" / "prompt-check.json"
    round_output = project_dir / "checks" / "platform" / f"prompt-check-r{round_no:02d}.json"
    result["outputPath"] = str(output)
    result["roundCheckPath"] = str(round_output)
    write_json(output, result)
    write_json(round_output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if blocked else 0


def prompt_semantic_init(args: argparse.Namespace) -> int:
    project_dir = args.project_dir.expanduser().resolve()
    round_no = int(args.round)
    if round_no < 1:
        raise RuntimeError("round 必须大于等于 1")
    prompt = project_dir / "prompts" / f"{round_no:03d}.txt"
    if not prompt.is_file() or not prompt.read_text(encoding="utf-8").strip():
        raise RuntimeError(f"缺少或空提示词: {prompt}")
    history_path = project_dir / "checks" / "platform" / "solo2-prompt-history.json"
    history_document = read_json(history_path, {})
    if not isinstance(history_document, dict) or history_document.get("status") != "ready":
        raise RuntimeError(f"SOLO2 历史缓存未就绪，请先运行 solo2-history: {history_path}")
    if int(history_document.get("missingPromptCount") or 0) > 0 or history_document.get("historyTruncated"):
        raise RuntimeError("SOLO2 历史缓存不完整，拒绝生成语义审核模板")
    target = semantic_review_path(project_dir, round_no)
    if target.exists() and not args.force:
        raise RuntimeError(f"语义审核记录已存在，如需覆盖请加 --force: {target}")
    text = prompt.read_text(encoding="utf-8").strip()
    review = {
        "schemaVersion": 1,
        "roundNo": round_no,
        "promptPath": str(prompt),
        "promptSha256": normalize_prompt(text)[1],
        "reviewedSourceCounts": {
            "local": len(local_history_entries(project_dir)),
            "solo2": len(history_document.get("prompts") or []),
        },
        "decision": "REVIEW_REQUIRED",
        "summary": "待完成语义审核：逐条比较业务目标、目标模块、修改动作、交付物和使用边界，完成后改为 OK 或 DUPLICATE。",
        "matches": [],
        "createdAt": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    write_json(target, review)
    print(json.dumps({"status": "created", "path": str(target), "review": review}, ensure_ascii=False, indent=2))
    return 0


def github_context() -> dict[str, Any]:
    data = read_json(GITHUB_CONTEXT, {})
    if not data.get("github", {}).get("token") or not data.get("github", {}).get("username"):
        raise RuntimeError(f"缺少 GitHub 凭据: {GITHUB_CONTEXT}")
    return data


def git_env(remote_url: str, context: dict[str, Any], token: str) -> dict[str, str]:
    env = os.environ.copy()
    parsed = urllib.parse.urlparse(remote_url)
    base = f"{parsed.scheme}://{parsed.netloc}/"
    username = str((context.get("github") or {}).get("username") or context.get("username") or "")
    git_author = context.get("gitAuthor") or {}
    if not username or not git_author.get("name") or not git_author.get("email"):
        raise RuntimeError("GitHub 上下文缺少 username 或 gitAuthor")
    auth = base64.b64encode(f"{username}:{token}".encode()).decode()
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"http.{base}.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {auth}",
        "GIT_AUTHOR_NAME": git_author["name"],
        "GIT_AUTHOR_EMAIL": git_author["email"],
        "GIT_COMMITTER_NAME": git_author["name"],
        "GIT_COMMITTER_EMAIL": git_author["email"],
    })
    return env


def github_request(url: str, token: str, method: str = "GET", body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("User-Agent", "solo-annotation-loop")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"message": raw}
        return exc.code, payload


def ensure_github_repo(workspace: Path, repo_name: str, configured_repo_url: str = "") -> tuple[str, str, str]:
    context = github_context()
    github = context["github"]
    owner = github["username"]
    token = github["token"]
    existing = run(["git", "remote", "get-url", "origin"], cwd=workspace, check=False).stdout.strip()
    expected = f"https://github.com/{owner}/{repo_name}"

    def remote_matches_task_repo(value: str) -> bool:
        if "github.com" not in value:
            return False
        remote_name = value.rstrip("/").removesuffix(".git").rsplit("/", 1)[-1]
        return remote_name.lower() == repo_name.lower()

    # configured_repo_url may describe the source-project repository from the
    # task package. It is only a target when its repository name matches the
    # task-isolated repo name; otherwise create or reuse the task repository.
    candidate = configured_repo_url.strip() if remote_matches_task_repo(configured_repo_url.strip()) else ""
    if not candidate and remote_matches_task_repo(existing):
        candidate = existing
    if candidate and "github.com" in candidate:
        repo_url = candidate[:-4] if candidate.endswith(".git") else candidate.rstrip("/")
        remote_url = repo_url + ".git"
        remotes = run(["git", "remote"], cwd=workspace).stdout.splitlines()
        if "origin" in remotes:
            if existing != remote_url:
                run(["git", "remote", "set-url", "origin", remote_url], cwd=workspace)
        else:
            run(["git", "remote", "add", "origin", remote_url], cwd=workspace)
        return repo_url, remote_url, token
    status, payload = github_request(f"https://api.github.com/repos/{owner}/{repo_name}", token)
    if status == 404:
        status, payload = github_request("https://api.github.com/user/repos", token, "POST", {
            "name": repo_name,
            "private": False,
            "auto_init": False,
        })
    if status not in (200, 201):
        raise RuntimeError(f"GitHub 仓库准备失败: HTTP {status} {payload.get('message', '')}")
    repo_url = f"https://github.com/{owner}/{repo_name}"
    remote_url = repo_url + ".git"
    remotes = run(["git", "remote"], cwd=workspace).stdout.splitlines()
    if "origin" in remotes:
        run(["git", "remote", "set-url", "origin", remote_url], cwd=workspace)
    else:
        run(["git", "remote", "add", "origin", remote_url], cwd=workspace)
    return repo_url, remote_url, token


def push_initial_snapshot(workspace: Path, remote_url: str, token: str, author: dict[str, Any]) -> str:
    env = git_env(remote_url, author, token)
    run(["git", "push", "-u", "origin", "HEAD:main"], cwd=workspace, env=env)
    root = run(["git", "rev-list", "--max-parents=0", "HEAD"], cwd=workspace).stdout.strip().splitlines()
    if not root:
        raise RuntimeError("无法定位初始环境快照提交")
    return root[0]


def safe_repo_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-").lower()
    return value or "solo-task-" + uuid.uuid4().hex[:8]


def validate_json_command(cmd: list[str]) -> None:
    run([sys.executable] + cmd)


def load_round_files(project_dir: Path, round_no: int) -> dict[str, Path]:
    prompt = project_dir / "prompts" / f"{round_no:03d}.txt"
    round_dir = project_dir / "rounds" / f"r{round_no:02d}"
    result = {
        "prompt": prompt,
        "locator": round_dir / "round-locator.json",
        "score": round_dir / "round-score.json",
        "verification": round_dir / "verify.json",
    }
    missing = [str(path) for key, path in result.items() if key in {"prompt", "locator", "score"} and not path.is_file()]
    if missing:
        raise RuntimeError("缺少轮次文件: " + ", ".join(missing))
    return result


def update_state(project_dir: Path, key: str, value: Any) -> None:
    state_path = project_dir / "state.json"
    state = read_json(state_path, {})
    state[key] = value
    write_json(state_path, state)


def build_bundle(project_dir: Path, round_no: int, meta: dict[str, Any], files: dict[str, Path],
                 trace_path: Path, delivery: Path, commit_url: str, env_snapshot: str,
                 round_type: str, harness: str, harness_version: str, os_platform: str,
                 difficulty: str, languages: str, repro_level: str) -> tuple[Path, dict[str, Any]]:
    score = read_json(files["score"], {})
    locator = read_json(files["locator"], {})
    prompt_text = files["prompt"].read_text(encoding="utf-8").strip()
    review_prompt = project_dir / "checks" / "human-writing" / f"r{round_no:02d}-prompt.json"
    review_comments = project_dir / "checks" / "human-writing" / f"r{round_no:02d}-comments.json"
    temp = Path(tempfile.mkstemp(prefix=f"solo-round-{round_no}-", suffix=".zip")[1])
    manifest_files: list[dict[str, Any]] = []

    with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        def add(path: Path, member: str, kind: str) -> None:
            data = path.read_bytes()
            archive.writestr(member, data)
            manifest_files.append({"path": member, "kind": kind,
                                   "sha256": sha256_bytes(data), "size": len(data)})

        add(files["prompt"], "prompt.txt", "PROMPT")
        add(files["locator"], "round-locator.json", "ROUND_LOCATOR")
        add(files["score"], "round-score.json", "ROUND_SCORE")
        if files["verification"].is_file():
            add(files["verification"], "verification.json", "VERIFICATION")
        if review_prompt.is_file():
            add(review_prompt, "reviews/prompt.json", "PROMPT_REVIEW")
        if review_comments.is_file():
            add(review_comments, "reviews/comments.json", "COMMENTS_REVIEW")
        add(trace_path, "traces/trace.jsonl", "TRACE")
        if delivery.is_file():
            add(delivery, "delivery/delivery.xlsx", "DELIVERY_EXCEL")

        workspace = project_dir / "source"
        round_dir = project_dir / "rounds" / f"r{round_no:02d}"
        round_commit = read_json(round_dir / "round-commit.json", {})
        commit_ref = str(locator.get("commitHash") or round_commit.get("commitHash") or "HEAD")
        workspace_zip = Path(tempfile.mkstemp(prefix="solo-workspace-", suffix=".zip")[1])
        try:
            run(["git", "archive", "--format=zip", "-o", str(workspace_zip), commit_ref], cwd=workspace)
            add(workspace_zip, "workspace.zip", "WORKSPACE")
        finally:
            workspace_zip.unlink(missing_ok=True)

        manifest = {
            "schemaVersion": 1,
            "taskNo": meta["taskNo"],
            "roundNo": round_no,
            "taskType": round_type,
            "sessionId": locator.get("sessionId") or score.get("sessionId") or "",
            "promptId": locator.get("promptId") or score.get("promptId") or "",
            "harness": harness,
            "harnessVersion": harness_version,
            "osPlatform": os_platform,
            "sourceSnapshotSha256": meta["sourceSnapshotSha256"],
            "envSnapshot": env_snapshot,
            "commitUrl": commit_url,
            "repoUrl": locator.get("repoUrl") or score.get("repoUrl") or "",
            "difficulty": difficulty,
            "languages": languages,
            "reproLevel": repro_level,
            "otherIssues": (score.get("comments") or {}).get("other", "无"),
            "scores": score.get("scores") or {},
            "comments": score.get("comments") or {},
            "files": manifest_files,
        }
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return temp, manifest


def platform_upload(meta: dict[str, Any], bundle: Path, round_no: int, round_type: str,
                    locator: dict[str, Any], score: dict[str, Any]) -> dict[str, Any]:
    token = load_manager_token()
    round_id = str(meta.get("roundId") or "")
    if round_no != int(meta.get("roundNo") or 1) or not round_id:
        # 上传失败后的重试必须复用平台已经创建的同一 roundNo，避免重复轮次。
        task = api_json(meta, f"/tasks/{meta['taskId']}", token=token)
        existing_round = next(
            (
                item
                for item in (task.get("rounds") or [])
                if int(item.get("roundNo") or 0) == round_no
            ),
            None,
        )
        if existing_round:
            existing_type = str(existing_round.get("taskType") or "")
            if existing_type and existing_type != round_type:
                raise RuntimeError(
                    f"平台轮次 {round_no} 的任务类型为 {existing_type}，与本次 {round_type} 不一致"
                )
            round_id = str(existing_round.get("id") or "")
        else:
            created = api_json(meta, f"/tasks/{meta['taskId']}/rounds", "POST", {
                "taskType": round_type,
                "sessionId": locator.get("sessionId") or score.get("sessionId") or "",
                "promptId": locator.get("promptId") or score.get("promptId") or "",
            }, token)
            round_id = created["id"]
        if not round_id:
            raise RuntimeError(f"平台轮次 {round_no} 缺少 round id")
        meta["roundId"] = round_id
        meta["roundNo"] = round_no
        write_json(Path(meta["_project_dir"]) / ".solo-platform.json", {k: v for k, v in meta.items() if not k.startswith("_")})
    payload = bundle.read_bytes()
    digest = sha256_bytes(payload)
    ticket = api_json(meta, f"/rounds/{round_id}/packages/presign", "POST", {
        "fileName": bundle.name,
        "sizeBytes": len(payload),
        "sha256": digest,
    }, token)
    upload_status = put_presigned_upload(
        str(ticket["uploadUrl"]), meta, payload, ticket.get("headers") or {}
    )
    if upload_status not in (200, 201, 204):
        raise RuntimeError(f"平台对象上传失败: HTTP {upload_status}")
    return api_json(meta, f"/packages/{ticket['packageId']}/complete", "POST", {
        "sha256": digest,
        "sizeBytes": len(payload),
    }, token)


def publish_git(project_dir: Path, meta: dict[str, Any], round_no: int, session_id: str) -> dict[str, Any]:
    workspace = project_dir / "source"
    initialize_git(workspace)
    round_dir = project_dir / "rounds" / f"r{round_no:02d}"
    locator_path = round_dir / "round-locator.json"
    score_path = round_dir / "round-score.json"
    locator = read_json(locator_path, {})
    score = read_json(score_path, {})
    existing_commit = read_json(round_dir / "round-commit.json", {})
    existing_hash = str(existing_commit.get("commitHash") or "").strip()
    if existing_hash and not existing_commit.get("reachable", {}).get("reachable") is False:
        probe = run(["git", "cat-file", "-t", existing_hash], cwd=workspace, check=False)
        if probe.returncode == 0 and probe.stdout.strip() == "commit":
            repo_url = str(existing_commit.get("repoUrl") or locator.get("repoUrl") or "").rstrip("/")
            initial_url = str(locator.get("initialSnapshot") or locator.get("envSnapshot") or "")
            commit_url = str(existing_commit.get("commitUrl") or f"{repo_url}/commit/{existing_hash}")
            locator.update({"initialSnapshot": initial_url, "envSnapshot": initial_url, "repoUrl": repo_url, "commitUrl": commit_url, "commitHash": existing_hash})
            score.update({"repoUrl": repo_url, "commitUrl": commit_url, "commitHash": existing_hash})
            write_json(locator_path, locator)
            write_json(score_path, score)
            state = read_json(project_dir / "state.json", {})
            if initial_url:
                state["initialSnapshot"] = initial_url
            if repo_url:
                state["repoUrl"] = repo_url
            write_json(project_dir / "state.json", state)
            return {"repoUrl": repo_url, "initialSnapshot": initial_url, "commitUrl": commit_url, "commitHash": existing_hash, "reusedHistoricalCommit": True}
    task_match = re.search(r"-(\d+)$", str(meta.get("taskNo") or ""))
    task_sequence = int(task_match.group(1)) if task_match else int(meta.get("sequence", round_no))
    repo_name = safe_repo_name(
        str(meta.get("repoName") or f"solo-{meta.get('projectCode')}-{task_sequence}")
    )
    repo_url, remote_url, token = ensure_github_repo(workspace, repo_name, str(meta.get("repoUrl") or ""))
    context = github_context()
    initial_sha = push_initial_snapshot(workspace, remote_url, token, context)
    initial_url = f"{repo_url}/commit/{initial_sha}"
    locator.update({"initialSnapshot": initial_url, "envSnapshot": initial_url, "repoUrl": repo_url})
    write_json(locator_path, locator)
    score["repoUrl"] = repo_url
    write_json(score_path, score)
    state_path = project_dir / "state.json"
    state = read_json(state_path, {})
    state["initialSnapshot"] = initial_url
    state["repoUrl"] = repo_url
    write_json(state_path, state)

    expected_subject = f"round {round_no}: {session_id}"
    head_sha = run(["git", "rev-parse", "HEAD"], cwd=workspace).stdout.strip()
    head_subject = run(["git", "show", "-s", "--format=%s", "HEAD"], cwd=workspace).stdout.strip()
    commit = {}
    if head_subject == expected_subject:
        commit = {"commitHash": head_sha, "commitUrl": f"{repo_url}/commit/{head_sha}"}
    else:
        commit_result = run([
            sys.executable, str(SKILL_SCRIPTS / "commit-round.py"), str(workspace),
            "--round", str(round_no), "--session-id", session_id,
            "--output", str(project_dir / "rounds" / f"r{round_no:02d}" / "round-commit.json"),
        ], check=False)
        if commit_result.returncode != 0 and "无变更文件" not in (commit_result.stdout + commit_result.stderr):
            raise RuntimeError(commit_result.stderr or commit_result.stdout)
        commit = read_json(project_dir / "rounds" / f"r{round_no:02d}" / "round-commit.json", {})
    commit_hash = commit.get("commitHash") or head_sha
    commit_url = commit.get("commitUrl") or f"{repo_url}/commit/{commit_hash}"
    locator["commitUrl"] = commit_url
    write_json(locator_path, locator)
    score["commitUrl"] = commit_url
    score["commitHash"] = commit_hash
    write_json(score_path, score)
    return {"repoUrl": repo_url, "initialSnapshot": initial_url, "commitUrl": commit_url,
            "commitHash": commit_hash}


def normalize_repro_level(value: Any) -> str:
    """把说明性文字归一化为 SOLO2 接受的固定枚举。"""
    text = str(value or "").strip()
    if "已容器化" in text:
        return "已容器化，可一键起环境"
    if "未容器化" in text or "有外部依赖" in text:
        return "有外部依赖，未容器化"
    if "无外部依赖" in text:
        return "无外部依赖"
    return text or "已容器化，可一键起环境"


DELIVERY_LANGUAGE_LIMIT = 4


def shorten_language_framework(values: Any, limit: int = DELIVERY_LANGUAGE_LIMIT) -> str:
    """交付表「语言/框架」只写主要的前几项，避免把整套技术栈都堆进去。"""
    if isinstance(values, str):
        items = re.split(r"[,，、;；/]", values)
    elif isinstance(values, (list, tuple, set)):
        items = [str(item) for item in values]
    else:
        items = []
    kept: list[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in kept:
            kept.append(text)
    return ", ".join(kept[:limit])


def update_delivery(project_dir: Path, round_no: int, meta: dict[str, Any]) -> dict[str, Any]:
    difficulty_review = ensure_required_difficulty(project_dir, round_no)
    state = read_json(project_dir / "state.json", {})
    delivery = Path(state.get("deliveryPath") or project_dir / "delivery" / f"{meta['projectCode']}-用户满意度交付.xlsx")
    files = load_round_files(project_dir, round_no)
    locator = read_json(files["locator"], {})
    score = read_json(files["score"], {})
    profile = read_json(project_dir / "project-profile.json", {})
    profile_languages = profile.get("languageFramework") or []
    state_languages = state.get("languageFramework") or []
    locator_languages = locator.get("languageFramework") or []
    language_values = locator_languages or state_languages or profile_languages
    language_text = shorten_language_framework(language_values)
    defaults = {
        "taskType": score.get("taskType") or locator.get("taskType") or state.get("taskType") or "",
        "difficulty": difficulty_review["effectiveDifficulty"],
        "os": locator.get("os") or state.get("os") or "MacOS/Linux",
        "languageFramework": language_text,
        "reproducibility": normalize_repro_level(
            locator.get("reproducibility") or state.get("reproducibility") or profile.get("reproducibility") or ""
        ),
    }
    changed = False
    for key, value in defaults.items():
        if value and not locator.get(key):
            locator[key] = value
            changed = True
    if changed:
        write_json(files["locator"], locator)
    output = project_dir / "delivery" / f".delivery-round-{round_no}.xlsx"
    output.unlink(missing_ok=True)
    profile = project_dir / "project-profile.json"
    cmd = [
        "node", str(SKILL_SCRIPTS / "update-delivery.mjs"),
        "--delivery", str(delivery), "--score", str(files["score"]),
        "--locator", str(files["locator"]), "--prompt", str(files["prompt"]),
        "--output", str(output), "--project-id", meta["projectCode"],
        "--round", str(round_no),
    ]
    if profile.is_file():
        cmd.extend(["--profile", str(profile)])
    result = run(cmd, check=False)
    duplicate = result.returncode != 0 and "重复记录" in (result.stdout + result.stderr)
    if result.returncode != 0 and not duplicate:
        raise RuntimeError(result.stderr or result.stdout)
    backup = delivery.with_name(delivery.stem + f".bak-platform-{round_no}.xlsx")
    shutil.copy2(delivery, backup)
    appended = output.is_file()
    if appended:
        os.replace(output, delivery)
        # 表格模板会把 ISO 时间自动转成日期序列值，提交审核要求 ISO 文本，这里显式回写纯文本格式。
        from openpyxl import load_workbook

        workbook = load_workbook(delivery)
        sheet = workbook.active
        headers = [cell.value for cell in sheet[1]]
        session_col = headers.index("SessionID") + 1
        prompt_col = headers.index("TurnID/PromptID") + 1
        submit_col = headers.index("提交时间") + 1
        session_value = str(locator.get("sessionId") or score.get("sessionId") or "")
        prompt_value = str(locator.get("promptId") or score.get("promptId") or "")
        submit_value = str(locator.get("endTime") or score.get("submittedAt") or "")
        for row_index in range(2, sheet.max_row + 1):
            if (
                str(sheet.cell(row_index, session_col).value or "") == session_value
                and str(sheet.cell(row_index, prompt_col).value or "") == prompt_value
            ):
                submit_cell = sheet.cell(row_index, submit_col)
                submit_cell.value = submit_value
                submit_cell.number_format = "@"
                break
        workbook.save(delivery)
    # Backfill the published snapshot across every row from the same container/session,
    # then normalize the current round's commit and submission time as text.
    from openpyxl import load_workbook
    workbook = load_workbook(delivery, data_only=False)
    sheet = workbook.worksheets[0]
    headers = [str(sheet.cell(1, column).value or "") for column in range(1, sheet.max_column + 1)]
    column = {name: index + 1 for index, name in enumerate(headers)}
    session_id = str(locator.get("sessionId") or score.get("sessionId") or "")
    env_snapshot = str(locator.get("envSnapshot") or locator.get("initialSnapshot") or "")
    commit_url = str(score.get("commitUrl") or locator.get("commitUrl") or "")
    submitted_at = str(score.get("submittedAt") or locator.get("endTime") or
                     dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"))
    for row in range(2, sheet.max_row + 1):
        if session_id and str(sheet.cell(row, column.get("SessionID", 2)).value or "") != session_id:
            continue
        if env_snapshot:
            sheet.cell(row, column["初始环境快照"]).value = env_snapshot
            sheet.cell(row, column["初始环境快照"]).number_format = "@"
        if str(sheet.cell(row, column.get("轮次", 4)).value or "") == str(round_no):
            if commit_url:
                sheet.cell(row, column["提交地址"]).value = commit_url
                sheet.cell(row, column["提交地址"]).number_format = "@"
            if submitted_at:
                sheet.cell(row, column["提交时间"]).value = submitted_at
                sheet.cell(row, column["提交时间"]).number_format = "@"
            for field, value in {
                "操作系统": defaults["os"],
                "任务类型": defaults["taskType"],
                "任务难度": defaults["difficulty"],
                "语言/框架": defaults["languageFramework"],
                "环境可复现等级": defaults["reproducibility"],
            }.items():
                if value and column.get(field):
                    sheet.cell(row, column[field]).value = value
                    sheet.cell(row, column[field]).number_format = "@"
    workbook.save(delivery)
    return {"delivery": str(delivery), "backup": str(backup), "updated": appended, "backfilled": bool(env_snapshot)}


def solo2_submit(project_dir: Path, delivery: Path, round_no: int) -> dict[str, Any]:
    review_root = project_dir / "checks" / "human-writing"
    audit = run([
        sys.executable, str(SOLO_PUSH_SCRIPTS / "audit_before_push.py"),
        "--delivery", str(delivery), "--round", str(round_no),
        "--human-review-root", str(review_root),
    ], check=False)
    if audit.returncode != 0:
        raise RuntimeError("solo2 交付前审核失败:\n" + (audit.stdout + audit.stderr))
    output = project_dir / "rounds" / f"r{round_no:02d}" / "solo2-submit-result.json"
    result = run([
        sys.executable, str(SOLO_PUSH_SCRIPTS / "push_delivery.py"),
        "--delivery", str(delivery), "--all", "--submit",
        "--human-review-root", str(review_root), "--output", str(output),
    ], check=False)
    if result.returncode != 0:
        raise RuntimeError("solo2 提交失败:\n" + (result.stdout + result.stderr))
    return read_json(output, {"status": "submitted"})


def submit(args: argparse.Namespace) -> int:
    project_dir = args.project_dir.expanduser().resolve()
    meta = platform_meta(project_dir)
    round_no = int(args.round)
    files = load_round_files(project_dir, round_no)
    difficulty_review = ensure_required_difficulty(project_dir, round_no)
    locator = read_json(files["locator"], {})
    score = read_json(files["score"], {})
    high_score_review = validate_high_score_review(project_dir, round_no, score)
    if not high_score_review.get("ok"):
        raise RuntimeError(
            "SOLO2 均分门禁未通过: "
            + "；".join(str(item) for item in high_score_review.get("errors", []))
        )
    if high_score_review.get("terminalAbandon"):
        raise RuntimeError(
            "高分解剖报告判定没有真实扣分点，本轮必须放弃，禁止提交 SOLO2"
        )
    state = read_json(project_dir / "state.json", {})
    session_id = str(locator.get("sessionId") or score.get("sessionId") or state.get("sessionId") or "")
    if not session_id:
        raise RuntimeError("缺少 SessionID，不能提交")
    round_type = canonical_task_type(score.get("taskType") or state.get("taskType") or meta["firstTaskType"])
    trace_value = locator.get("tracePath") or state.get("tracePath") or ""
    trace_path = Path(str(trace_value)).expanduser()
    if not trace_path.is_file():
        matches = sorted((project_dir / "traces").rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not matches:
            raise RuntimeError("缺少轨迹 JSONL")
        trace_path = matches[0]
    delivery = Path(state.get("deliveryPath") or project_dir / "delivery" / f"{meta['projectCode']}-用户满意度交付.xlsx")
    if not delivery.is_file():
        raise RuntimeError(f"缺少交付表: {delivery}")

    if args.dry_run:
        prompt_text = files["prompt"].read_text(encoding="utf-8").strip()
        result = {
            "status": "dry-run",
            "projectDir": str(project_dir),
            "round": round_no,
            "taskNo": meta.get("taskNo"),
            "taskType": round_type,
            "sessionId": session_id,
            "tracePath": str(trace_path),
            "delivery": str(delivery),
            "historyCount": len(local_history_texts(project_dir)),
            "normalizedPromptSha256": normalize_prompt(prompt_text)[1],
            "difficultyReview": difficulty_review,
            "highScoreReview": high_score_review,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    validate_json_command([str(SKILL_SCRIPTS / "validate-prompt.py"), str(files["prompt"]),
                           "--review", str(project_dir / "checks" / "human-writing" / f"r{round_no:02d}-prompt.json")])
    validate_json_command([str(SKILL_SCRIPTS / "validate-comments.py"), str(files["score"]),
                           "--review", str(project_dir / "checks" / "human-writing" / f"r{round_no:02d}-comments.json")])
    validate_json_command([str(SKILL_SCRIPTS / "validate-score-gate.py"), str(files["score"])])

    git_result = publish_git(project_dir, meta, round_no, session_id)
    delivery_result = update_delivery(project_dir, round_no, meta)
    locator = read_json(files["locator"], {})
    score = read_json(files["score"], {})
    env_snapshot = locator.get("envSnapshot") or locator.get("initialSnapshot") or state.get("initialSnapshot")
    if not env_snapshot:
        raise RuntimeError("GitHub 初始快照链接为空")
    bundle, manifest = build_bundle(
        project_dir, round_no, meta, files, trace_path, delivery, git_result["commitUrl"], env_snapshot,
        round_type, str(state.get("harness") or "Claude Code"),
        str(state.get("harnessVersion") or ""), str(state.get("os") or "MacOS/Linux"),
        str(difficulty_review["effectiveDifficulty"]),
        shorten_language_framework(state.get("languageFramework") or []),
        normalize_repro_level(state.get("reproducibility") or "已容器化，可一键起环境")
    )
    try:
        meta["_project_dir"] = str(project_dir)
        result_path = project_dir / "rounds" / f"r{round_no:02d}" / "platform-submit-result.json"

        # Solo Manager 是尽力上报，SOLO2 是硬提交目标。Manager 失败不能阻断 SOLO2，
        # 但必须保留原始错误、已完成步骤和失败状态供后续人工补交。
        platform_result = None
        platform_error = None
        try:
            platform_result = platform_upload(meta, bundle, round_no, round_type, locator, score)
            state["platformSubmission"] = platform_result
        except Exception as exc:
            platform_error = {"status": "failed", "error": str(exc)}
            state["platformSubmission"] = platform_error
        write_json(project_dir / "state.json", state)
        write_json(result_path, {
            "status": "manager-submitted" if platform_result else "manager-failed",
            "projectDir": str(project_dir),
            "round": round_no,
            "git": git_result,
            "delivery": delivery_result,
            "platform": platform_result or platform_error,
        })

        try:
            solo2_result = solo2_submit(project_dir, delivery, round_no)
        except Exception as exc:
            state["solo2Submission"] = {"status": "failed", "error": str(exc)}
            write_json(project_dir / "state.json", state)
            write_json(result_path, {
                "status": "partial" if platform_result else "failed",
                "projectDir": str(project_dir),
                "round": round_no,
                "git": git_result,
                "delivery": delivery_result,
                "platform": platform_result or platform_error,
                "solo2": {"status": "failed", "error": str(exc)},
            })
            raise

        result = {
            "status": "submitted" if platform_result else "solo2_submitted_manager_failed",
            "projectDir": str(project_dir),
            "round": round_no,
            "git": git_result,
            "delivery": delivery_result,
            "platform": platform_result or platform_error,
            "managerSubmissionFailed": bool(platform_error),
            "solo2": solo2_result,
            "difficultyReview": difficulty_review,
            "highScoreReview": high_score_review,
        }
        write_json(result_path, result)
        state["solo2Submission"] = solo2_result
        write_json(project_dir / "state.json", state)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        bundle.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Solo annotation loop platform bridge")
    sub = parser.add_subparsers(dest="command", required=True)

    p_select = sub.add_parser(
        "select",
        help="Select an available Solo Manager project, download its package, and bootstrap",
    )
    p_select.add_argument("--workdir", type=Path, required=True)
    p_select.add_argument("--task-type", required=True)
    p_select.add_argument("--project-code")
    p_select.add_argument("--project-id")
    p_select.add_argument("--keyword")
    p_select.add_argument("--domain")
    p_select.add_argument("--category")
    p_select.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p_select.add_argument("--package-output", type=Path)
    p_select.add_argument("--force-source", action="store_true")
    p_select.add_argument("--dry-run", action="store_true")
    p_select.add_argument(
        "--platform-task-id",
        default="",
        help="监控台已预扣除的平台任务 ID；提供后复用该任务，不再重复扣除配额",
    )
    p_select.add_argument("--platform-task-no", default="")
    p_select.set_defaults(func=select_project)

    p_boot = sub.add_parser("bootstrap", help="Expand a downloaded task package")
    p_boot.add_argument("--package", dest="package_zip", required=True, type=Path)
    p_boot.add_argument("--workdir", type=Path, required=True)
    p_boot.add_argument(
        "--base-url",
        default=None,
        help=f"平台地址；不传时改写任务包里的 localhost 为 {DEFAULT_BASE_URL}",
    )
    p_boot.add_argument("--force-source", action="store_true")
    p_boot.add_argument(
        "--platform-task-id",
        default="",
        help="监控台已预扣除的平台任务 ID；提供后复用该任务，不再重复扣除配额",
    )
    p_boot.add_argument("--platform-task-no", default="")
    p_boot.set_defaults(func=bootstrap)

    p_check = sub.add_parser("check-prompt", help="Check a candidate prompt for duplicates")
    p_check.add_argument("--project-dir", required=True, type=Path)
    p_check_group = p_check.add_mutually_exclusive_group(required=True)
    p_check_group.add_argument("--prompt", type=Path)
    p_check_group.add_argument("--text")
    p_check.set_defaults(func=check_prompt)

    p_semantic = sub.add_parser(
        "prompt-semantic-init",
        help="Create the per-round semantic prompt dedup review scaffold",
    )
    p_semantic.add_argument("--project-dir", required=True, type=Path)
    p_semantic.add_argument("--round", required=True, type=int)
    p_semantic.add_argument("--force", action="store_true")
    p_semantic.set_defaults(func=prompt_semantic_init)

    p_history = sub.add_parser(
        "solo2-history",
        help="Fetch historical prompts for the current project from SOLO2",
    )
    p_history.add_argument("--project-dir", required=True, type=Path)
    p_history.add_argument("--project-code")
    p_history.set_defaults(func=solo2_history)

    p_submit = sub.add_parser("submit", help="Submit one round to Solo Manager and solo2")
    p_submit.add_argument("--project-dir", required=True, type=Path)
    p_submit.add_argument("--round", required=True, type=int)
    p_submit.add_argument("--dry-run", action="store_true")
    p_submit.set_defaults(func=submit)

    args = parser.parse_args()
    try:
        return args.func(args)
    except Exception as exc:
        print(f"platform bridge 失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

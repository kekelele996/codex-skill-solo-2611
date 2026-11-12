#!/usr/bin/env python3
"""Shared policy gate for Solo2 rounds whose raw average exceeds 4.2."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SOLO2_MAX_AVERAGE = 4.2
SCORE_KEYS = ("delivery", "instruction", "planning", "reasoning", "execution")
ALLOWED_DECISIONS = {"adjust_score", "abandon"}
EPSILON = 0.0001


def read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def score_average(scores: Any) -> float:
    if not isinstance(scores, dict):
        raise ValueError("scores 必须是对象")
    values: list[float] = []
    for key in SCORE_KEYS:
        try:
            values.append(float(scores.get(key)))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"分数 {key} 不是数字: {scores.get(key)!r}") from exc
    return sum(values) / len(values)


def _score_map(value: Any, label: str, errors: list[str]) -> dict[str, float]:
    if not isinstance(value, dict):
        errors.append(f"{label} 必须是包含五维分数的对象")
        return {}
    result: dict[str, float] = {}
    for key in SCORE_KEYS:
        try:
            result[key] = float(value.get(key))
        except (TypeError, ValueError):
            errors.append(f"{label}.{key} 不是数字: {value.get(key)!r}")
    return result


def _same_number(left: float, right: float) -> bool:
    return abs(left - right) <= EPSILON


def review_path(project_dir: Path, round_no: int) -> Path:
    return project_dir / "checks" / "high-score" / f"r{int(round_no):02d}.json"


def validate_high_score_review(
    project_dir: Path,
    round_no: int,
    score: dict[str, Any],
) -> dict[str, Any]:
    """Validate the high-score review and return a submission-safe decision."""
    current_scores: dict[str, float] = {}
    current_errors: list[str] = []
    raw_scores = score.get("scores") if isinstance(score.get("scores"), dict) else {}
    for key in SCORE_KEYS:
        try:
            current_scores[key] = float(raw_scores.get(key) or 0)
        except (TypeError, ValueError):
            current_errors.append(f"分数 {key} 不是数字: {raw_scores.get(key)!r}")
    if current_errors:
        return {
            "ok": False,
            "required": True,
            "status": "invalid_scores",
            "terminalAbandon": False,
            "currentAverage": 0.0,
            "rawAverage": 0.0,
            "errors": current_errors,
            "path": str(review_path(project_dir, round_no)),
        }
    current_average = score_average(current_scores)

    path = review_path(project_dir, round_no)
    review = read_json(path)
    declared_raw = None
    if isinstance(review, dict):
        try:
            declared_raw = float(review.get("rawAverage"))
        except (TypeError, ValueError):
            declared_raw = None
    raw_average = declared_raw if declared_raw is not None else current_average
    required = current_average > SOLO2_MAX_AVERAGE or raw_average > SOLO2_MAX_AVERAGE
    if not required:
        return {
            "ok": True,
            "required": False,
            "status": "not_required",
            "terminalAbandon": False,
            "currentAverage": current_average,
            "rawAverage": current_average,
            "errors": [],
            "path": str(path),
        }
    if not isinstance(review, dict):
        return {
            "ok": False,
            "required": True,
            "status": "missing_review",
            "terminalAbandon": False,
            "currentAverage": current_average,
            "rawAverage": raw_average,
            "errors": [f"缺少高分解剖报告: {path}"],
            "path": str(path),
        }

    errors: list[str] = []
    if review.get("schemaVersion") != 1:
        errors.append("高分解剖报告 schemaVersion 必须为 1")
    try:
        if int(review.get("round") or 0) != int(round_no):
            errors.append(f"高分解剖报告轮次不是 {round_no}")
    except (TypeError, ValueError):
        errors.append(f"高分解剖报告轮次不是数字: {review.get('round')!r}")
    if review.get("reviewCompleted") is not True:
        errors.append("高分解剖尚未完成 reviewCompleted=true")
    maximum = review.get("platformMaximum")
    try:
        if abs(float(maximum) - SOLO2_MAX_AVERAGE) > EPSILON:
            errors.append(f"platformMaximum 必须为 {SOLO2_MAX_AVERAGE}")
    except (TypeError, ValueError):
        errors.append(f"platformMaximum 必须为 {SOLO2_MAX_AVERAGE}")

    decision = str(review.get("decision") or "").strip()
    if decision not in ALLOWED_DECISIONS:
        errors.append("decision 只能是 adjust_score 或 abandon")

    original = _score_map(review.get("originalScores"), "originalScores", errors)
    if original:
        original_average = score_average(original)
        if declared_raw is None or not _same_number(declared_raw, original_average):
            errors.append("rawAverage 必须等于 originalScores 的平均值")
        raw_average = original_average

    evidence = review.get("evidence")
    if not isinstance(evidence, list) or len([item for item in evidence if str(item).strip()]) < 2:
        errors.append("evidence 至少需要两条可回指证据")

    terminal_abandon = False
    if decision == "adjust_score":
        if review.get("realDefectsFound") is not True:
            errors.append("adjust_score 时必须 realDefectsFound=true")
        defects = review.get("defects")
        if not isinstance(defects, list) or not defects:
            errors.append("adjust_score 时必须提供至少一条真实缺陷或纰漏")
        else:
            for index, defect in enumerate(defects, start=1):
                if not isinstance(defect, dict):
                    errors.append(f"defects[{index}] 不是对象")
                    continue
                dimension = str(defect.get("dimension") or "").strip()
                if dimension not in SCORE_KEYS:
                    errors.append(f"defects[{index}].dimension 非法: {dimension}")
                for field in ("title", "evidence", "location", "impact"):
                    if not str(defect.get(field) or "").strip():
                        errors.append(f"defects[{index}].{field} 不能为空")
        adjusted = _score_map(review.get("adjustedScores"), "adjustedScores", errors)
        if adjusted and current_scores:
            for key in SCORE_KEYS:
                if not _same_number(adjusted.get(key, -1), current_scores.get(key, -2)):
                    errors.append(f"当前 round-score.json 的 {key} 与 adjustedScores 不一致")
        if original and adjusted:
            increased = [
                key
                for key in SCORE_KEYS
                if adjusted.get(key, 0) > original.get(key, 0) + EPSILON
            ]
            decreased = [
                key
                for key in SCORE_KEYS
                if adjusted.get(key, 0) < original.get(key, 0) - EPSILON
            ]
            if increased:
                errors.append("adjustedScores 不允许高于 originalScores: " + "，".join(increased))
            if not decreased:
                errors.append("adjust_score 时至少一个维度必须真实下调")
        current_average = score_average(current_scores)
        if current_average > SOLO2_MAX_AVERAGE + EPSILON:
            errors.append(
                f"当前均分 {current_average:.2f} 仍大于 {SOLO2_MAX_AVERAGE}，尚未完成降分"
            )
        try:
            final_average = float(review.get("finalAverage"))
        except (TypeError, ValueError):
            final_average = -1.0
        if not _same_number(final_average, current_average) or final_average > SOLO2_MAX_AVERAGE + EPSILON:
            errors.append("finalAverage 必须等于当前均分且不得大于 4.2")
    elif decision == "abandon":
        terminal_abandon = True
        if review.get("realDefectsFound") is not False:
            errors.append("abandon 时必须 realDefectsFound=false")
        defects = review.get("defects")
        if defects not in (None, []):
            errors.append("abandon 时 defects 必须为空，不能伪造扣分点")
        checks = review.get("exhaustiveChecks")
        if not isinstance(checks, list) or len(checks) < 2:
            errors.append("abandon 时 exhaustiveChecks 至少需要两项已执行的完整性核验")
        else:
            for index, item in enumerate(checks, start=1):
                if not isinstance(item, dict):
                    errors.append(f"exhaustiveChecks[{index}] 不是对象")
                    continue
                for field in ("check", "evidence", "outcome"):
                    if not str(item.get(field) or "").strip():
                        errors.append(f"exhaustiveChecks[{index}].{field} 不能为空")
        if current_average <= SOLO2_MAX_AVERAGE + EPSILON:
            errors.append("abandon 只适用于当前仍大于 4.2 且没有真实扣分点的情况")
        if original and not _same_number(current_average, score_average(original)):
            errors.append("abandon 时当前分数必须与 originalScores 一致")

    return {
        "ok": not errors,
        "required": True,
        "status": "abandon" if terminal_abandon and not errors else "adjust_score" if decision == "adjust_score" and not errors else "invalid",
        "terminalAbandon": terminal_abandon and not errors,
        "decision": decision,
        "platformMaximum": SOLO2_MAX_AVERAGE,
        "currentAverage": current_average,
        "rawAverage": raw_average,
        "errors": errors,
        "path": str(path),
        "report": review,
    }

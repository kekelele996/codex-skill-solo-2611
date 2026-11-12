#!/usr/bin/env python3
"""Validate the evidence gate for high-score Solo round scoring."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate evidence gate for scores of 4 or 5")
    parser.add_argument("score", type=Path)
    parser.add_argument("--require-auth", action="store_true")
    parser.add_argument("--require-state-transition", action="store_true")
    parser.add_argument("--require-concurrency", action="store_true")
    parser.add_argument("--require-rollback", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    score = json.loads(args.score.read_text(encoding="utf-8"))
    scores = score.get("scores", {})
    gate = score.get("gate", {})
    values: dict[str, float] = {}
    errors: list[str] = []
    for key, raw_value in scores.items():
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            errors.append(f"分数 {key} 不是数字: {raw_value!r}")
            continue
        if value < 1 or value > 5 or int(value) != value:
            errors.append(f"分数 {key} 必须是 1 到 5 的整数: {raw_value!r}")
            continue
        values[str(key)] = value

    if values:
        average = sum(values.values()) / len(values)
        if average > 4.2 + 0.0001:
            errors.append(
                f"五维均分 {average:.2f} 大于 SOLO2 上限 4.2；"
                "先完成高分解剖并真实降分，或放弃该条数据，不能直接提交"
            )

    high_score_keys = sorted(key for key, value in values.items() if value >= 4)
    full_score_keys = sorted(key for key, value in values.items() if value == 5)
    has_high_score = bool(high_score_keys)
    has_full_score = bool(full_score_keys)
    checks: list[tuple[str, bool, str]] = []

    def add(name: str, required: bool, reason_key: str, *, allow_na: bool = True) -> None:
        if not required:
            return
        value = gate.get(name) is True
        reason = str(gate.get(reason_key, "")).strip()
        ok = value or (allow_na and bool(reason))
        checks.append((name, ok, reason))
        if not ok:
            suffix = "，且没有不适用说明" if allow_na else ""
            errors.append(f"存在四分或五分但 {name} 未验证{suffix}")

    add("counterexamplesReviewed", has_high_score, "counterexamplesNotApplicableReason", allow_na=False)
    add("criticalPathsChecked", has_high_score, "criticalPathsNotApplicableReason", allow_na=False)
    add("adversarialChecksPassed", has_high_score, "adversarialChecksNotApplicableReason", allow_na=False)

    raw_evidence = score.get("evidence") or gate.get("supportingEvidence") or []
    evidence = [str(item).strip() for item in raw_evidence if str(item).strip()] if isinstance(raw_evidence, list) else []
    evidence_ok = len(evidence) >= 2
    checks.append(("supportingEvidence", evidence_ok, f"{len(evidence)} 条"))
    if has_high_score and not evidence_ok:
        errors.append("存在四分或五分但 evidence 少于两条，不能证明独立复跑和相关反例")

    justification = str(gate.get("highScoreJustification", "")).strip()
    checks.append(("highScoreJustification", bool(justification), justification))
    if has_high_score and not justification:
        errors.append("存在四分或五分但 highScoreJustification 为空")

    # 五分必须逐项说明权限、状态流转、并发和回滚是否适用；不适用也要写理由。
    add("authBoundaryChecked", args.require_auth or has_full_score, "authBoundaryNotApplicableReason")
    add("stateTransitionChecked", args.require_state_transition or has_full_score, "stateTransitionNotApplicableReason")
    add("concurrencyChecked", args.require_concurrency or has_full_score, "concurrencyNotApplicableReason")
    add("rollbackChecked", args.require_rollback or has_full_score, "rollbackNotApplicableReason")

    result = {
        "ok": not errors,
        "highScoreKeys": high_score_keys,
        "fullScoreKeys": full_score_keys,
        "hasHighScore": has_high_score,
        "hasFullScore": has_full_score,
        "errors": errors,
        "checks": [{"name": name, "ok": ok, "reason": reason} for name, ok, reason in checks],
        "path": str(args.score.resolve()),
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("PASS" if result["ok"] else "FAIL")
        for error in errors:
            print(f"- {error}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""提交前审核：检查交付表串题、轨迹、时间格式、快照链接、必填字段与评语格式。

用法：
  python3 audit-delivery.py --delivery <交付表.xlsx> [--sheet 交付记录] [--json]

检查项：
1. 串题：每轮的 TurnID/PromptID 与 User Prompt 必须能在轨迹 JSONL 中找到对应 user 消息且文本逐字一致
2. 轨迹：轨迹文件路径存在、非空、绝对路径
3. 时间：提交时间为可读 ISO 时间，不是 Excel 日期序列号
4. 快照：初始环境快照是 GitHub 四十位 commit 永久链接
5. 必填：交付表关键字段非空；操作系统必须是 MacOS/Linux 或 Windows
6. 评语：五维描述不少于六十字、不含括号引号反引号、不含阿拉伯数字；每条必须包含具体操作或工具调用，并引用文件名、函数名或命令；五维之间无长公共片段（软提示，供人工复核是否重复归因）
7. ra-人话：提示词和五维评语重新扫描硬禁表达，并核对审核 JSON 的版本、哈希和未解决问题
8. 评语证据：每维每句事实必须绑定轨迹、文件行或源码覆盖范围；缺失、错位、极性相反一律阻断
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from openpyxl import load_workbook

from human_writing import (
    DIM_KEYS,
    comment_evidence_issues,
    comments_to_text,
    load_review,
    non_perfect_deduction_issues,
    round_comment_evidence_path,
    round_review_path,
    scan_text,
    validate_comment_consistency,
    validate_review,
)

GITHUB_COMMIT_RE = re.compile(r"^https://github\.com/[^/\s]+/[^/\s]+/commit/[0-9a-fA-F]{40}/?$")
FORBIDDEN = set('()[]{}"\'`（）【】「」『』“”‘’')
ISO_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
ARABIC_NUM_RE = re.compile(r"[0-9]")

DIM_COLS = ["交付完整性 - 描述", "指令遵循 - 描述", "任务规划 - 描述", "推理能力 - 描述", "执行能力 - 描述"]
DIM_SCORE_COLS = ["交付完整性", "指令遵循", "任务规划", "推理能力", "执行能力"]


def longest_common(text_a: str, text_b: str) -> str:
    """返回两个字符串的最长公共子串（用于提示重复归因）。"""
    if not text_a or not text_b:
        return ""
    shorter, longer = (text_a, text_b) if len(text_a) <= len(text_b) else (text_b, text_a)
    best = ""
    # 简单动态规划，文本较短，开销可接受
    prev = [0] * (len(shorter) + 1)
    for i in range(1, len(longer) + 1):
        curr = [0] * (len(shorter) + 1)
        for j in range(1, len(shorter) + 1):
            if longer[i - 1] == shorter[j - 1]:
                curr[j] = prev[j - 1] + 1
                if curr[j] > len(best):
                    best = longer[i - curr[j]:i]
        prev = curr
    return best


def find_user_messages(trace_path: Path) -> list[tuple[str, str]]:
    """返回轨迹中 (promptId, promptText) 列表，仅统计真正的用户输入。"""
    users: list[tuple[str, str]] = []
    with trace_path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("type") != "user":
                continue
            msg = row.get("message", {})
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, str) or not content.strip():
                continue
            text = content.strip()
            if text.startswith("[Request interrupted"):
                continue
            pid = str(row.get("promptId") or row.get("prompt_id") or "")
            users.append((pid, text))
    return users


def audit(
    delivery: Path,
    sheet: str,
    human_review_root: Path | None = None,
    require_human_review: bool = True,
    round_filter: set[int] | None = None,
) -> dict:
    issues: list[str] = []
    ok_items: list[str] = []
    wb = load_workbook(delivery, data_only=True, read_only=True)
    if sheet not in wb.sheetnames and sheet == "数据表" and "交付记录" in wb.sheetnames:
        sheet = "交付记录"
    if sheet not in wb.sheetnames:
        return {"status": "failed", "issues": [f"工作表不存在: {sheet}"]}
    ws = wb[sheet]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return {"status": "failed", "issues": ["交付表为空"]}
    headers = [str(v or "").strip() for v in rows[0]]
    review_root = human_review_root or (delivery.parent.parent / "checks" / "human-writing")
    project_root = delivery.expanduser().resolve().parent.parent

    def col(name: str) -> int:
        return headers.index(name) if name in headers else -1

    for r_idx, values in enumerate(rows[1:], start=2):
        def cell(name: str):
            c = col(name)
            return values[c] if 0 <= c < len(values) else None

        round_no = cell("轮次")
        try:
            current_round = int(round_no)
        except (TypeError, ValueError):
            current_round = -1
        if round_filter is not None and current_round not in round_filter:
            continue
        session = str(cell("SessionID") or "").strip()
        turn = str(cell("TurnID/PromptID") or "").strip()
        prompt = str(cell("User Prompt") or "").strip()
        trace = str(cell("轨迹文件") or "").strip()
        snapshot = str(cell("初始环境快照") or "").strip()
        submit_time = cell("提交时间")

        # 1. 必填字段
        for name in ["User Prompt", "SessionID", "TurnID/PromptID", "轮次", "初始环境快照", "提交地址", "轨迹文件", "任务类型", "任务难度", "Harness", "Harness 版本"]:
            if str(cell(name) or "").strip() == "":
                issues.append(f"第{round_no}轮 字段为空: {name}")

        # 1b. 难度政策：所有轮次只允许困难或地狱。
        difficulty = str(cell("任务难度") or "").strip()
        if difficulty and difficulty not in ("困难", "地狱"):
            if difficulty == "中等":
                issues.append(
                    f"第{round_no}轮 任务难度为中等，当前政策要求困难或以上；提交前必须先迁移为困难"
                )
            elif difficulty == "简单":
                issues.append(f"第{round_no}轮 任务难度为简单，当前禁止困难以下难度")
            else:
                issues.append(f"第{round_no}轮 任务难度不是允许值困难或地狱: {difficulty}")
        elif difficulty:
            ok_items.append(f"第{round_no}轮 任务难度为 {difficulty}，符合困难或以上要求")

        # 1c. SOLO2 不接受均分大于 4.2，提交表必须是已经完成高分解剖后的最终分数。
        numeric_scores: list[float] = []
        for name in DIM_SCORE_COLS:
            value = cell(name)
            try:
                numeric_scores.append(float(value))
            except (TypeError, ValueError):
                numeric_scores = []
                break
        if numeric_scores:
            average = sum(numeric_scores) / len(numeric_scores)
            if average > 4.2 + 0.0001:
                issues.append(
                    f"第{round_no}轮 均分为 {average:.2f}，SOLO2 不接受大于 4.2 的记录；"
                    "必须先完成高分解剖并真实降分，或放弃该条数据"
                )
            else:
                ok_items.append(f"第{round_no}轮 均分为 {average:.2f}，符合 SOLO2 上限")

        # 2. 快照
        if snapshot and not GITHUB_COMMIT_RE.match(snapshot):
            issues.append(f"第{round_no}轮 初始环境快照不是 GitHub 四十位 commit 永久链接: {snapshot[:60]}")
        elif snapshot:
            ok_items.append(f"第{round_no}轮 初始环境快照为 GitHub commit 永久链接")

        # 2b. 操作系统枚举
        os_value = str(cell("操作系统") or "").strip()
        if os_value and os_value not in ("MacOS/Linux", "Windows"):
            issues.append(f"第{round_no}轮 操作系统不是枚举值 MacOS/Linux 或 Windows: {os_value}")

        # 2c. 语言/框架只写四个主要项
        language = str(cell("语言/框架") or "").strip()
        if language:
            language_items = [item.strip() for item in re.split(r"[,，、;；/]", language) if item.strip()]
            if len(language_items) > 4:
                issues.append(
                    f"第{round_no}轮 语言/框架写了 {len(language_items)} 项，只保留四个主要项: {language[:80]}"
                )
            else:
                ok_items.append(f"第{round_no}轮 语言/框架为 {len(language_items)} 项")

        # 3. 轨迹
        trace_path = Path(trace).expanduser() if trace else None
        if not trace:
            issues.append(f"第{round_no}轮 轨迹文件为空")
        elif not trace_path.is_file():
            issues.append(f"第{round_no}轮 轨迹文件不存在: {trace}")
        elif trace_path.stat().st_size == 0:
            issues.append(f"第{round_no}轮 轨迹文件为空文件: {trace}")
        elif not trace_path.is_absolute():
            issues.append(f"第{round_no}轮 轨迹文件不是绝对路径: {trace}")
        else:
            ok_items.append(f"第{round_no}轮 轨迹文件存在，大小 {trace_path.stat().st_size} 字节")

        # 4. 串题
        if trace_path and trace_path.is_file():
            users = find_user_messages(trace_path)
            matched = [p for pid, p in users if pid == turn and p == prompt]
            if not matched:
                pids = [pid for pid, _ in users]
                issues.append(
                    f"第{round_no}轮 串题风险：轨迹中未找到 turn_id={turn[:8]} 且文本逐字一致的 user 消息；轨迹内 promptId={pids[:3]}"
                )
            else:
                ok_items.append(f"第{round_no}轮 轨迹中找到匹配 user 消息，串题检查通过")

        # 5. 时间
        if submit_time is None or str(submit_time).strip() == "":
            issues.append(f"第{round_no}轮 提交时间为空")
        elif isinstance(submit_time, str) and not ISO_TIME_RE.match(submit_time.strip()):
            issues.append(f"第{round_no}轮 提交时间不是可读 ISO 时间: {submit_time}")
        elif not isinstance(submit_time, str):
            issues.append(f"第{round_no}轮 提交时间是日期/数字类型，建议写成 ISO 文本: {submit_time}")
        else:
            ok_items.append(f"第{round_no}轮 提交时间为 ISO 文本")

        # 6. 评语格式
        comments = {name: str(cell(name) or "").strip() for name in DIM_COLS}
        for name, text in comments.items():
            if len(text) < 60:
                issues.append(f"第{round_no}轮 {name} 只有 {len(text)} 字，不足六十字")
            bad = sorted({ch for ch in text if ch in FORBIDDEN})
            if bad:
                issues.append(f"第{round_no}轮 {name} 出现不允许的括号或引号: {bad}")
            if ARABIC_NUM_RE.search(text):
                issues.append(f"第{round_no}轮 {name} 出现阿拉伯数字")
            issues.extend(f"第{round_no}轮 {name} {issue}" for issue in comment_evidence_issues(text, name))

        # 8. ra-人话 写作审核
        issues.extend(f"第{round_no}轮 提示词 {issue['message']}" for issue in scan_text(prompt, "prompt"))
        comment_values = {key: comments[name] for key, name in zip(DIM_KEYS, DIM_COLS)}
        score_values = {key: cell(name) for key, name in zip(DIM_KEYS, DIM_SCORE_COLS)}
        comment_values["other"] = str(cell("其他问题") or "").strip()
        issues.extend(
            f"第{round_no}轮 五维评语扣分依据 {issue}"
            for issue in non_perfect_deduction_issues(comment_values, score_values)
        )
        comment_text = comments_to_text(comment_values)
        issues.extend(f"第{round_no}轮 五维评语 {issue['message']}" for issue in scan_text(comment_text, "comments"))
        round_number = current_round
        if round_number > 0:
            evidence_path = round_comment_evidence_path(project_root, round_number)
            consistency_errors = validate_comment_consistency(
                comment_values,
                evidence_path,
                project_root,
                round_number,
                session,
                turn,
                score_values,
            )
            issues.extend(
                f"第{round_no}轮 五维评语证据 {error}" for error in consistency_errors
            )
            if not consistency_errors:
                ok_items.append(f"第{round_no}轮 五维评语通过轨迹证据一致性审核")
            targets = (
                ("prompt", prompt, round_review_path(review_root, round_number, "prompt")),
                ("comments", comment_text, round_review_path(review_root, round_number, "comments")),
            )
            for target_type, text, review_path in targets:
                label = "提示词" if target_type == "prompt" else "五维评语"
                if not review_path.is_file():
                    if require_human_review:
                        issues.append(f"第{round_no}轮 {label}缺少 ra-人话 审核记录: {review_path}")
                    continue
                try:
                    review_errors = validate_review(load_review(review_path), text, target_type)
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    issues.append(f"第{round_no}轮 {label}审核记录不可用: {exc}")
                else:
                    issues.extend(f"第{round_no}轮 {label}{error}" for error in review_errors)
                    if not review_errors:
                        ok_items.append(f"第{round_no}轮 {label}通过 ra-人话 审核")
        elif require_human_review:
            issues.append(f"第{round_no}轮 无法识别轮次，不能核对 ra-人话 审核记录")

        # 7. 五维之间长公共片段（软提示，需人工确认，不阻塞提交）
        dim_names = list(comments)
        warnings = []
        for i in range(len(dim_names)):
            for j in range(i + 1, len(dim_names)):
                common = longest_common(comments[dim_names[i]].replace(" ", ""), comments[dim_names[j]].replace(" ", ""))
                if len(common) >= 10:
                    warnings.append(
                        f"第{round_no}轮 评语可能重复归因：{dim_names[i]} 与 {dim_names[j]} 共享片段「{common}」"
                    )

    return {
        "status": "ok" if not issues else "failed",
        "issues": issues,
        "warnings": warnings,
        "passed_checks": ok_items,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="提交前审核交付表")
    parser.add_argument("--delivery", required=True, type=Path)
    parser.add_argument("--sheet", default="交付记录")
    parser.add_argument("--human-review-root", type=Path, help="ra-人话 审核 JSON 所在目录，默认取项目 checks/human-writing")
    parser.add_argument("--allow-missing-human-review", action="store_true", help="仅用于检查历史记录，不用于提交")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = audit(
        args.delivery.expanduser(),
        args.sheet,
        args.human_review_root.expanduser() if args.human_review_root else None,
        not args.allow_missing_human_review,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("PASS" if result["status"] == "ok" else "FAIL")
        for line in result.get("passed_checks", []):
            print(f"  [通过] {line}")
        for line in result.get("issues", []):
            print(f"  [问题] {line}")
        for line in result.get("warnings", []):
            print(f"  [提示] {line}")
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""校验五维评语：长度、标点、机器味和可回指证据均需合规。

文件名、函数名和可执行命令属于必需的事实证据，按原样保留其中的英文、
斜杠和点号；其余叙述仍遵守中文自然表达要求。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from human_writing import (
    comment_evidence_issues,
    comments_to_text,
    load_review,
    non_perfect_deduction_issues,
    round_comment_evidence_path,
    scan_text,
    validate_comment_consistency,
    validate_review,
)

DIMS = ('delivery', 'instruction', 'planning', 'reasoning', 'execution')
# 不允许：括号、双引号、单引号、反引号
AWKWARD_DESCRIPTIONS = ("没有临时绕路", "没有绕路")
FORBIDDEN = set('()[]{}"\'`（）【】「」『』“”‘’')


def validate(
    comments: dict,
    review_path: Path | None = None,
    evidence_path: Path | None = None,
    project_root: Path | None = None,
    expected_round: int | None = None,
    expected_session_id: str = "",
    expected_prompt_id: str = "",
    scores: dict | None = None,
) -> list[str]:
    errors: list[str] = []
    errors.extend(non_perfect_deduction_issues(comments, scores))
    for key in DIMS:
        text = str(comments.get(key, '')).strip()
        if len(text) < 60:
            errors.append(f'{key} 只有 {len(text)} 字，要求不少于 60 字')
        bad = set(ch for ch in text if ch in FORBIDDEN)
        if bad:
            errors.append(f'{key} 出现不允许的括号或引号: {sorted(bad)}')
        for phrase in AWKWARD_DESCRIPTIONS:
            if phrase in text:
                errors.append(f'{key} 包含生硬或别扭表达: {phrase}')
        errors.extend(f'{key} {issue}' for issue in comment_evidence_issues(text, key))
    canonical = comments_to_text(comments)
    errors.extend(issue["message"] for issue in scan_text(canonical, "comments"))
    if review_path is None:
        errors.append("缺少 ra-人话 评语审核记录")
    else:
        try:
            errors.extend(validate_review(load_review(review_path), canonical, "comments"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"审核记录不可用: {exc}")
    if evidence_path is not None and project_root is not None:
        errors.extend(
            validate_comment_consistency(
                comments,
                evidence_path,
                project_root,
                expected_round,
                expected_session_id,
                expected_prompt_id,
                scores,
            )
        )
    return errors


def main() -> int:
    ap = argparse.ArgumentParser(description='校验五维评语')
    ap.add_argument('score', type=Path, help='round-score.json')
    ap.add_argument('--review', required=True, type=Path, help='ra-人话 评语审核 JSON')
    ap.add_argument('--evidence', type=Path, help='评语证据索引，默认取 checks/comment-evidence/rNN.json')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()
    data = json.loads(args.score.read_text(encoding='utf-8'))
    comments = data.get('comments', data)
    scores = data.get('scores') if isinstance(data.get('scores'), dict) else None
    score_path = args.score.expanduser().resolve()
    project_root = score_path.parents[2]
    try:
        round_no = int(data.get('round'))
    except (TypeError, ValueError):
        round_no = 0
    evidence_path = (
        args.evidence.expanduser().resolve()
        if args.evidence
        else round_comment_evidence_path(project_root, round_no)
    )
    session_id = str(data.get('sessionId') or data.get('session_id') or '').strip()
    prompt_id = str(data.get('promptId') or data.get('prompt_id') or '').strip()
    errors = list(dict.fromkeys(
        validate(
            comments,
            args.review,
            evidence_path,
            project_root,
            round_no if round_no > 0 else None,
            session_id,
            prompt_id,
            scores,
        )
    ))
    result = {'ok': not errors, 'errors': errors, 'path': str(args.score.resolve())}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print('PASS' if result['ok'] else 'FAIL')
        for e in errors:
            print(f'- {e}')
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

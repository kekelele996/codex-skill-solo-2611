#!/usr/bin/env python3
"""Validate the user-facing prompt without rewriting it."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from human_writing import load_review, scan_text, validate_review

MARKDOWN = re.compile(r"(^|\n)\s{0,3}(#{1,6}|[-*+]\s|\d+[.)]\s|```)|[`*_]{2,}|\[[^\]]+\]\([^)]*\)")
PATH_OR_CODE = re.compile(r"(?:/Users/|/home/|[A-Za-z]:\\|\b(?:src|frontend|backend|internal|cmd)/[\w./-]+|\b(?:class|function|def|func|import|require)\b)")
GUIDED_FIX = re.compile(r"(?:改成|改为|修改为|换成|替换为|加上|加入|删除|删掉|加锁|改返回|使用.+修复)")
ROUND_REFERENCE = re.compile(r"(第[一二三四五六七八九十百0-9]+轮|本轮|上一轮|下一轮|首轮|轮次)")


def validate(text: str, review_path: Path | None = None) -> list[str]:
    errors: list[str] = []
    clean = text.strip()
    if not clean:
        errors.append("提示词为空")
    if len(clean) > 240:
        errors.append(f"提示词超过 240 字，当前 {len(clean)} 字")
    if MARKDOWN.search(clean):
        errors.append("包含 Markdown 格式")
    if PATH_OR_CODE.search(clean):
        errors.append("包含文件路径或代码术语")
    if GUIDED_FIX.search(clean):
        errors.append("包含引导性修复措辞")
    if ROUND_REFERENCE.search(clean):
        errors.append("包含轮次表述")
    errors.extend(issue["message"] for issue in scan_text(clean, "prompt"))
    if review_path is None:
        errors.append("缺少 ra-人话 审核记录")
    else:
        try:
            errors.extend(validate_review(load_review(review_path), clean, "prompt"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"审核记录不可用: {exc}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a plain-text Solo prompt")
    parser.add_argument("prompt", type=Path)
    parser.add_argument("--review", required=True, type=Path, help="ra-人话 提示词审核 JSON")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    text = args.prompt.read_text(encoding="utf-8")
    errors = validate(text, args.review)
    # 同一套式可能同时命中旧检查和 ra-人话，保留一次即可。
    errors = list(dict.fromkeys(errors))
    result = {"ok": not errors, "charCount": len(text.strip()), "errors": errors, "path": str(args.prompt.resolve())}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("PASS" if result["ok"] else "FAIL")
        for error in errors:
            print(f"- {error}")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""按 ra-人话 规则审核提示词或五维评语。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from human_writing import comments_to_text, load_review, scan_text, text_sha256, validate_review


def main() -> int:
    parser = argparse.ArgumentParser(description="审核 Solo 提示词或五维评语的机器味")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--text-file", type=Path)
    source.add_argument("--score-json", type=Path)
    parser.add_argument("--target-type", choices=("prompt", "comments"))
    parser.add_argument("--review", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.score_json:
        data = json.loads(args.score_json.read_text(encoding="utf-8"))
        comments = data.get("comments", data)
        text = comments_to_text(comments)
        target_type = "comments"
    else:
        text = args.text_file.read_text(encoding="utf-8")
        target_type = args.target_type or "prompt"

    errors = [issue["message"] for issue in scan_text(text, target_type)]
    if args.review:
        try:
            errors.extend(validate_review(load_review(args.review), text, target_type))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"审核记录不可用: {exc}")
    else:
        errors.append("缺少 ra-人话 审核记录")

    result = {
        "ok": not errors,
        "targetType": target_type,
        "textSha256": text_sha256(text),
        "errors": errors,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("PASS" if result["ok"] else "FAIL")
        for error in errors:
            print(f"- {error}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

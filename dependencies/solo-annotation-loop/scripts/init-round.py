#!/usr/bin/env python3
"""Create a round state file without touching the source project."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

TASK_TYPES = ["Bug修复", "0-1代码生成", "feature迭代", "代码理解", "代码重构", "工程化", "代码测试"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize a Solo round state")
    parser.add_argument("project", type=Path)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--task-type", required=True, choices=TASK_TYPES)
    parser.add_argument("--session-id", default="")
    parser.add_argument("--prompt-path", type=Path, default=None)
    parser.add_argument("--delivery", type=Path, default=None)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    project = args.project.resolve()
    if not project.is_dir():
        parser.error(f"项目目录不存在: {project}")
    if args.output.exists():
        parser.error(f"状态文件已存在，拒绝覆盖: {args.output.resolve()}")
    state = {
        "projectId": args.project_id,
        "projectPath": str(project),
        "taskType": args.task_type,
        "sessionId": args.session_id,
        "promptPath": str(args.prompt_path.resolve()) if args.prompt_path else "",
        "deliveryPath": str(args.delivery.resolve()) if args.delivery else "",
        "round": 1,
        "status": "prepared",
        "history": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Create one isolated project-usage directory for a Solo annotation task."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

TASK_TYPES = [
    "Bug修复",
    "0-1代码生成",
    "feature迭代",
    "代码理解",
    "代码重构",
    "工程化",
    "代码测试",
]
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
    "0-1代码生成": "0-1代码生成",
    "功能迭代": "feature迭代",
    "feature迭代": "feature迭代",
}


def parse_task_type(value: str | None) -> tuple[str, str]:
    if not value:
        return "", ""
    canonical = TASK_ALIASES.get(value, value)
    if canonical not in TASK_TYPES:
        raise ValueError(f"未知任务类型: {value}")
    return canonical, TASK_SLUGS[canonical]


def discover_usage_dirs(workdir: Path, project_id: str) -> list[dict[str, object]]:
    pattern = re.compile(rf"^{re.escape(project_id)}(?:-(.+))?-(\d+)$")
    rows: list[dict[str, object]] = []
    for path in workdir.iterdir():
        if not path.is_dir():
            continue
        match = pattern.match(path.name)
        if not match:
            continue
        rows.append(
            {
                "path": path,
                "slug": match.group(1) or "",
                "sequence": int(match.group(2)),
                "name": path.name,
            }
        )
    return sorted(rows, key=lambda item: int(item["sequence"]))


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a project-usage workspace")
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--task-type", default="")
    parser.add_argument("--sequence", type=int)
    parser.add_argument("--reuse", action="store_true")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--clone-source", action="store_true", help="Clone --source into the project directory as source/")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    workdir = args.workdir.resolve()
    if not workdir.is_dir():
        parser.error(f"作业目录不存在: {workdir}")
    if not args.project_id.strip():
        parser.error("项目编号不能为空")
    try:
        canonical_task, task_slug = parse_task_type(args.task_type or None)
    except ValueError as exc:
        parser.error(str(exc))

    usages = discover_usage_dirs(workdir, args.project_id)
    if args.sequence is not None:
        sequence = args.sequence
    else:
        same = [row for row in usages if row["slug"] == task_slug]
        if args.reuse and same:
            sequence = max(int(row["sequence"]) for row in same)
        elif usages:
            sequence = max(int(row["sequence"]) for row in usages) + 1
        else:
            sequence = 1
    if sequence < 1:
        parser.error("序号必须从 1 开始")

    if task_slug:
        folder_name = f"{args.project_id}-{task_slug}-{sequence}"
    else:
        folder_name = f"{args.project_id}-{sequence}"
    project_dir = workdir / folder_name

    if args.reuse:
        project_dir.mkdir(parents=True, exist_ok=True)
    else:
        try:
            project_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            parser.error(f"项目目录已存在，若为多轮续跑请加 --reuse: {project_dir}")

    directories = {
        "prompts": project_dir / "prompts",
        "rounds": project_dir / "rounds",
        "traces": project_dir / "traces",
        "delivery": project_dir / "delivery",
        "checks": project_dir / "checks",
        "humanWriting": project_dir / "checks" / "human-writing",
        "commentEvidence": project_dir / "checks" / "comment-evidence",
    }
    for path in directories.values():
        path.mkdir(parents=True, exist_ok=True)

    base_source = args.source.resolve() if args.source else None
    source_path = str(base_source) if base_source else ""
    if args.clone_source:
        if not base_source:
            parser.error("使用 --clone-source 时必须提供 --source")
        if not (base_source / ".git").exists():
            parser.error(f"--source 不是 Git 仓库: {base_source}")
        clone_path = project_dir / "source"
        if not clone_path.exists():
            subprocess.run(
                ["git", "clone", "--no-hardlinks", str(base_source), str(clone_path)],
                check=True,
            )
        source_path = str(clone_path)

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    manifest_path = project_dir / "workspace.json"
    previous: dict[str, object] = {}
    if manifest_path.exists():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = {}
    manifest = {
        **previous,
        "projectId": args.project_id,
        "taskType": canonical_task,
        "taskTypeSlug": task_slug,
        "sequence": sequence,
        "projectDir": str(project_dir),
        "sourcePath": source_path or previous.get("sourcePath", ""),
        "baseSourcePath": str(base_source) if base_source else previous.get("baseSourcePath", ""),
        "createdAt": previous.get("createdAt", now),
        "updatedAt": now,
        "paths": {
            "state": str(project_dir / "state.json"),
            "source": str(project_dir / "source"),
            "profile": str(project_dir / "project-profile.json"),
            "prompts": str(directories["prompts"]),
            "rounds": str(directories["rounds"]),
            "traces": str(directories["traces"]),
            "delivery": str(directories["delivery"]),
            "checks": str(directories["checks"]),
            "humanWriting": str(directories["humanWriting"]),
        },
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    result = {
        "status": "ready",
        "projectId": args.project_id,
        "taskType": canonical_task,
        "taskTypeSlug": task_slug,
        "sequence": sequence,
        "projectDir": str(project_dir),
        "workspaceManifest": str(manifest_path),
        "paths": manifest["paths"],
        "existingUsages": [
            {"name": row["name"], "sequence": row["sequence"], "slug": row["slug"]}
            for row in usages
        ],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

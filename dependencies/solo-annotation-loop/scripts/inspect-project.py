#!/usr/bin/env python3
"""Build a factual project profile from a local repository."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


TASK_TYPES = {"Bug修复", "0-1代码生成", "feature迭代", "代码理解", "代码重构", "工程化", "代码测试"}
TEXT_FILES = {"README.md", "go.mod", "package.json", "pyproject.toml", "Cargo.toml", "pom.xml", "Dockerfile", "docker-compose.yml"}
SKIP_DIRS = {".git", "node_modules", "vendor", "dist", "build", ".venv", "__pycache__"}


def project_files(root: Path):
    for path in root.rglob("*"):
        if path.is_file() and not any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            yield path


def read_text(path: Path, limit: int = 12000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def detect_stack(root: Path) -> list[str]:
    files = list(project_files(root))
    names = {p.name for p in files}
    stacks: list[str] = []
    if "go.mod" in names:
        stacks.append("Go")
        go_mod = read_text(next((p for p in files if p.name == "go.mod"), root / "go.mod"))
        for lib, label in (("gin-gonic/gin", "Gin"), ("gorm.io/gorm", "GORM")):
            if lib in go_mod:
                stacks.append(label)
    if "package.json" in names:
        stacks.append("Node.js")
        package_text = "\n".join(read_text(p) for p in files if p.name == "package.json")
        for lib, label in (("vue", "Vue"), ("react", "React"), ("typescript", "TypeScript"), ("vite", "Vite"), ("pinia", "Pinia")):
            if re.search(rf'"[^"\n]*{re.escape(lib)}[^"\n]*"', package_text, re.I):
                stacks.append(label)
    if "pyproject.toml" in names or "requirements.txt" in names:
        stacks.append("Python")
    if "Cargo.toml" in names:
        stacks.append("Rust")
    if "docker-compose.yml" in names or "docker-compose.yaml" in names:
        stacks.append("Docker Compose")
    return list(dict.fromkeys(stacks))


def business_domain(root: Path) -> str:
    """从 README/说明文件的首个一级标题提取业务领域，无则留空。"""
    for name in ("README.md", "README", "readme.md"):
        path = root / name
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[:40]:
            stripped = line.strip()
            if stripped.startswith("# ") and not stripped.startswith("## "):
                return stripped[2:].strip()
            if stripped and not stripped.startswith("#"):
                break
    return ""


def entrypoints(root: Path) -> list[str]:
    candidates = []
    patterns = ("cmd/*/main.go", "main.go", "src/main.*", "src/index.*", "app.py", "manage.py", "Dockerfile", "docker-compose.yml")
    for pattern in patterns:
        for path in root.glob(pattern):
            if path.is_file():
                candidates.append(str(path.relative_to(root)))
    return list(dict.fromkeys(candidates))[:20]


def commands(root: Path) -> tuple[list[str], list[str]]:
    tests: list[str] = []
    builds: list[str] = []
    if (root / "go.mod").exists():
        tests.append("go test ./...")
        builds.append("go build ./...")
    packages = [p for p in project_files(root) if p.name == "package.json"]
    for package in packages:
        try:
            scripts = json.loads(read_text(package)).get("scripts", {})
        except json.JSONDecodeError:
            scripts = {}
        prefix = package.parent.relative_to(root).as_posix()
        npm_prefix = f"npm --prefix {prefix} " if prefix != "." else "npm "
        if "test" in scripts:
            tests.append(f"{npm_prefix}test")
        if "build" in scripts:
            builds.append(f"{npm_prefix}run build")
    return tests, builds


def candidate_areas(root: Path) -> list[dict[str, str]]:
    result = []
    for path in sorted(root.iterdir()):
        if path.name.startswith(".") or path.name in {"node_modules", "vendor", "dist", "build"}:
            continue
        if path.is_dir():
            files = sum(1 for p in project_files(path))
            if files:
                result.append({"path": str(path.relative_to(root)), "fileCount": str(files)})
    return result[:30]


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect a project and write a factual profile")
    parser.add_argument("project", type=Path)
    parser.add_argument("--project-id", default="")
    parser.add_argument("--task-type", choices=sorted(TASK_TYPES), default="")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.project.resolve()
    if not root.is_dir():
        parser.error(f"项目目录不存在: {root}")
    tests, builds = commands(root)
    profile = {
        "projectId": args.project_id or root.name,
        "repoPath": str(root),
        "taskType": args.task_type,
        "businessDomain": business_domain(root),
        "languageFramework": detect_stack(root),
        "entrypoints": entrypoints(root),
        "testCommands": tests,
        "buildCommands": builds,
        "reproducibility": "已容器化，可一键起环境" if any((root / n).exists() for n in ("docker-compose.yml", "docker-compose.yaml")) else "",
        "candidateAreas": candidate_areas(root),
        "evidence": [str(p.relative_to(root)) for p in sorted(project_files(root)) if p.name in TEXT_FILES][:30],
        "status": "ready" if detect_stack(root) and entrypoints(root) else "information_needed",
    }
    text = json.dumps(profile, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Compose a short prompt from a project profile and explicit task facts.

提示词四条硬规则（由 human_writing.prompt_design_issues 强制）：
不写端口配置、不约束技术栈、不指定 Docker 或容器方案、不用「用户/使用者」主语句式。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from human_writing import prompt_design_issues

STRATEGIES = {
    "Bug修复": "请排查{area}出现的{symptom}，在{scenario}下会{bad}，结果应{expected}。完成后验证问题不再复现且原有功能不受影响。",
    "0-1代码生成": "请做一个{product}，需要{features}，并能在本地运行。完成后验证主要流程可用，并补充必要的错误处理。",
    "feature迭代": "现有{product}已经支持{existing}，现在增加{feature}：{flow}，同时保留旧功能和原有数据。完成后验证新旧流程都能正常使用。",
    "代码理解": "请阅读{area}并说明它的职责、调用关系、数据流、关键边界和潜在风险。请以当前代码为准，指出依据，不修改项目文件。",
    "代码重构": "{area}存在{smell}，请围绕{goal}做重构，保持现有接口和外部行为不变。完成后运行原有测试，并说明结构变化和验证结果。",
    "工程化": "请处理项目的{problem}，让{target}可以稳定完成。保持现有业务代码不变，完成后实际运行构建或部署检查，并说明关键配置。",
    "代码测试": "请为{area}补充测试，覆盖{cases}和失败场景，沿用项目现有测试方式。测试应能稳定复现问题或保护行为，完成后运行并说明结果。",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Draft a task-type-specific prompt")
    parser.add_argument("--task-type", required=True, choices=sorted(STRATEGIES))
    parser.add_argument("--facts", required=True, type=Path, help="JSON object with strategy placeholders")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    facts = json.loads(args.facts.read_text(encoding="utf-8"))
    missing = [key for key, value in facts.items() if value is None or str(value).strip() == ""]
    if missing:
        raise SystemExit(f"事实字段为空，拒绝生成带占位符的提示词: {', '.join(missing)}")
    try:
        prompt = STRATEGIES[args.task_type].format(**facts).strip()
    except KeyError as exc:
        raise SystemExit(f"缺少事实字段: {exc.args[0]}") from exc
    if len(prompt) > 240:
        raise SystemExit(f"提示词超过 240 字，当前 {len(prompt)} 字，已阻止输出")
    violations = prompt_design_issues(prompt)
    if violations:
        detail = "；".join(f"{item['kind']}: {item['match']}" for item in violations)
        raise SystemExit(f"提示词命中硬规则（{detail}），请改写事实字段后重新生成")
    result = {"taskType": args.task_type, "projectFacts": facts, "promptText": prompt, "charCount": len(prompt), "status": "draft"}
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

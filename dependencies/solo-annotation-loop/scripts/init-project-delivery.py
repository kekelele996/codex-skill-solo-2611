#!/usr/bin/env python3
"""Create a project-local delivery workbook from a 28-column template."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from openpyxl import load_workbook


def header_row(ws) -> int:
    for row in range(1, min(ws.max_row, 20) + 1):
        values = [str(ws.cell(row, col).value or "") for col in range(1, ws.max_column + 1)]
        if "SessionID" in values and "TurnID/PromptID" in values:
            return row
    raise SystemExit("模板中未找到 SessionID 和 TurnID/PromptID 表头")


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a project-local delivery workbook")
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    template = args.template.resolve()
    output = args.output.resolve()
    if not template.is_file():
        raise SystemExit(f"模板不存在: {template}")
    if output.exists() and not args.force:
        raise SystemExit(f"输出已存在，拒绝覆盖: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    wb = load_workbook(template, data_only=False)
    if not wb.worksheets:
        raise SystemExit("模板没有工作表")
    ws = wb.worksheets[0]
    header = header_row(ws)
    if ws.max_row > header:
        ws.delete_rows(header + 1, ws.max_row - header)
    wb.save(output)

    check = load_workbook(output, data_only=False)
    check_ws = check.worksheets[0]
    result = {
        "status": "ready",
        "template": str(template),
        "output": str(output),
        "sheet": check_ws.title,
        "headerRow": header,
        "maxRow": check_ws.max_row,
        "maxColumn": check_ws.max_column,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

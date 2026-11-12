"""官方表头的本地 Excel 交付表。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from spec import COLUMN_GROUPS, SUBMIT_COLUMNS

INTERNAL_COLUMNS = [
    "任务目录", "运行模式", "实际尝试次数", "初始快照 SHA", "产物快照 SHA", "远端", "轨迹目录",
    "子代理轨迹数", "录屏时长(秒)", "录屏大小(MB)", "录屏采集方式", "录屏窗口证据",
    "Feedback 检查警告", "导出时间",
]


def write_workbook(path: Path, rows: list[dict[str, Any]], internal: list[dict[str, Any]]) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "提交表"
    head_fill = PatternFill("solid", fgColor="DDEBF7")
    group_fill = PatternFill("solid", fgColor="BDD7EE")
    col = 1
    for name, span in COLUMN_GROUPS:
        ws.cell(row=1, column=col, value=name).fill = group_fill
        ws.cell(row=1, column=col).font = Font(bold=True)
        ws.cell(row=1, column=col).alignment = Alignment(horizontal="center")
        if span > 1:
            ws.merge_cells(start_row=1, start_column=col, end_row=1, end_column=col + span - 1)
        col += span
    for idx, name in enumerate(SUBMIT_COLUMNS, 1):
        cell = ws.cell(row=2, column=idx, value=name)
        cell.font = Font(bold=True)
        cell.fill = head_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    wide = {"User Prompt": 60, "Feedback": 70, "轨迹文件": 40, "运行录屏": 40, "初始环境快照": 44, "产物快照": 44}
    for idx, name in enumerate(SUBMIT_COLUMNS, 1):
        ws.column_dimensions[get_column_letter(idx)].width = wide.get(name, 16)
    for r, row in enumerate(rows, 3):
        for c, name in enumerate(SUBMIT_COLUMNS, 1):
            value = row.get(name, "")
            cell = ws.cell(row=r, column=c, value=value if value != "" else None)
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    ws.freeze_panes = "B3"

    ws2 = wb.create_sheet("内部记录")
    for idx, name in enumerate(INTERNAL_COLUMNS, 1):
        cell = ws2.cell(row=1, column=idx, value=name)
        cell.font = Font(bold=True)
        cell.fill = head_fill
        ws2.column_dimensions[get_column_letter(idx)].width = 24
    for r, row in enumerate(internal, 2):
        for c, name in enumerate(INTERNAL_COLUMNS, 1):
            ws2.cell(row=r, column=c, value=row.get(name, "")).alignment = Alignment(vertical="top", wrap_text=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def read_back(path: Path) -> list[dict[str, Any]]:
    from openpyxl import load_workbook

    ws = load_workbook(path, read_only=True)["提交表"]
    rows = list(ws.iter_rows(values_only=True))
    header = list(rows[1])
    if header != SUBMIT_COLUMNS:
        raise ValueError(f"表头与官方模板不一致: {header}")
    return [dict(zip(header, r)) for r in rows[2:]]

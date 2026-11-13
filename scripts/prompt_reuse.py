"""Read workbench prompt history for the current engineering."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_WORKBENCH_URL = "http://127.0.0.1:8099"


class WorkbenchPromptHistoryError(RuntimeError):
    def __init__(self, message: str, *, managed: bool = False, code: str = "") -> None:
        super().__init__(message)
        self.managed = managed
        self.code = code


def read_task_marker(task_root: Path) -> dict[str, Any]:
    path = task_root / "monitor" / "workbench.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def workbench_url(task_root: Path) -> str:
    marker = read_task_marker(task_root)
    return str(
        marker.get("apiBase")
        or os.environ.get("SOLO2611_WORKBENCH_URL")
        or DEFAULT_WORKBENCH_URL
    ).rstrip("/")


def query_prompt_history(
    task_root: Path,
    *,
    candidate_text: str = "",
    limit: int = 500,
    timeout: float = 5,
) -> dict[str, Any]:
    task_root = task_root.resolve()
    marker = read_task_marker(task_root)
    managed = bool(marker)
    payload: dict[str, Any] = {
        "text": candidate_text[:5000],
        "limit": max(1, min(limit, 500)),
    }
    if marker.get("projectId") and marker.get("engineeringId"):
        payload.update(
            {
                "project_id": str(marker.get("projectId") or ""),
                "engineering_id": str(marker.get("engineeringId") or ""),
                "exclude_task_id": str(marker.get("taskId") or ""),
            }
        )
    else:
        payload["task_root"] = str(task_root)
    request = urllib.request.Request(
        f"{workbench_url(task_root)}/api/source/prompts/dedupe",
        data=json.dumps(payload, ensure_ascii=False).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            error = json.loads(raw).get("error") or {}
        except json.JSONDecodeError:
            error = {}
        raise WorkbenchPromptHistoryError(
            str(error.get("message") or f"工作台历史查询失败：HTTP {exc.code}"),
            managed=managed,
            code=str(error.get("code") or f"HTTP_{exc.code}"),
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise WorkbenchPromptHistoryError(
            f"无法读取工作台提示词历史：{exc}",
            managed=managed,
        ) from exc
    if not isinstance(data, dict) or data.get("ok") is not True:
        raise WorkbenchPromptHistoryError(
            "工作台返回了无法识别的提示词历史",
            managed=managed,
        )
    return data


def history_records(response: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in response.get("items") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        records.append(
            {
                "taskRoot": str(
                    item.get("taskRoot")
                    or item.get("taskName")
                    or item.get("taskId")
                    or item.get("recordId")
                    or "工作台历史题面"
                ),
                "sha256": str(item.get("sha256") or ""),
                "date": str(item.get("usedAt") or "")[:10],
                "text": text,
                "taskName": str(item.get("taskName") or ""),
                "engineeringName": str(item.get("engineeringName") or ""),
                "similarity": float(item.get("ratio") or 0),
                "commonFragment": str(item.get("commonFragment") or ""),
                "level": str(item.get("level") or "history"),
            }
        )
    return records

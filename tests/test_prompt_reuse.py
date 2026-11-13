"""Workbench prompt history integration without a live workbench."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="solo2611-prompt-reuse-"))
os.environ.setdefault("CODEX_HOME", str(TMP / "codex"))
os.environ.setdefault("SOLO2611_CONFIG", str(TMP / "codex" / "solo-2611" / "config.json"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import prompt_reuse  # noqa: E402
import solo2611 as cli  # noqa: E402


def _task_root(tmp_path: Path) -> Path:
    root = tmp_path / "task"
    (root / "monitor").mkdir(parents=True)
    (root / "monitor" / "workbench.json").write_text(
        json.dumps(
            {
                "taskId": "current",
                "projectId": "project-a",
                "engineeringId": "eng-1",
                "engineeringName": "GB-544",
                "apiBase": "http://127.0.0.1:8099",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return root


def test_query_uses_workbench_marker_scope(monkeypatch) -> None:
    root = _task_root(Path(tempfile.mkdtemp(prefix="solo2611-reuse-query-")))
    seen: dict[str, object] = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        seen["payload"] = json.loads(request.data)
        return Response(b'{"ok":true,"items":[]}')

    monkeypatch.setattr(prompt_reuse.urllib.request, "urlopen", fake_urlopen)
    response = prompt_reuse.query_prompt_history(root, candidate_text="候选题面")

    assert response["ok"] is True
    assert seen["url"] == "http://127.0.0.1:8099/api/source/prompts/dedupe"
    assert seen["payload"] == {
        "text": "候选题面",
        "limit": 500,
        "project_id": "project-a",
        "engineering_id": "eng-1",
        "exclude_task_id": "current",
    }


def test_load_history_merges_local_and_workbench(monkeypatch, tmp_path: Path) -> None:
    root = _task_root(tmp_path)
    local_history = tmp_path / "local-history.jsonl"
    local_history.write_text(
        json.dumps(
            {
                "taskRoot": "/used/local",
                "sha256": "local-sha",
                "date": "2026-11-12",
                "text": "本地记录里的旧题面。",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "PROMPT_HISTORY", local_history)
    monkeypatch.setattr(
        prompt_reuse,
        "query_prompt_history",
        lambda *_args, **_kwargs: {
            "ok": True,
            "projectId": "project-a",
            "projectName": "项目 A",
            "engineeringId": "eng-1",
            "engineeringName": "GB-544",
            "status": "review",
            "summary": {"history": 1, "duplicate": 0, "review": 1},
            "items": [
                {
                    "recordId": "task:old",
                    "taskId": "old",
                    "taskName": "旧任务",
                    "text": "工作台记录里的同工程题面。",
                    "usedAt": "2026-11-13T10:00:00+08:00",
                    "sha256": "remote-sha",
                    "level": "review",
                    "ratio": 0.6,
                }
            ],
        },
    )

    history, meta = cli._load_history(root, candidate_text="候选题面")

    assert meta["ok"] is True
    assert meta["source"] == "workbench+local"
    assert meta["engineeringName"] == "GB-544"
    assert len(history) == 2
    assert {item["text"] for item in history} == {
        "本地记录里的旧题面。",
        "工作台记录里的同工程题面。",
    }


def test_managed_history_query_failure_blocks_prompt(monkeypatch, tmp_path: Path) -> None:
    root = _task_root(tmp_path)
    monkeypatch.setattr(cli, "PROMPT_HISTORY", tmp_path / "missing.jsonl")

    def fail(*_args, **_kwargs):
        raise prompt_reuse.WorkbenchPromptHistoryError(
            "工作台离线",
            managed=True,
            code="WORKBENCH_OFFLINE",
        )

    monkeypatch.setattr(prompt_reuse, "query_prompt_history", fail)
    history, meta = cli._load_history(root, candidate_text="候选题面")

    assert history == []
    assert meta["required"] is True
    assert meta["ok"] is False
    assert "工作台离线" in meta["error"]

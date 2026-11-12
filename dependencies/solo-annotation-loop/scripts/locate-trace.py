#!/usr/bin/env python3
"""Locate a completed Claude Code/Codex CLI round from a JSONL trace."""
from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path
from typing import Any


def content_text(message: Any) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        return content_text(message.get("content", ""))
    if isinstance(message, list):
        return "".join(content_text(item.get("text", item.get("content", "")) if isinstance(item, dict) else item) for item in message)
    return ""


def message_dict(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("message", {})
    return value if isinstance(value, dict) else {}


def is_user(row: dict[str, Any]) -> bool:
    return row.get("type") == "user" or message_dict(row).get("role") == "user"


def is_assistant(row: dict[str, Any]) -> bool:
    return row.get("type") == "assistant" or message_dict(row).get("role") == "assistant"


def is_synthetic_resume_assistant(row: dict[str, Any]) -> bool:
    """Claude Code --resume emits a synthetic 'No response requested.' marker."""
    message = message_dict(row)
    return (
        is_assistant(row)
        and str(message.get("model") or "") == "<synthetic>"
        and str(message.get("stop_reason") or "") == "stop_sequence"
    )


def prompt_id_of(row: dict[str, Any]) -> str:
    """Return a prompt id regardless of the field shape used by the harness."""
    return str(
        row.get("promptId")
        or row.get("prompt_id")
        or message_dict(row).get("promptId")
        or message_dict(row).get("prompt_id")
        or ""
    )


def is_root_direct_user_message(row: dict[str, Any]) -> bool:
    """Identify a user-authored root message, excluding tool results and replies."""
    return (
        is_user(row)
        and row.get("parentUuid") is None
        and isinstance(message_dict(row).get("content"), str)
    )


# 同一轮的输入不只看用户手写的“继续”。Claude Code 的 Harness 会自动插入
# isMeta 恢复消息，例如 [Your previous response had no visible output. Please
# continue and produce a user-visible response.]。这类事件必须并入原 promptId
# 的回合，不能让它前面的 end_turn 成为最终收口。
CONTINUATION_TEXT = re.compile(r"^继续$")
INTERRUPT_MARKER = "[Request interrupted by user]"
HARNESS_CONTINUATION_TEXT = "Continue from where you left off."
HARNESS_RECOVERY_PATTERN = re.compile(
    r"previous response had no visible output.{0,120}continue.{0,120}user-visible response",
    re.I | re.S,
)


def is_harness_recovery(row: dict[str, Any]) -> bool:
    text = content_text(row.get("message", row.get("content", ""))).strip()
    if not text:
        return False
    if row.get("isMeta") is True or message_dict(row).get("isMeta") is True:
        return True
    return bool(HARNESS_RECOVERY_PATTERN.search(text))


def is_continuation(row: dict[str, Any]) -> bool:
    """用户输入、Harness 恢复输入和 isMeta 事件都并入当前轮直到最终 end_turn。"""
    text = content_text(row.get("message", row.get("content", ""))).strip()
    if not text:
        return False
    if text == INTERRUPT_MARKER or text == HARNESS_CONTINUATION_TEXT:
        return True
    if is_harness_recovery(row):
        return True
    return bool(CONTINUATION_TEXT.match(text))


def is_harness_notification(row: dict[str, Any]) -> bool:
    """Claude Code task notifications are same-turn events, not new prompts."""
    text = content_text(row.get("message", row.get("content", ""))).strip()
    return text.startswith("<task-notification>")


def session_of(row: dict[str, Any]) -> str:
    message = message_dict(row)
    return str(row.get("session_id") or row.get("sessionId") or message.get("session_id") or message.get("sessionId") or "")


def main() -> int:
    parser = argparse.ArgumentParser(description="Locate one completed trace round")
    parser.add_argument("trace", type=Path)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--prompt", type=Path)
    parser.add_argument("--prompt-id")
    parser.add_argument("--harness", default="Claude Code", choices=["Claude Code", "Codex CLI"])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--project-dir", type=Path)
    parser.add_argument("--copy-trace", action="store_true")
    args = parser.parse_args()
    trace_path = args.trace.resolve()
    if not trace_path.is_file():
        parser.error(f"轨迹文件不存在: {trace_path}")
    final_trace_path = trace_path
    if args.copy_trace:
        if not args.project_dir:
            parser.error("使用 --copy-trace 时必须提供 --project-dir")
        trace_dir = args.project_dir.resolve() / "traces"
        trace_dir.mkdir(parents=True, exist_ok=True)
        destination = trace_dir / trace_path.name
        if destination.resolve() != trace_path:
            shutil.copy2(trace_path, destination)
        final_trace_path = destination.resolve()
    expected_prompt = args.prompt.read_text(encoding="utf-8").strip() if args.prompt else ""
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(trace_path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        row["_line"] = line_no
        rows.append(row)
    users = []
    for index, row in enumerate(rows):
        if not is_user(row) or session_of(row) != args.session_id:
            continue
        prompt_id = prompt_id_of(row)
        prompt_text = content_text(row.get("message", row.get("content", ""))).strip()
        if args.prompt_id and prompt_id != args.prompt_id:
            continue
        if expected_prompt and prompt_text != expected_prompt:
            continue
        users.append((index, row, prompt_id, prompt_text))
    if not users:
        result = {"status": "unverified", "reason": "没有找到匹配的 user 消息", "tracePath": str(trace_path)}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1
    direct_users = [item for item in users if isinstance(message_dict(item[1]).get("content"), str)]
    matching_direct = [item for item in direct_users if expected_prompt and item[3] == expected_prompt]
    index, user, prompt_id, prompt_text = (matching_direct or direct_users or users)[0]
    final = None
    continuation_count = 0
    harness_recovery_count = 0
    round_rows = []
    continuation_prompt_ids: list[str] = []
    duplicate_initial_prompt_ids: list[str] = []
    current_prompt_id = prompt_id
    assistant_seen = False
    end_turn_seen = False
    continuation_after_end_turn = False
    for row in rows[index + 1 :]:
        if is_user(row):
            content = message_dict(row).get("content")
            text = content_text(row.get("message", row.get("content", ""))).strip()
            if text:
                next_prompt_id = prompt_id_of(row)
                is_tool_result = isinstance(content, list) and any(
                    isinstance(item, dict) and item.get("type") == "tool_result"
                    for item in content
                )
                if (
                    not assistant_seen
                    and expected_prompt
                    and text == expected_prompt
                    and next_prompt_id
                    and next_prompt_id != prompt_id
                ):
                    # Claude Code may persist the CLI prompt and the TUI prompt as
                    # two independent root user events before the first assistant
                    # event. Keep the first promptId as the canonical round identity
                    # and treat the later copy as part of the same prompt segment.
                    if next_prompt_id not in duplicate_initial_prompt_ids:
                        duplicate_initial_prompt_ids.append(next_prompt_id)
                    current_prompt_id = next_prompt_id
                    round_rows.append(row)
                    continue
                if is_harness_notification(row):
                    round_rows.append(row)
                    continue
                if is_continuation(row):
                    if end_turn_seen:
                        continuation_after_end_turn = True
                    if text == "继续":
                        continuation_count += 1
                    elif is_harness_recovery(row):
                        harness_recovery_count += 1
                    if next_prompt_id:
                        current_prompt_id = next_prompt_id
                    if (
                        next_prompt_id
                        and next_prompt_id != prompt_id
                        and next_prompt_id not in duplicate_initial_prompt_ids
                        and next_prompt_id not in continuation_prompt_ids
                    ):
                        continuation_prompt_ids.append(next_prompt_id)
                    round_rows.append(row)
                    continue
                if is_tool_result:
                    if current_prompt_id and next_prompt_id and next_prompt_id != current_prompt_id:
                        break
                    round_rows.append(row)
                    continue
                # 非续跑的直接用户消息表示本轮结束；下一轮即使复用 SessionID 也不能进入本轮。
                break
        if is_assistant(row):
            # A new TTY resumed on the same SessionID may persist a synthetic
            # "No response requested." stop_sequence before the next real
            # prompt. It is not part of the prior assistant turn and must not
            # displace that turn's final end_turn.
            if is_synthetic_resume_assistant(row) and end_turn_seen:
                continue
            assistant_seen = True
            if message_dict(row).get("stop_reason") == "end_turn":
                end_turn_seen = True
        round_rows.append(row)
    last_assistant = None
    for row in round_rows:
        if is_assistant(row) and session_of(row) == args.session_id:
            last_assistant = row
    final = (
        last_assistant
        if last_assistant is not None
        and message_dict(last_assistant).get("stop_reason") == "end_turn"
        else None
    )
    stop_reason = str(message_dict(final).get("stop_reason") or "") if final else ""
    final_index = rows.index(final) if final is not None else -1
    latest_assistant_index = rows.index(last_assistant) if last_assistant is not None else -1
    result = {
        "projectDir": str(args.project_dir.resolve()) if args.project_dir else "",
        "sessionId": args.session_id,
        "promptId": prompt_id,
        "originalPromptId": prompt_id,
        "promptText": prompt_text,
        "tracePath": str(final_trace_path),
        "harness": args.harness,
        "harnessVersion": str(user.get("version") or user.get("message", {}).get("version") or ""),
        "startTime": user.get("timestamp", ""),
        "endTime": final.get("timestamp", "") if final else "",
        "stopReason": stop_reason,
        "endTurnIndex": final_index,
        "latestAssistantIndex": latest_assistant_index,
        "promptMatches": bool(expected_prompt and prompt_text == expected_prompt),
        "continuationCount": continuation_count,
        "harnessRecoveryCount": harness_recovery_count,
        "continuationPromptIds": continuation_prompt_ids,
        "duplicateInitialPromptIds": duplicate_initial_prompt_ids,
        "continuationAfterEndTurn": continuation_after_end_turn,
        "toolCount": sum(1 for row in round_rows if is_assistant(row) and any(isinstance(item, dict) and item.get("type") == "tool_use" for item in message_dict(row).get("content", []) if isinstance(message_dict(row).get("content", []), list))),
        "status": (
            "verified"
            if final
            and stop_reason == "end_turn"
            and not continuation_after_end_turn
            and (not expected_prompt or prompt_text == expected_prompt)
            else "unverified"
        ),
    }
    # 若输出文件已存在（如已预填 projectId/round/initialSnapshot/commitUrl），保留这些字段，只覆盖脚本生成字段
    if args.output and args.output.exists():
        try:
            existing = json.loads(args.output.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                result = {**existing, **result}
        except (json.JSONDecodeError, OSError):
            pass
    output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    print(output, end="")
    return 0 if result["status"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())

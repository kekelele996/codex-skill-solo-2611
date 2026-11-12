"""Claude Code 轨迹解析：定位主会话、单轮结构校验、确定性时间线（不做任何评价）。"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from common import Solo2611Error


def main_trace_files(traces_dir: Path) -> list[Path]:
    """主会话 jsonl 位于 <traces>/<项目目录>/<sessionId>.jsonl；子代理在 <sessionId>/subagents/ 下，不算主会话。"""
    if not traces_dir.is_dir():
        return []
    files: list[Path] = []
    for project in sorted(p for p in traces_dir.iterdir() if p.is_dir()):
        files.extend(sorted(p for p in project.iterdir() if p.is_file() and p.suffix == ".jsonl"))
    return files


def subagent_files(main_trace: Path) -> list[Path]:
    folder = main_trace.with_suffix("")
    return sorted(folder.rglob("*.jsonl")) if folder.is_dir() else []


def load_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise Solo2611Error(f"轨迹第 {lineno} 行不是合法 JSON: {path}") from exc
        if isinstance(item, dict):
            events.append(item)
    return events


def _content(event: dict[str, Any]) -> Any:
    msg = event.get("message")
    return msg.get("content") if isinstance(msg, dict) else None


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text") or "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def is_real_user_prompt(event: dict[str, Any]) -> bool:
    if event.get("type") != "user" or event.get("isMeta") or event.get("isSidechain") or event.get("isCompactSummary"):
        return False
    content = _content(event)
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return False
        return any(isinstance(b, dict) and b.get("type") in {"text", "image"} for b in content)
    return False


def _norm(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").strip().splitlines())


def _ts(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def analyze(path: Path, prompt_text: str | None = None) -> dict[str, Any]:
    events = load_events(path)
    errors: list[str] = []
    warnings: list[str] = []
    session_ids = sorted({str(e["sessionId"]) for e in events if e.get("sessionId")})
    if len(session_ids) != 1:
        errors.append(f"轨迹中的 sessionId 不唯一: {session_ids}")
    if path.stem not in session_ids:
        warnings.append(f"文件名 {path.stem} 与 sessionId 不一致")

    prompts = [e for e in events if is_real_user_prompt(e)]
    if len(prompts) != 1:
        errors.append(f"真人 user prompt 数量为 {len(prompts)}，单轮数据必须恰好 1 条")
    prompt_event = prompts[0] if prompts else {}
    prompt_id = str(prompt_event.get("promptId") or "")
    if prompts and not prompt_id:
        errors.append("真人 user 消息缺少 promptId")
    if prompts and prompt_text is not None and _norm(_text_of(_content(prompt_event))) != _norm(prompt_text):
        errors.append("轨迹中的 prompt 与 delivery/提示词.md 不一致")

    main_assistant = [e for e in events if e.get("type") == "assistant" and not e.get("isSidechain")]
    final = main_assistant[-1] if main_assistant else {}
    final_msg = final.get("message") if isinstance(final.get("message"), dict) else {}
    stop_reason = final_msg.get("stop_reason") if final_msg else None
    if not main_assistant:
        errors.append("轨迹里没有 assistant 消息")
    elif final.get("isApiErrorMessage"):
        errors.append(f"最终消息是 API 错误: {_text_of(final_msg.get('content'))[:200]}")
    elif stop_reason != "end_turn":
        errors.append(f"最终 assistant 的 stop_reason 为 {stop_reason}，不是 end_turn（模型中断，需要重新 roll）")
    api_errors = [e for e in main_assistant if e.get("isApiErrorMessage")]
    if api_errors and not final.get("isApiErrorMessage"):
        warnings.append(f"中途出现 {len(api_errors)} 次 API 错误消息，最终已恢复")
    if final:
        after = events[events.index(final) + 1:]
        if any(is_real_user_prompt(e) for e in after):
            errors.append("最终 assistant 之后还有真人输入")

    tool_uses = 0
    models: set[str] = set()
    for e in main_assistant:
        msg = e.get("message") or {}
        if msg.get("model") and msg.get("model") != "<synthetic>":
            models.add(str(msg["model"]))
        content = msg.get("content")
        if isinstance(content, list):
            tool_uses += sum(1 for b in content if isinstance(b, dict) and b.get("type") == "tool_use")

    stamps = [t for t in (_ts(e.get("timestamp")) for e in events) if t]
    duration = (max(stamps) - min(stamps)).total_seconds() if len(stamps) >= 2 else 0.0
    return {
        "path": str(path),
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "sessionId": session_ids[0] if len(session_ids) == 1 else "",
        "promptId": prompt_id,
        "harnessVersion": str(prompt_event.get("version") or ""),
        "entrypoint": str(prompt_event.get("entrypoint") or ""),
        "models": sorted(models),
        "stopReason": stop_reason,
        "toolUses": tool_uses,
        "assistantEvents": len(main_assistant),
        "durationSeconds": round(duration, 1),
        "subagentFiles": [str(p) for p in subagent_files(path)],
        "finalText": _text_of(final_msg.get("content")) if final_msg else "",
    }


def stdout_result(stdout_path: Path) -> dict[str, Any]:
    """print 模式 stream-json 的最后一个 result 事件。"""
    if not stdout_path.is_file():
        return {}
    result: dict[str, Any] = {}
    for line in stdout_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("type") == "result":
            result = item
    return {k: result.get(k) for k in ("subtype", "is_error", "api_error_status", "num_turns", "duration_ms", "session_id")} if result else {}


# ---------------------------------------------------------------- 是否真的结束

# 结束标志只能说明进程退出了。下面这些说明模型还在等用户、或者话没说完、或者被切断了。
ASKING_USER = re.compile(
    r"(需要我[^。！？]{0,12}[吗么]|需要我[^。！？]{0,8}(继续|开始|执行|修改|补充)|要我(继续|开始|执行)?[吗么]|"
    r"是否(继续|需要|希望)|如需[^。！？]{0,20}(请|可以)|如果你(需要|希望|想要)|需要的话|告诉我[^。！？]{0,12}[。！？]|"
    r"请(确认|告知|指示)[^。！？]{0,12}[。！？]|你(可以|要不要)[^。！？]{0,10}(吗|么))"
)
PLAN_PROMISE = re.compile(
    r"(接下来(我)?(会|将|准备)|下一步(我)?(会|将|准备)|我现在(开始|将|去)|即将(开始|执行)|"
    r"下面(我)?(开始|将)|正准备|马上(开始|执行)|我先(说明|列出|梳理)[^。]{0,40}(然后|再))"
)
CUT_OFF = re.compile(
    r"(由于[^。]{0,20}(限制|超时|中断|无法访问)|上下文(不足|已满|限制)|context (window|limit)|"
    r"(token|tokens)[^。]{0,12}(不足|用完|限制)|(请求|操作)(被)?(中断|取消|超时)|"
    r"interrupted|aborted|timed out|Request was aborted|API Error|无法继续执行)"
)
INCOMPLETE_TAIL = re.compile(r"([,，、:：；;]|以及|和|或者|例如|\b(and|or|then|with|the|to)\b)\s*$")


def completion_review(path: Path, info: dict[str, Any], result: dict[str, Any] | None = None) -> dict[str, Any]:
    """判断这一轮是否真的做完并结束：只看结束标志不够，还要看最后在说什么、有没有断在半路。

    这里只拦结构性不完整（被截断、还在等用户、工具调用没有结果、上下文耗尽），
    不评价产物好坏；结构完整但质量差仍按规范如实评价，不重新 roll。
    """
    result = result or {}
    errors: list[str] = []
    warnings: list[str] = []

    if result:
        if result.get("is_error"):
            errors.append(f"print 模式 result 事件标记为错误：{result.get('subtype')} {result.get('api_error_status') or ''}".strip())
        elif result.get("subtype") and result.get("subtype") != "success":
            errors.append(f"print 模式没有以 success 结束，subtype={result.get('subtype')}")

    events = load_events(path)
    tool_uses: set[str] = set()
    tool_results: set[str] = set()
    for event in events:
        content = _content(event)
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                tool_uses.add(str(block.get("id")))
            elif block.get("type") == "tool_result":
                tool_results.add(str(block.get("tool_use_id")))
    dangling = sorted(tool_uses - tool_results)
    if dangling:
        errors.append(f"有 {len(dangling)} 次工具调用没有拿到结果就结束了，这一轮是断在半路的")

    final_text = str(info.get("finalText") or "").strip()
    tail = final_text[-400:]
    if not final_text:
        errors.append("最终回复为空，看不出任务已经交代完")
    if ASKING_USER.search(tail):
        errors.append("最终回复在向用户提问或等用户确认；单轮数据只发一次提示词，不能把未完成的一轮当成结果")
    if PLAN_PROMISE.search(tail):
        errors.append("最终回复还在说接下来要做的事，说明工作没有做完就结束了")
    if CUT_OFF.search(final_text):
        errors.append("轨迹里出现了中断、超时或上下文耗尽的痕迹，这一轮不是正常结束")
    if tail and INCOMPLETE_TAIL.search(tail):
        errors.append("最终回复断在句子中间，像是被截断后直接结束")
    if info.get("stopReason") == "max_tokens":
        errors.append("最终回复因输出长度上限被截断")
    if info.get("stopReason") == "stop_sequence" and not result.get("is_error"):
        warnings.append("最终 stop_reason 是 stop_sequence 而不是 end_turn，请确认不是被打断")

    turns = result.get("num_turns")
    uses = int(info.get("toolUses") or 0)
    if isinstance(turns, int) and uses == 0 and turns <= 2:
        warnings.append("整轮几乎没有工具调用，请确认提示词确实被发送并执行")
    return {"ok": not errors, "errors": errors, "warnings": warnings, "danglingToolUses": len(dangling)}


# ---------------------------------------------------------------- 时间线

def _short(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tool_label(name: str, data: dict[str, Any]) -> str:
    if name == "Bash":
        return f"`{_short(data.get('command', ''), 160)}`"
    if name in {"Read", "Write", "Edit", "MultiEdit", "NotebookEdit"}:
        return f"`{data.get('file_path') or data.get('notebook_path') or ''}`"
    if name in {"Glob", "Grep"}:
        return f"`{_short(data.get('pattern', ''), 80)}`" + (f" in `{data['path']}`" if data.get("path") else "")
    if name in {"Task", "Agent"}:
        return _short(data.get("description") or data.get("prompt", ""), 100)
    return _short(json.dumps(data, ensure_ascii=False), 120)


def _result_text(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, list):
        content = "\n".join(str(c.get("text") or "") for c in content if isinstance(c, dict))
    return str(content or "")


def timeline_markdown(path: Path, info: dict[str, Any], diff_stat: str = "") -> str:
    events = load_events(path)
    results: dict[str, dict[str, Any]] = {}
    for e in events:
        content = _content(e)
        if e.get("type") == "user" and isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    results[str(b.get("tool_use_id"))] = b

    lines = [
        "# 轨迹时间线",
        "",
        "> 由脚本按原生轨迹逐条列出，只做机械摘录。feedback 结论必须回到原始 JSONL、产物 diff 和真实运行复核，再由标注专家定稿。",
        "",
        f"- SessionID：`{info.get('sessionId')}`",
        f"- TurnID/PromptID：`{info.get('promptId')}`",
        f"- Harness 版本：`{info.get('harnessVersion')}`（entrypoint `{info.get('entrypoint')}`）",
        f"- 模型：`{', '.join(info.get('models') or [])}`",
        f"- 工具调用：{info.get('toolUses')} 次；轨迹跨度 {info.get('durationSeconds')} 秒；子代理轨迹 {len(info.get('subagentFiles') or [])} 份",
        f"- 结构校验：{'通过' if info.get('ok') else '未通过：' + '；'.join(info.get('errors') or [])}",
        "",
        "## 步骤",
        "",
    ]
    step = 0
    for e in events:
        if e.get("type") != "assistant" or e.get("isSidechain"):
            continue
        content = _content(e)
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and str(b.get("text") or "").strip():
                lines.append(f"  - 说明：{_short(b['text'], 220)}")
            elif b.get("type") == "tool_use":
                step += 1
                name = str(b.get("name") or "")
                lines.append(f"{step}. **{name}** {_tool_label(name, b.get('input') or {})}")
                res = results.get(str(b.get("id")))
                if res is not None:
                    text = _result_text(res).strip()
                    flag = "❌ " if res.get("is_error") else ""
                    first = _short(text.splitlines()[0], 200) if text else "(无输出)"
                    lines.append(f"   - {flag}结果：{first}")
    lines += ["", "## 模型最终回复（原文）", "", info.get("finalText") or "(空)", ""]
    if diff_stat:
        lines += ["## 产物 diff 统计（初始快照 → 产物快照）", "", "```text", diff_stat, "```", ""]
    return "\n".join(lines)

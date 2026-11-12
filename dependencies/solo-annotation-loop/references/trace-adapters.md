# 轨迹适配

## 统一输出

适配器必须输出：`session_id`、`prompt_id`、`prompt_text`、`start_time`、`end_time`、`stop_reason`、`harness`、`version`、`trace_path`、`tool_count`。

## Claude Code

读取 JSONL，按 `promptId` 找 user 消息，按同一 `session_id` 找后续 assistant 最终消息。只有最终消息的 `stop_reason` 为 `end_turn` 才算结束。版本优先取轨迹中的 `version`，不能用当前本地版本代替历史版本。

## Codex CLI

当前 `locate-trace.py` 只实现了 Claude Code 的 JSONL 结构（`message.content` 字符串、`promptId`、`stop_reason=end_turn`）。Codex CLI 的轨迹字段与 Claude Code 不同，适配器尚未实现：遇到 Codex CLI 轨迹时不得套用 Claude Code 逻辑，必须先拿到该版本的真实样例轨迹、确认 user 消息与最终状态的对应关系，再在 `locate-trace.py` 里补对应适配器。适配器未补之前一律失败关闭，并报告“Codex 轨迹适配器缺失，需人工确认”。

## 安全规则

轨迹必须是绝对路径。不能按文件修改时间猜 PromptID，不能把别的 Session 或上一轮轨迹当成本轮。Prompt 文本要和落盘提示词逐字比对，失败时只报告差异。


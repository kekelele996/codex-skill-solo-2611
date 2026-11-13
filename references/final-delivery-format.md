# 单题完成后的汇报格式

单题进入完成、暂停、阻断或本轮结束态后，Agent 的最终回复必须使用下面固定六段，标题文字和顺序不变，
路径一律绝对路径。该格式是交付门禁，不是可选建议。

## 强制约束

- 不得只返回任务目录、文件清单、状态行或一段摘要；必须完整呈现六段。
- 运行录屏必须直接嵌入并保留普通链接；Feedback 必须原样放入写作块，不得只给文件链接或概括。
- `交付表.xlsx` 尚未生成时，继续保留“交付表”段并明确写“尚未生成”或“平台待生成”，不得删除整段。
- 本轮若另有固定结束标记，先把标记放在固定格式最后一行；不得用标记替代或压缩六段内容。
- 本轮未解决的问题逐条写清；没有则写「无」。

## 产物呈现

- 所有已生成产物都要在对应段落给出绝对路径链接，不能只汇报任务摘要。
- 在 Codex 桌面端，运行录屏必须用标准 Markdown 媒体嵌入直接展示为可播放视频：
  `![运行录屏.mp4](/abs/.../recording/运行录屏.mp4)`。同时保留同路径的普通链接作为回退。
- Feedback 不能只给文件链接或概括。交付表段要给出 `feedback.md` 链接、交付完整性分数，并把
  `feedback.md` 的正文原样放进 `:::writing{variant="standard" id="<唯一五位ID>"}` 写作块中展示。
- 提示词、轨迹、轨迹时间线、窗口捕获证据、隐私证据、运行时复核证据、交付表和字段说明均给出链接；
  截图、窗口 JSON 和内部审计文件只列链接，不伪造或改写其中的证据。
- 若录屏因真实启动失败而只包含 Terminal，这是合规的失败证据；最终格式照常内嵌视频，并在未解决问题中
  写清页面不可达的实际原因。

```markdown
## 题目与初始快照

- 任务目录：/abs/TASK_ROOT
- 领域/子方向：按工程实际标签填写；未指定时默认数据工程 / 数仓与数据湖
- 任务类型 / 难度：功能开发 / 困难
- 初始快照：https://github.com/<owner>/<repo>/commit/<40位SHA>
- 提示词：[提示词.md](/abs/.../delivery/提示词.md)

## 会话与轨迹

- SessionID：`...`
- TurnID/PromptID：`...`
- 运行模式：supervised / print；会话轮次：N；继续：M/2
- 退出记录：accepted / aborted（原因）或无头回退结果
- 轨迹：[<sessionId>.jsonl](/abs/.../traces/-workspace/<sessionId>.jsonl)
- 轨迹时间线：[轨迹时间线.md](/abs/.../delivery/轨迹时间线.md)

## 产物快照

- 产物快照：https://github.com/<owner>/<repo>/commit/<40位SHA>；diff：`N files changed, +A, -D`

## 运行录屏

![运行录屏.mp4](/abs/.../recording/运行录屏.mp4)

- 视频：[运行录屏.mp4](/abs/.../recording/运行录屏.mp4)：`时长`，1280x720，`大小`，采集方式 `window-id / manual-import`
- 窗口捕获证据：[运行录屏-window-capture.json](/abs/.../recording/运行录屏-window-capture.json)
- 隐私证据：[运行录屏-cursor-guard.json](/abs/.../recording/运行录屏-cursor-guard.json)

## 交付表

- [交付表.xlsx](/abs/.../delivery/交付表.xlsx)
- [字段说明.md](/abs/.../delivery/字段说明.md)
- [feedback.md](/abs/.../delivery/feedback.md)
- [runtime-verification.json](/abs/.../monitor/runtime-verification.json)
- [feedback-evidence.json](/abs/.../monitor/feedback-evidence.json)
- 交付完整性：`N`（Agent 证据化起草，专家复核；正文事实已逐条回查真实运行）
- 若交付完整性为 `5`，必须同时给出 [score-review.json](/abs/.../monitor/score-review.json)
  并说明严格满分审计已通过；没有该证据时最高只能交付 `4`

### Feedback 正文

:::writing{variant="standard" id="00000"}
<完整复制 feedback.md 中“过程问题”和“产物问题”的正文，不改写、不删减>
:::

## 未解决问题

- 逐条列出；没有写「无」。
```

Agent 汇报时不能省略 Feedback 正文。未解决项只列客观缺失，不重复整段分析；视频和 Feedback 是给人直接查看的
交付物，不能退化成只有文字链接或摘要。

# Web 录屏一次成片流程

用于本轮存在用户可见页面功能的题目。目标是一次准备、两段录制、一次拼接，不在录制阶段反复试探。

只有页面真实可达时才执行本流程。若镜头内启动在登录页或业务页之前失败，立即停止 Web 录制，改用
`mode=terminal` 和 `expectedAppFailure=true` 保存实际失败；不要启动 Chrome，不要切换到另一套能运行的数据，
也不要把另一条成功路径的页面嫁接到失败视频中。

## 1. 开录前一次准备

1. 从题面和 diff 列出 `pageSteps`：页面入口、关键操作、最终可见结果。至少两步。
2. 准备可见的 `startCommand`。依赖构建和数据清理放 `preflightCommands`；服务启动必须留给
   `startCommand`。启动等待期间持续打印健康状态，避免终端数秒无更新导致采集流提前收尾。
3. 准备 `pageCommand`。脚本通过独立 Chrome 的 CDP 完成登录、页面操作、结果等待和最终截图；
   调试端口由 `{chromeDebugPort}` 注入。
4. 页面脚本使用稳定选择器和业务文案等待。操作间隔保持约 1.8 秒；避免超过 3 秒完全没有页面变化。
5. Chrome 使用本次临时 profile，并关闭同步、通知、密码管理和密码保存浮层。
6. 写入并运行 `record check-plan`。计划必须有 `startCommand`、`pageSteps`、本地 `chromeUrl` 和
   `pageCommand`。

## 2. Terminal 启动段

1. 清空本题数据并确认没有旧服务占用端口。
2. 用 `record terminal` 打开干净终端。
3. 用显式 `windowId` 开录，在画面中输入或执行 `startCommand`。
4. 等待健康检查和稳定提示符出现，随后立即停止。
5. 先保存该段视频和两份窗口证据，立即执行 `record terminal-stop` 关闭本次终端并生成
   `monitor/terminal-cleanup.json`，再进入下一段。后续录制会复用 `运行录屏.mp4` 文件名。

保存命名建议：

```text
recording/startup-terminal-segment.mp4
recording/startup-terminal-segment-window-capture.json
recording/startup-terminal-segment-cursor-guard.json
```

## 3. Chrome 页面段

1. 用 `record chrome-start` 启动独立实例，设置 `chromeUrl`。
2. 用返回的显式 Chrome `windowId` 开录。
3. 立即执行 `pageCommand`，覆盖从登录/入口到最终结果的全部 `pageSteps`。
4. 关键结果出现后短暂停留，立即停止录制。
5. 保存 Chrome 段视频和两份窗口证据，然后执行 `record chrome-stop`。

保存命名建议：

```text
recording/chrome-page-segment.mp4
recording/chrome-page-segment-window-capture.json
recording/chrome-page-segment-cursor-guard.json
```

## 4. 一次拼接与验收

1. 按真实时间顺序把 Terminal 段放在前、Chrome 段放在后。
2. 用 ffmpeg concat 合成 `combined.mp4`，再用 `record import --plan ROOT/monitor/record-plan.json`
   写成正式 `运行录屏.mp4`。
3. 用 `ffprobe` 确认 1280x720、30fps、H.264、总时长不超过 90 秒。
4. 至少抽取启动中、健康完成、登录页、业务操作结果和最终历史结果五帧，确认画面不是空白且没有遮挡浮层。
5. 运行 `status`。页面可达的 Web 方案必须同时具备 Terminal 与 Chrome 证据、独立 Chrome 实例和 profile
   清理结果；若启动在页面可达前失败，不得继续本流程，改用 Terminal 失败录屏。

## 避免返工

- 不要先启动服务再补录一条健康检查冒充启动过程。
- 不要只录 API、登录页或首页结果页。
- 不要在密码保存、同步、通知、翻译或恢复页面等浮层仍可见时开录。
- 不要用 `activate`、`open -a`、AX 提窗、设置前台应用或任何 z-order 操作让录制窗口置前；
  这些动作会抢用户鼠标和焦点。页面只能通过后台 CDP 驱动。
- 不要在桌面留下用过的录屏终端。Terminal 段结束就关闭，Chrome 段结束用 `record chrome-stop`。
- 不要在第一次检查失败时逐项临时修；回到计划字段和 `pageSteps` 一次改完，再重录完整两段。
- 不要先录制再猜结果；页面脚本应先能无录制跑完，并把最终业务文案作为等待条件。
- 台账、日志或明细表达到数百上千行时，不要让页面脚本每个渲染帧都读取整页
  `document.body.innerText`。这会把主线程拖死，甚至让已完成的任务无法显示最终状态。
  优先轮询稳定的任务状态接口，结束后重新加载一次页面，再用标题或业务元素定位最终结果。
- 文本选择器可能同时命中导航链接和页面标题。正式开录前先在无录屏模式跑完整页面脚本，
  对重复文本使用 heading/link 等语义角色收窄，避免录到一半才因 strict mode 歧义退出。

# 运行录屏

本期参考 `sologsb-0917/references/recording.md` 的窗口隔离和真实失败原则。录屏证明产物确实运行过，也证明模型交付结果能不能完成题目要求。

## 固定要求

- 从干净状态启动，完整展示真实输出；跑不起来也必须录出构建立项、启动报错或关键业务路径失败。
- 不剪掉失败或不好看的片段。最终为 1280x720、30fps、H.264、mp4，单段不超过 90 秒。
- 只用 macOS 数字 `CGWindowID` 做 ScreenCaptureKit 窗口级采集，不录整屏、不按 bounds 裁切、不按应用名或标题猜窗口。
- 只允许 Terminal.app 和 Google Chrome。是否有页面功能以用户可见变化为准，不由题目自称“纯后端”决定。
  页面能够启动时，只要新增/修改页面、页面交互、页面展示字段或状态，或者后端结果需要在页面呈现，就必须
  录制 Terminal 和对应页面，两者都要出现。若应用因真实启动错误在页面可达前就失败，则只录 Terminal 的
  实际失败过程即可，使用 `mode=terminal` 和 `expectedAppFailure=true`，不得另起一套成功环境、复用已有服务
  或拼接另一条数据路径的页面来补画面。
- 页面段必须从真实入口进入，完成登录（需要时）、目标页面操作和结果核验。仅打开首页、健康检查返回 200、
  页面存在但未操作任务相关功能，都不能证明页面功能可用。
- 录制器固定 `showsCursor=false`，不移动、不查询、不停靠宿主鼠标。专家可以正常操作鼠标，最终画面不采集指针。
- 开录前清空终端 scrollback，只运行与验收有关的命令。禁止展示 `.env`、API Key、token、连接串、`~/.codex` 配置和任务目录外文件。
- 录制过程中不激活、不置前、不最小化录制窗口。失败时也要保留真实失败过程。
- 禁止通过 `activate`、`open -a`、AX 提窗、设置 frontmost 或调整 z-order 来让录制窗口显示；
  页面自动化必须使用后台 CDP，不能抢占用户鼠标和焦点。
- 画面里不能出现宿主机的登录账号、家目录路径或主机名。Terminal 段一律用 `record terminal` 打开的录屏终端，
  不要录自己日常用的终端窗口（标题栏和提示符都带 `用户名@主机名`）。

## 录屏终端与隐私门禁

```bash
python3 $S record terminal --task-root ROOT
```

会新开一个 1280x720 的 Terminal 窗口并返回 `windowId`：已 `cd` 到 workspace，提示符固定为 `目录名 $`，
标题为「solo-2611 录屏」，不加载个人 shell 配置（别名、主题不可用），`PATH` 沿用登录 shell。
信息记在 `monitor/record-terminal.json`。

门禁有三道，都不能跳过：

- `record check-plan`：`startCommand` 和 `commands` 会原样出现在画面里，带本机账号或家目录路径就不通过，
  改用相对路径。`buildCommands`、`preflightCommands`、`cleanupCommands` 不进视频，不受限。
- `record start`：窗口标题或 Terminal 现有内容（含 scrollback）带账号信息时拒绝开录。
- `record stop`：再读一遍 Terminal 内容。录制期间有命令打印了账号、家目录绝对路径或主机名，这段录屏作废，
  原始片段直接删除，需要重录。常见来源是 `pwd`、`pytest` 不带 `-q` 时的 `rootdir`、带绝对路径的 traceback。

读取 Terminal 内容走 AppleScript，首次使用要在 系统设置 → 隐私与安全性 → 自动化 里允许当前程序控制「终端」；
读不到时按未通过处理。Chrome 窗口只能检查标题，页面里不要打开 `file://` 下的家目录路径。
`record import` 导入的手工视频无法做这些检查，由复核人负责确认。

手工执行的 Terminal 段停止后必须立即运行 `record terminal-stop`。Web 题开始 Chrome 段前，
`monitor/terminal-cleanup.json` 必须显示终端窗口和临时会话都已删除。

## 录屏计划

先生成并填写计划：

```bash
S=~/.codex/skills/solo-2611/scripts/solo2611.py
python3 $S record plan --task-root ROOT --mode terminal|web
python3 $S record check-plan --task-root ROOT --file ROOT/monitor/record-plan.json
```

`mode=terminal` 只能有 Terminal；`mode=web` 必须包含 Terminal 和 Chrome。计划字段：

- `buildCommands`：依赖安装、编译等只动本任务目录的准备命令，不进入视频。
- `preflightCommands`：占用端口、重置本地数据等会改变运行环境的步骤放在正式开录前；页面功能题不能在这里
  提前启动服务后只录测试命令。
- `startCommand`：正式录制时在 Terminal 中展示的真实应用启动命令。依赖构建和数据清空可以提前，
  服务启动必须在镜头里发生。
- `commands`：健康检查、等待服务、执行关键验收命令。
- `pageSteps`：`mode=web` 时为必填数组。逐条写清登录/进入路径、触发的业务操作、等待页面出现的关键结果；
  至少包含页面入口、业务操作和最终结果三步。这是视频必须覆盖的页面验收集合，不能只写“打开页面”或“检查 HTTP 200”。
- `chromeUrl`：`mode=web` 时为必填本地回环地址，独立 Chrome 从这里打开页面。
- `pageCommand`：`mode=web` 时必填。录制 Chrome 段时执行的页面自动化命令，`{chromeDebugPort}` 会替换为
  本次独立实例的调试端口。完整执行顺序见 `references/web-recording-runbook.md`。
- `cleanupCommands`：结束时停止本次服务，不能误杀用户原有进程。
- `expectedAppFailure`：只有题目就是要展示产物失败时才设 `true`；不能用来掩盖录错。

## 数据工程题的录法

- 增量/幂等：先展示干净 fixture 导入后的行数或批次状态，再重复执行同一批并回读，直接展示“没有新增”或“版本保持不变”。
- 迟到/乱序事件：展示一条晚到或旧版本事件进入后的状态读取，能看出旧值是否错误覆盖。
- 断点续跑：在中途失败或人为停止后重新启动，展示从哪个水位继续、最终输出与一次性运行是否一致。
- 对账：展示源侧、入库侧和差异分类结果，不只跑一个“全部通过”的测试脚本。
- 隔离/回填：展示异常记录被隔离且好记录已提交，或修复只替换异常分区。
- 纯后端/API：在 Terminal 里真实执行启动、请求、回读和清理命令，请求体只用本地测试数据。

## Web 题独立 Chrome

进入本节前，先确认镜头内的启动过程已经成功，页面和登录入口都真实可达。若启动在页面可达前失败，停止按 Web
流程操作，保留 Terminal 失败录屏即可；不要为了出现页面而切换数据库版本、临时修复迁移或录制另一套实例。

不要复用用户正在使用的 Chrome。先看窗口列表，再用技能启动本次独立实例：

```bash
python3 $S record list
python3 $S record chrome-start --task-root ROOT --url http://127.0.0.1:3000
python3 $S record list
python3 $S record start --task-root ROOT --window-id <界面窗口ID>
# 在页面完成真实操作，必要时再录 Terminal 启动段
python3 $S record stop --task-root ROOT
python3 $S record chrome-stop --task-root ROOT
```

录制 Chrome 段前，先把 `targetApps` 设为 `["Terminal", "Chrome"]`，并填写 `pageSteps`。例如：

```json
{
  "mode": "web",
  "targetApps": ["Terminal", "Chrome"],
  "pageSteps": [
    "登录后进入影响分析页",
    "选择目标资产与字段并发起分析",
    "页面出现新的缺口说明和迁移清单状态"
  ]
}
```

页面段必须让观众看见任务相关操作及其结果，不能只停留在登录页、首页或网络健康状态。
浏览器操作之间保留至少约 1.8 秒观察时间，关键状态要等待稳定的业务文案或元素后再继续。密码保存、
登录 Chrome、同步、翻译、通知、下载和崩溃恢复等浮层必须在开录前关闭或禁用，不能遮挡页面结果。
原生下拉、文件选择、确认框等系统窗口不会进入窗口级视频，关键验收不得依赖展开它们。

`chrome-start` 会记录现有 Chrome PID/窗口、使用新的临时 profile 和调试端口，并把证据写到
`monitor/chrome-session.json` 与 `recording/chrome-instance.json`。它会用 `lsof` 确认调试端口监听进程
属于本次新实例，再确认被采集窗口的 owner PID 也在本次 Chrome 进程集合里。`record start`
会拒绝非本任务独立实例的 Chrome 窗口。收尾只清理带本次临时 profile 的进程，最后删除 profile
并写 `recording/chrome-profile-cleanup.json`。

## 无人值守录制（Terminal 段）

```bash
python3 $S record auto --task-root ROOT [--plan FILE]
```

一条命令做完：离屏执行 `buildCommands`、`preflightCommands` → 打开录屏终端 → 开录 → 把 `startCommand` 和
`commands` 逐条敲进终端，等上一条回到提示符再敲下一条 → 停止、过隐私门禁、转码 → 离屏执行 `cleanupCommands`
并关掉录屏终端。全程不激活窗口、不碰鼠标。

写计划时注意：

- 每条命令都要能自己结束。常驻服务放 `preflightCommands`，或在命令末尾加 `&`。
- 正式段总共 90 秒。没敲完的命令不会补录，整段判为不完整并要求重录。
- 产物本来就跑不起来时照常录，真实报错留在画面里，并在计划里写 `expectedAppFailure: true`。
- 只支持 `mode=terminal`。Web 题仍按下面的分段流程录制后 `record import`。

## Terminal 与最终停止

```bash
python3 $S record terminal --task-root ROOT        # 返回 windowId
python3 $S record start --task-root ROOT --window-id <返回的windowId> --plan ROOT/monitor/record-plan.json
# 在 Terminal 中执行计划里的真实命令，必要时另录 Chrome 段并拼接
python3 $S record stop --task-root ROOT
```

`record stop` 会转码、检查时长与分辨率，并写：

- `recording/运行录屏-window-capture.json`
- `recording/运行录屏-cursor-guard.json`
- `monitor/recording.json`

手工录制的视频可用 `record import` 导入，但来源会标记为 `manual-import`，不会伪造
`showsCursor=false` 或窗口所有权证据。团队复核时要确认它不是整屏录制、没有混入凭据或后期伪造；
优先使用工具生成的 `window-id` 片段。

## 拼接

页面功能题需要 Terminal 和 Chrome 两段时，分别录制为独立的 mov/mp4，在本地按真实执行顺序拼接为一段后导入：

```bash
ffmpeg -f concat -safe 0 -i parts.txt -c copy combined.mp4
python3 $S record import --task-root ROOT --file combined.mp4
```

合计时长仍不得超过 90 秒。优先把启动段压到十几秒，把页面上的关键验收路径完整保留。最终成片缺少
Terminal 或缺少任务相关页面操作时，应按录屏未通过处理并重录。

Web 成片的默认顺序是：Terminal 中真实启动服务并等待健康检查 → Chrome 中从页面入口开始登录/进入目标页 →
完成关键业务操作 → 停留在最终结果。不能先启动服务再只把测试命令补进镜头，也不能只展示结果页而省略触发操作。

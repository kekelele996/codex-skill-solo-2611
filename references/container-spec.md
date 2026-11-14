# 容器规范与技能映射

原文：`../official-specs/CLAUDE_CODE_DOCKER_MAC2.md`。

## 官方要点

- 镜像 `adminfather/benzhi-claude-code2:20261110`，Intel/Apple 芯片同名；内置 Claude Code `2.1.303`、网关 `https://llm2.jzxhnh.com`、模型 `auto_model/urm`、Git 工具链。
- 一题一个新容器，容器内固定 `/workspace`；本机目录 bind mount 到 `/workspace`，双向读写不是备份。
- 启动参数：`docker run -it --init --restart=no --cap-drop ALL --security-opt no-new-privileges --name <名> --mount type=bind,src=<本机>,dst=/workspace -e apikey=<KEY> <镜像>`。
- 不加 `--rm`（退出会删掉还没导出的轨迹）；不要 `docker start/restart` 复用容器；不支持 `--continue/--resume`；斜杠命令被禁用。
- 结束：对话里按两次 `Ctrl+D`；导出：`docker cp <名>:/home/node/.claude/projects <本机>/traces`，保留完整目录结构；确认后 `docker rm <名>`。
- 多开时每个容器名不同、每个容器使用不同的新建工作目录；路径不能含逗号。
- Docker 容器名只允许 ASCII 字母、数字、下划线和连字符。中文任务名必须经过
  `container_slug()` 转为稳定 ASCII 名称，不能直接复用允许中文的普通路径 slug。
- 入口禁用自动记忆、技能、自定义指令、MCP；Key 会留在终端历史和容器配置里，不要分享。

## 镜像入口实测（2026-11-10 读取 `/usr/local/bin/entrypoint.sh`）

- 模式参数只有 `interactive`（默认）和 `print`。`print` 追加 `-p --output-format stream-json --verbose`，提示词从 stdin 读。
- 入口用 `$HOME/.task-session-started` 拒绝同一容器第二次启动；要求 `/home/node/.claude/projects` 为空。
- 入口注释写明 `/workspace` **可以非空**：调用方挂载初始快照仓库。官方文档写「启动时必须为空」是针对手工从零开始的流程；本技能挂载的是已提交初始快照的仓库，符合入口设计。
- 固定参数：`--safe-mode --disable-slash-commands --setting-sources '' --strict-mcp-config --tools 'Bash,Read,Write,Edit,Glob,Grep' --dangerously-skip-permissions`。没有 TodoWrite、WebFetch。
- 环境变量已设 `ANTHROPIC_MODEL=auto_model/urm`、`CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000`，技能不覆盖。
- 容器工具链：Debian 12，node 22 / npm / yarn，gcc/g++/make，python3.11（无 pip、无 numpy），git，curl/wget。
  **没有** cmake、ninja、clang、pnpm、go、rust、java，**没有 GPU**，用户是非 root 的 `node`，装不了系统包。

## 两种运行方式

| 方式 | 命令 | 轨迹 `entrypoint` | 说明 |
|---|---|---|---|
| 受管交互（默认） | `run` → tmux + 独立监督器 | `cli` | 逐字发送题面后以轨迹确认送达；8/20/36/50 秒自动补按回车或同文重发，75 秒仍无匹配 prompt 才按启动失败重排。确认后仅明确 API/接口故障可发送字面 `继续`，最多 2 次；正常结束立即终止 |
| print（回退） | `run --mode print` | `sdk-cli` | 无头执行，一次机会、无自动继续；用于交互 TUI 或容器环境不可用时的保守回退 |
| 手工交互 | `interactive` → 专家粘贴 → `collect` | `cli` | 保留为专家排障入口；不提供自动继续 |

两种方式的 `sessionId`、`promptId`、`version` 字段都齐全（实测）。

受管交互额外把本机 `monitor/runs/attempt-NN/live` bind 到
`/home/node/.claude/projects`，用于实时读取轨迹。Token 写入权限为 `0600` 的临时 `env.sh`；
tmux 命令行、`ps` 输出和 `launch.sh` 都不包含 Key。题面发出后立即删除密钥临时文件。

`claude.openTerminalOnRun=true` 时，技能会用 macOS AppleScript 新开 Terminal 窗口并执行
`exec tmux attach-session -r -t <会话名>`。该窗口只负责显示：不抢键盘焦点，默认只读接入
（`claude.terminalReadOnly=false` 时去掉 `-r`，`claude.terminalActivate=true` 时才把窗口提到前台）；
关闭窗口或按 `Ctrl+B`、`D` 脱离不会停止监督器和容器。默认关闭，避免无人值守运行、自动恢复和
录屏阶段被弹窗打扰。

窗口回收：会话正常收尾、`abort`、启动失败、题面送达失败、重启换会话以及监督器退出时，都会关闭
对应窗口并写入 `terminal.visible_close` 事件；启动新会话前、`cleanup` 和 `terminal-sweep` 会清扫
所有已结束会话残留的窗口。判断依据是 tmux 会话是否还存在，会话存活或窗口里还有其它命令在运行时
一律不关。窗口按标题或启动命令中的 `tmux attach … -t solo2611-*` 认领，旧版本打开的窗口同样能回收。

启动容错：确认页（Bypass Permissions、信任文件夹、Press Enter to continue）统一按选项行解析，
先移动光标、回读屏幕确认高亮在 `Yes` 选项上再回车，不会把 `Down` 和回车连发；tmux 会话消失
（容器已退出）时返回 `session_exited` 并在 5 秒后重启，其余失败按 `launchSpacingSeconds` 间隔重启，
最多 `claude.startAttempts`（默认 3）次；`tmux new-session` 失败自动重试 3 次。

## 容器名额

同时运行的 solo-2611 容器不得超过 `claude.maxContainers`。所有启动路径共用 `scripts/capacity.py`
这一道闸：`run` 的受管模式、打印模式，以及 supervisor 的每一次重启，都要先拿到名额才能启动。

- 占用数 = `docker ps --filter label=solo2611=true` 的运行中容器 + 尚未出现在 docker ps 里的
  启动租约。租约记在 `~/.codex/solo-2611/container-leases.json`，覆盖“已经放行、容器还没起来”
  的空窗；容器出现后租约自动让位，容器提前退出或经技能删除时租约立即归还，启动方崩溃时
  300 秒后过期。
- 计数和放行在同一把文件锁（`containers.lock`）内完成，多个会话同时执行 `run` 也只会按上限放行。
- 同一道题重复申请只续期，不多占名额。
- Docker 查询失败时一律不放行，不按“0 个容器”处理。
- 名额满时每 5 秒重试，最多等 `claude.slotWaitSeconds`（默认 1800 秒）。受管模式在创建尝试
  之前申请名额，等待时间不计入单题 3 小时总时限，也不消耗启动机会；超时后命令报错退出。
- `python3 scripts/solo2611.py capacity` 查看上限、运行中的容器和启动中的租约。

## 轨迹结构（实测）

- 主会话：`traces/-workspace/<sessionId>.jsonl`；子代理轨迹在 `traces/-workspace/<sessionId>/subagents/`。
- 真人消息：`type=user`、带 `promptId`、`version=2.1.303`。
- 结束判断：最后一条主线 assistant 的 `message.stop_reason=end_turn` 且不是 `isApiErrorMessage`。
  自动或人工继续只接受明确 API/接口故障；正常结束里的提问、提交/推送/PR询问或“下一步可以”都不是接口故障。
- 正常结束直接进入终态，不自动恢复、不录屏、不导出、不提交。
- 误触发继续：监督器必须停止；可用 `recover-round` 在保留原始轨迹副本和哈希的前提下，截到正常完成轮次，
  不得向同一会话再补发“继续”。
- print 模式 stdout 最后有 `type=result`，`is_error`/`api_error_status` 可直接判断鉴权或网关故障。

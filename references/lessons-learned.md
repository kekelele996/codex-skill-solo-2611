# 实测记录

## 2026-11-12 交付完整性满分校准

- 实际提交中，功能全部通过、Feedback 也无门禁错误时容易直接给 `5`，导致当天满分占比超过平台 40% 上限。
- 平台打回后复核发现，当前任务仍有可降分的真实缺口：完整数据库与页面流程没有固化为项目内自动化回归测试，
  大批次详情页也只展示前一部分记录。
- 新口径把 `4` 设为常规高质量完成的上限。`5` 只能作为例外，必须填写
  `monitor/score-review.json`，同时满足全部明示需求、关键边界、至少两条真实运行、回归、跨层回读和
  `knownGaps` 为空；缺少任何一项都由 `feedback --check` 阻断。
- `4` 分也必须写明少量残余缺口，避免把“没有报错”误当成接近满分。

## 2026-11-12 Feedback 事实核对与真实结束判定

- `feedback --check` 原来只有正则：它能拦禁词和格式，拦不住编造的数字、文件和报错。
  新增 `scripts/evidence.py`，由脚本自己从主会话与子代理轨迹、产物快照、`runtime-verification.json`
  建索引，把正文里的数值、文件路径、反引号标识符和报错原文逐条回查，查不到即阻断。
- 索引只收工具真实返回的输出。模型在轨迹里说的话、`verify` 的命令参数都不作为事实来源——
  否则先把编造的数字写进命令参数再执行一次就能绕过。`echo`、`python3 -c`、`bash -c` 直接拒绝。
- 两次运行输出之间的差值、倍数算可核验数字，避免“必须凑到原文”逼着人去改数字。
- 至少一条真实执行记录是硬门槛；4–5 分要求有一次成功退出；写了报错/失败表现就必须有真实失败输出。
  实际测下来，编造的“240 行”被精准拦下，改为真实输出里的“120 行”后才通过。
- `run`/`collect` 增加 `trace.completion_review`：`end_turn` 只说明进程退了。最终回复在向用户提问、
  还在许诺“接下来我会…”、有 tool_use 没有对应 tool_result、出现上下文耗尽/超时痕迹、回复断在句子中间，
  都按未完成处理并重新 roll，判定写进 `attempts[].completion`。
- 端到端测试里故意保留了两次 `SystemExit`：没有执行记录时拒绝、反馈数字与真实输出不符时拒绝。
- 补 `tests/test_evidence.py` 后立刻抓出三个问题，都是端到端测不出来的：
  1. 路径检查从被掩码过的文本里找，`PATH_CLAIM` 已被抹掉，这一层实际上从没生效过。
     现在数字用掩码文本（避免文件名里的版本号被当成声明），路径用原文。
  2. `CLAIM_NUMBER` 原来用 `\w` 做边界，Python 的 `\w` 含汉字，`订单120行` 这种最常见的
     中文写法整体漏掉。改成只排除 ASCII 标识符字符，中英混排都能抽出来。
  3. 轨迹里模型自己敲的命令文本也算数值来源，等于把数字写进命令参数再执行一次就自证了。
     现在命令文本只给函数名和报错原文作证，不给数字作证——和 `verify` 参数同一条口径。

## 2026-11-10 搭建时

- 新镜像 `benzhi-claude-code2:20261110` 拉取约 1 分钟；`claude --version` = 2.1.303。
- 用旧 sologsb 配置里的 Key 跑 print 模式冒烟：网关返回 `401 invalid_token`，Claude Code 内部重试约 3 分钟才结束，
  轨迹照常生成但最后一条 assistant 是 `isApiErrorMessage`、`stop_reason=stop_sequence`。
  因此 `run` 遇到 401 不再重新 roll，直接停下提示换本期团队 Key。
- print 模式轨迹的真人消息 `entrypoint=sdk-cli`，带 `promptId`、`promptSource`、`turnOrigin`、`version`。
- 容器 stderr 会出现 `[claude-code:unrecognized_model] {"model":"auto_model/urm"...}`，是网关模型名未登记在 Claude Code 内置表里的提示，不影响运行。
- 路径里不要带 `[` `]`：Python `glob` 会把它当字符类，导致找不到轨迹（技能内部用 `iterdir`/`rglob` 规避）。
- `run --attempts 1` 真实跑通链路（旧 Key）：约 3 分钟后容器退出码 1，stdout 末尾 `result.is_error=true, api_error_status=401`；
  技能导出轨迹到 `monitor/runs/attempt-01/traces`、删除容器、尝试标为 rejected、状态 `run_failed`，workspace 仍停在初始快照。
- 本期团队 Key 实际属于 `https://llm.jzxhnh.com`（不是容器文档和镜像内置的 llm2），在 llm2 上返回 401 `token_not_found_in_db`。
  所以配置里要写 `claude.baseUrl=https://llm.jzxhnh.com`，技能用 `-e ANTHROPIC_BASE_URL` 覆盖镜像默认值。
- 从截图抄 Key 时注意 `I`/`l`/`1` 易混；401 时先用 `config test` 排查，不要直接 `run`。
- 首个正式题（TexForge 分辨率一致性，Bugfix/困难）：print 模式约 35 分钟一次通过，18 个文件变更。
  `repo` 用 REST API 建公开仓库 `texforge-res-consistency-juyt`，初始与产物快照都经 `/repos/.../commits/<sha>` 确认可达。

## 2026-11-10 数据工程改版

- 本期默认方向改为「数据工程 / 其他」，区分的是多渠道、上下游延伸或边界混合的数据工程任务，不是通用后台。
- 2026-11-11 分类更新：安全增加身份认证、网络与通信安全、系统安全；图形增加游戏开发、图像处理；数据工程增加数仓与数据湖、数据分析与数据应用，并取消游戏和数据工程下的「其他」兜底。
- 大类下所有合法子方向都允许出题，例如数据工程三个子方向均可直接用于 `init` 和 `prompt`。旧配置中的合法领域/子方向不再被迁移覆盖。
- `prompt` 现在必须先铺并填写难度论证与 ra-人话审核记录。题面原句引不到、源码文件不存在、复杂度只靠文字自证都会被阻断。
- Feedback 不再套用“禁止分析轨迹”的旧限制；结论必须回到原始 JSONL、diff 和真实运行反复核对，再由专家复核。
- 录屏改为显式窗口 ID。Chrome 必须由 `record chrome-start` 用临时 profile 新开，`record start` 会拒绝用户已有 Chrome。
- 录屏固定不采集指针，并生成 `运行录屏-window-capture.json` 与 `运行录屏-cursor-guard.json`；`status` 会检查这两份证据。
- 同一 Python 进程内连续 `record start/stop` 时，录制器退出后会短暂留下僵尸进程；`_alive()` 必须先用
  `waitpid(WNOHANG)` 回收，再判断 `kill(0)`，否则会误等 45 秒后报“没有落盘”。

## 2026-11-11 页面录屏补充

- 一题虽然主要验证后端状态指纹和幂等，但只要交付同时修改了影响分析页面，terminal-only 录屏就不完整。
- 录屏模式按用户可见功能判断；Web 计划的 `pageSteps` 为空会被门禁阻断。
- 页面段必须展示任务相关操作和结果，登录页、首页或 HTTP 200 截图都不能代替功能验收。
- 服务应先清空数据，再在 Terminal 镜头里真实启动；构建和服务若在开录前就已经启动，不能只补录健康检查冒充启动过程。
- Chrome 的密码保存、同步或通知浮层会遮挡结果，独立临时 profile 需要预先关闭密码管理并过滤这些浮层。
- 录完 Terminal 段立即执行 `record terminal-stop`，不要让它留在桌面等待 Chrome 段。
- 不允许用 `activate`、`open -a`、AX 提窗或前台切换驱动录制窗口；这会抢用户的鼠标和焦点。

## 2026-11-12 账号领域锁定

- 一条“安全 / 系统安全”的数据脱敏题正常提交后被 L1 规则打回：表单允许选择三个领域，
  但该账号的首条提交已锁定为「数据工程」。领域选项不能证明当前账号可跨领域提交。
- 修正为「数据工程 / 数据分析与数据应用」后，通过返修更新原记录并质检通过。
- 技能已增加 `submission.lockedDomain`：出题阶段先阻断错领域，提交预检再按账号历史最早记录自动核对；
  本地配置与历史不一致时停止。首次提交错领域的账号必须联系管理员，不能新建重复记录绕过。
- 进一步收紧为账号大类只读、子方向可选：`init`、`prompt`、提交和返修都从配置读取唯一大类；
  配置缺大类、任务跨类或找不到同大类匹配子方向时直接阻断。不得为单题修改
  `submission.lockedDomain`、任务状态或交付表大类，历史记录只用于交叉核验。

## 2026-11-12 GitHub 仓库名被压空

- `safe_slug` 为保留中文任务目录而允许汉字，直接传给 GitHub 后，汉字会被远端替换成连字符，
  出现 `--5clx`、`--rplm`、`--k---15gx` 等没有项目含义的仓库名。
- GitHub 仓库名改用独立的 `repo_slug`：先替换常见业务术语，剩余中文用系统转写生成拼音，
  无法转写时再落到稳定哈希；最终名称始终含 ASCII 项目词和 4 位随机后缀。
- 该修复只约束后续新建仓库；历史仓库保持原名，不执行批量改名。

## 2026-11-12 实测修正

- 中文任务名不能直接拼入 Docker `--name`：即使普通路径允许中文，Docker 仍只接受
  `[a-zA-Z0-9][a-zA-Z0-9_.-]`。本题首次启动因此在建容器前失败，后续必须使用独立的
  `container_slug()`；普通任务目录和仓库名仍保留更友好的 `safe_slug()`。
- `record terminal-stop` 发出关闭后，ScreenCaptureKit 的窗口枚举可能仍短暂看到旧窗口。
  清理证据应在 5 秒内轮询确认窗口消失，不能立即判失败；实际关闭后再次运行命令也可修正报告。
- ScreenCaptureKit 的 `--list-windows` 偶尔会卡到 30 秒超时，不能让它成为 Terminal 清理的唯一
  依赖。窗口是否关闭改用 Terminal 自身的 AppleScript 窗口计数；计数失败时保守记为未确认，
  不能为了通过门禁伪造已清理。
- 页面包含 1200 行台账表时，`wait_for_function(document.body.innerText.includes(...))`
  每帧读取整页文本会拖死渲染。改为轮询任务状态，结束后 reload 一次，再用 heading/业务元素定位。
- 导航链接和页面标题常出现完全相同的文案；页面自动化先无录屏完整走一遍，并用
  `get_by_role("heading", ...)` 等语义选择器消除 strict mode 歧义，再进入正式录制。
- 页面题如果真实启动在登录页或业务页之前失败，页面本身就无法验收。此时 Terminal 失败录屏是完整证据，
  不能另起一套成功环境或把另一条路径的页面拼进来硬凑 Web 录屏。
- Codex 桌面端最终汇报不能只给录屏链接和 Feedback 摘要：视频要用 Markdown 媒体嵌入直接播放，Feedback
  正文要原样展示，其他交付文件用绝对路径集中列出。

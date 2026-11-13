---
name: solo-2611
description: >
  CC/Codex 用户反馈标注 26.11 领域专项（单轮 pointwise）的本地 Excel 交付流水线，支持
  安全、游戏图形与多媒体、数据工程三套最新分类，默认领域「数据工程 / 数仓与数据湖」。参考 sologsb-0917 的困难题设计、
  窗口级录屏和证据约束文案规则，用官方镜像 adminfather/benzhi-claude-code2:20261110
  一题一容器跑首轮，自动完成快照、轨迹校验、SessionID/PromptID 提取、轨迹时间线、
  720p 录屏证据和官方 19 列交付表。Feedback 与交付完整性必须由 Agent 基于真实轨迹和产物
  起草并验证，再由标注专家复核。
  用于“solo-2611”“26.11 三领域标注”“最新两级分类”“单轮 feedback 交付”等任务。
---

# solo-2611 三领域单轮 Feedback 交付

规范原文：`official-specs/CC_Codex_用户反馈标注说明_26.11.md`；
容器规范：`official-specs/CLAUDE_CODE_DOCKER_MAC2.md`。
出题、录屏与文案规则参考 `upstream/sologsb-0917/SKILL.md`，但本技能不是 A/B GSB：
一题只有一个 `auto` 模型、一个 session、一条 pointwise 数据。
  支持 SOLO2 的轨迹/录屏双文件上传、自动提交开关、质检轮询和原记录返修。

## 固定口径

- 一题 = 一个新容器 = 一个 session = 一条数据。提示词只发一次；不追问、不纠错。模型结构中断时，
  只能在同一会话里发送字面 `继续`，最多 2 次；不恢复快照、不换容器、不整题重跑。
- 首轮题面只有在实时 Claude Code 轨迹里出现内容完全一致的真人消息时才算送达。8 秒没有轨迹时补按
  回车，20 秒时清空输入框并重发同一题面，36 秒再补按回车，50 秒做第二次同文重发；75 秒仍没有
  证据才按启动失败处理并重新排队。所有补发都在原 tmux/Claude Code 会话内完成，不新增用户指令。
- 收到任务级“继续”时，先运行 `run-status --json` 和 `resume`，从状态文件与事件日志接回现有会话。
  不重新 `init`、不重新发送题面、不执行 `reset`，也不把任务级继续计入模型的 2 次预算。
- 全程 `auto` 模型。镜像固定 `ANTHROPIC_MODEL=auto_model/urm`、`CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000`，
  技能不覆盖模型或上下文配置。
- SOLO2 账号只允许一个所属领域，子方向不锁。新建任务、写提示词和提交前都必须读取配置中的
  `submission.lockedDomain` 作为唯一账号大类；该值一经设置即只读，不能为了适配单题用
  `config set`、直接修改任务状态或更换交付表标签。
- SOLO2 的提交平台地址、账号和登录会话只能来自本地工作台。技能进程禁止直接读取系统 Keychain、
  禁止读取或复用 `SOLO2_COOKIE` / `SOLO2_CSRF` / `SOLO2_PASSWORD`、禁止沿用旧浏览器会话。
  每次提交、查询和返修都必须先从工作台获取新会话，并校验平台返回账号与工作台配置账号完全一致；
  不一致或工作台不可用时立即停止，不能猜测、切换账号或继续提交。
- 账号大类决定可选子方向集合。必须先读真实工程，再从该大类的合法子方向中选择最匹配的一项；
  如果工程无法真实归入任一同大类子方向，直接拒绝提交。可以匹配时，只把大类保持为配置值，
  子方向填写实际匹配项。平台历史中的首条提交领域只用于交叉核验，不能反向授权跨域提交。
- 难题只收「困难」「地狱」。首轮 170 字以内优先，绝对上限 240 字。
- **“继续”只服务异常结构中断，不用于正常收尾。** 最终 assistant 为 `end_turn`，回复非空，
  没有悬空工具调用，也没有明确向用户提问、许诺尚未执行的动作或断在句子中间时，这一轮就是完成，
  监督器不得再发 `继续`。正文里正常描述 `interrupted`、中断状态、失败重试或继续执行等业务内容，
  不能仅凭关键词判成模型中断。
- 只有可观察的异常结构才允许在原会话发送字面 `继续`：限流、网关 5xx、鉴权或额度问题按各自等待/保留规则；
  `max_tokens`、空回复、明确的 `Request interrupted by user` / `API Error`、上下文耗尽、悬空工具调用、
  最终回复提问或继续许诺下一步，才按结构不完整处理。`继续` 最多 2 次，每次必须能回指具体异常信号；
  继续 2 次后仍不完整就终止并换题，鉴权、额度或模型名问题原地保留并等待处理，超时后终止。
- 误发 `继续` 后立即停止监督器，不再发送第二次。若前一正常轮次是可验证的 `end_turn`，先运行
  `recover-round --round N` 预览，核对原始轨迹哈希、截断点、保留事件数和被丢弃的 PromptID，
  再加 `--execute` 生成正式轨迹。技能必须完整保留原始轨迹审计副本，不得改写事件内容；正式轨迹只截到
  下一真人消息之前，之后所有误追加内容整体排除，并把 `monitor/round-boundary.json` 留作复核依据。
- 结束标志不等于真的做完：`run`/`collect` 还会读最终回复和轨迹本身。判定结果写进轮次记录与事件日志。
- 录屏按用户可见功能判定，不按题目自称“纯后端”或改动文件位置判定。页面能够启动时，新增/修改页面、
  页面交互、页面展示字段或状态，或者后端结果需要在页面呈现时，正式录屏必须包含该页面从入口到对应功能结果的
  真实操作；只打开页面、页面返回 200 或仅验证接口都不算页面证据。若应用因真实启动错误在页面可达前就失败，
  则使用 `terminal` 计划并设 `expectedAppFailure=true`，完整保留实际失败过程即可，不要求启动 Chrome；
  不得另起一套成功环境或拼接另一条路径的页面来凑页面证据。
- 初始快照在首轮前提交并 push；产物快照在运行后提交并 push。初始快照必须是产物快照祖先，
  模型自己的提交保留，不 squash，不 force-push/rebase。
- `.gitignore` 自动补齐 `.env`、密钥、依赖目录和构建产物；发现疑似凭据被跟踪时 `init` 直接失败。
- 轨迹和录屏当前在本地，Excel 填绝对路径；上传对象存储后再替换链接。
- 「备注」默认保持为空，SOLO2 新建和返修 payload 默认剔除 `other_issues`。发生 1–2 次继续时，
  交付表「备注」和平台 `other_issues` 写入
  `模型运行中途中断，人工输入"继续"N次后完成`；提交前轨迹命中明文 API Key 风险时，
  再接中文分号追加 `轨迹中有存在明文apikey暴露风险`。两句都没有时仍保持为空。
  平台表单缺少可用「备注」字段时必须停止提交。该备注不能记录 Key 原文、变量值、环境异常或其他补充说明。
- SOLO2 自动提交默认开启，不再等待本地二次确认。`export` 完成门禁后会直接上传轨迹和录屏、
  提交并轮询质检；需要排障时可以显式运行 `submit --dry-run`，但正常交付路径不停在本地确认。
- `SUBMITTED` 不是完成；只有 `QC_PASSED` 才算平台通过。`PENDING_FIX` 必须返修原记录，
  `QC_REJECTED`、`QC_DISCARDED`、`QC_FAILED` 或轮询超时都必须保留证据并停止。
- **最终产物呈现属于交付门禁**：任务完成、暂停、阻断或准备结束本轮时，最终回复必须按
  `references/final-delivery-format.md` 的固定六段呈现。不得只返回状态行、任务目录、文件清单或摘要；
  即使外部流程要求输出额外结束标记，也必须先完整呈现固定六段，再把结束标记单独放在最后一行。

## 证据与文案边界

本期明确要求「AI 给出的结论必须经过验证，文案自然流畅、无 AI 痕迹」。因此：

- Agent 必须读取原始轨迹、产物 diff、真实运行结果和录屏后再形成判断。模型最终回复、自述测试通过、
  文件看起来存在，都不能单独当证据。
- Agent 可以起草交付完整性分数和 Feedback，但每个文件名、函数、命令、报错、接口状态和前后计数
  都要回查到轨迹或本机真实复核结果。
- 代码支撑不等于在反馈里罗列代码。比较重要的问题允许保留 1–2 个最必要的代码或运行锚点，
  例如具体文件名、函数名、步骤、报错原文或未实现的需求点；其余代码细节留在审核材料。
- 每个重要负面问题按“触发节点 → 实际行为 → 业务后果 → 证据锚点”写清：在哪一步或哪次工具调用
  出问题，实际做了什么或漏做了什么，导致页面、数据或业务结果怎样变化，并用 1–2 个具体文件名、
  报错原文或未实现的需求点定位。锚点用于帮助复核，不能堆成代码清单。
- 已经由代码和运行结果确认的能力要使用确定表达，例如“能够”“可以”“能”“完成了”“已实现”，
  不要只留下“统一切换”“清楚展示”这类名词化短语，让读者猜实现是否真的完成。未验证或未完成的部分
  仍按事实描述，不能为了让句子肯定而拔高结论。
- 标注专家必须复核分数、事实和措辞。Agent 不得把未经运行验证的猜测写成结论，也不得为了“看起来自然”
  编造触发步骤或业务影响。这条不靠自觉：`feedback --check` 的事实层会把正文里的数值、文件路径、
  反引号标识符和报错原文逐条回查轨迹、产物快照和 `verify` 记录，查不到就阻断。
  要把一个说法变成结论，补一次真实运行；不要改数字去贴近已有输出。
- **平台 V2 以主会话轨迹核对 Feedback。** 起草前必须确定轨迹能证明的最高验证层级：
  `代码审查/语法检查`、`构建通过`、`业务链路运行` 不是同一层级。如果轨迹明确写了缺少
  pip/Docker、无法启动数据库或后端，即使之后另有本机验证，也不能把这部分写成“验证了/跑通了”
  业务链路；应写“已实现/静态检查通过”，并把未运行项写成具体缺口。`feedback --check` 会生成
  `monitor/feedback-trace-audit.json`，发现反馈越过轨迹能力上限时直接阻断。
- Feedback 同时覆盖过程与产物。负面判断写清触发节点、实际行为、客观结果和业务影响；高分也要写
  逐项核对的需求和真实运行结果。
- Feedback 是给人读的短复盘，不是验收报告：默认 180–280 个非空字，语句通畅，读一遍就能懂。
  代码信息（文件名、函数名、字段名、命令）和具体数值少写，能换成业务行为、前后变化和页面或接口可见结果就换；
  删掉后读者定位不了问题的（出错的具体位置、页面或接口直接出现的报错原文、题面点名的接口、本身就是缺陷的数字）
  可以保留，一般一两处。其余细节留在轨迹、diff 与 `monitor/runtime-verification.json`。
- 交付完整性默认高质量上限为 `4`。`5` 不是“没触发错误”的奖励，必须满足
  `references/score-calibration.md` 的严格条件，并填写 `monitor/score-review.json`；
  存在未固化的回归、未覆盖边界、分页/规模限制或任何已知缺口时评 `4`。
- `4` 分必须把残余缺口落到具体页面、功能点或验证环节。只写“完整流程缺少自动化回归”
  或“整体仍需人工复验”不够，`feedback --check` 会阻断，避免复核人无法判断后续要补哪条路径。
- 测试证据必须对准题目的业务目标。过程段写与需求直接相关的测试场景和它覆盖的业务不变量，一般不写测试文件名；
  不把“x/x 条测试通过”或无关的前端类型检查当成泛化证据，完整套件结果留在轨迹与复核材料。
- Feedback 出现读取、写入、忽略、拒绝等计数时，要说明每部分对应的业务含义，让没读代码的人能看懂
  哪些记录被写入、哪些早已入库或因旧版本被忽略。不要只堆一串裸计数。
- 不把网络波动、网关错误、宿主机权限、本地临时测试脚本等外部因素算成模型能力问题。
- Feedback 只写产品实现、需求行为和页面或接口可见结果，不得出现模型、助手、容器、Docker、临时依赖、
  本地脚本、录屏、视频、截图、浏览器、终端、鼠标、分辨率、提交、质检、返修、工作流、SessionID、
  PromptID、TurnID 或轨迹等工作流信息。业务验证可以直接写“补齐列并重新解析后再次分析”，
  但不交代由谁、在什么环境或用什么脚本执行；`feedback --check` 命中即阻断。
- Feedback 正文不得使用“用户”作主语或指代；直接写业务对象、页面、任务、样本或结果，例如把
  “用户无法创建样本”写成“样本无法创建”；`feedback --check` 命中“用户”即阻断。

## 收到“继续”时怎么做

这里的“继续”是调用方对 Agent 的续跑指令，不是第二次题面。先执行：

```bash
python3 $S run-status --task-root ROOT --json
python3 $S resume --task-root ROOT
```

以 `sessionRun`、`supervisor.log` 和实时轨迹为断点，从当前轮次继续监督或复核，不重新开始本题。
如果命令返回 `accepted`，继续后续录屏、验证、Feedback、导出与提交；如果返回 `aborted` 或
`session_lost`，保留现场并换题。不要因脚本断开、Agent 超时或工作台重启重新建容器。

## 出题规则

开始设计题面前：

1. 按实际领域读取对应出题参考：数据工程读 `references/data-engineering-topics.md`，
   游戏图形与多媒体参考 `references/graphics-topics.md`，安全围绕漏洞攻防、身份认证、
   通信安全或系统安全的真实工程边界。任一大类下的全部合法子方向都可以用于出题。
2. 运行 `prompt-history --task-root ROOT`，先读工作台里同一工程用过的全部提示词；
   查不到工程上下文时它会退回技能本地历史，工作台托管任务无法查询时不得假装已完成去重。
3. 读真实源码，至少找到 2–3 个相关业务文件、现有数据流和一条可观察的失败后果。
4. 读 `references/prompt-standard.md` 和 `references/difficulty-standard.md`。
5. 运行 `prompt --init` 生成题面草稿、`difficulty-review.json` 和 `ra-renhua-review.json`；
   初始化结果也会回传同工程历史，起草时必须先换掉重复业务问题、冲突点和改动边界。
6. 只有难度论证能逐项回指题面原句和真实源码文件时才允许安装提示词。

困难题至少满足平台三项特征中的两项，并且必须有关键设计取舍或复杂技术关注点之一；
地狱题三项全满足，另有开放方案、隐蔽约束、架构级取舍、深水调试或跨系统恢复证据。
“在现有模块加字段/筛选/结果表”不能靠文件数自称困难。

难度论证必须使用 `schemaVersion: 2`，并填写 `difficultyContract.schema=platform-p4-v2`。
合同至少包含 3 个真实运行边界、2 个来自 `ingest`、`persistence`、`publication`、`recovery`、
`accounting`、`authorization`、`downstream` 且类型不同的独立责任边界、2 个不同真实源码文件、
2 类由题面原句和对应风险正则同时命中的独立风险，以及可观察的开始到完成状态迁移。

- 代码量、文件数、测试数、页面区域数不能把中等题升级为困难题。页面、结果表、弹窗和同一算法拆出的函数
  不算独立责任边界；只靠“三个模块齐全”或手填 `passed=true` 也不能通过。
- 输入重复后的去重是数据质量问题，不是幂等。幂等只认重跑、重新运行、重试或断点续跑后不新增、不重复或保持一致。
- 出题前必须问：“如果页面完全不存在，剩下的是否仍是需要跨边界协调的困难问题？”答案为否时直接换题。
- 进入 `repo` 和 `run` 前，如果无法填写至少三个运行边界、两个独立责任边界和两类原句风险，必须阻断并换题。
  命中“判重只在……生效”“只按……归并”“工作台要显示……依据”等单点风险时，`nonRoutineDelta`
  必须点名至少两个边界并明确说明额外的跨边界约束。

提示词像业务方交代问题，不写文件路径、函数名、修法、端口、容器方案或具体技术栈。
硬性措辞最多 3 处，分号最多 2 个；不写轮次和模板收尾。固定版本 `ra-人话` 审核记录必须与最终题面 hash 一致。

## 一次性配置

```bash
S=~/.codex/skills/solo-2611/scripts/solo2611.py
python3 $S config init
python3 $S config set claude.baseUrl=<网关地址> claude.apiKey=<本期团队Key>
python3 $S config test
python3 $S config show
python3 $S config set submission.autoSubmit=true
python3 $S config set claude.openTerminalOnRun=true   # 首次启动受管会话时自动打开可见 Terminal
```

配置文件为 `~/.codex/solo-2611/config.json`（0600）。`SOLO2611_API_KEY`、`SOLO2611_BASE_URL`
环境变量优先。`submission.lockedDomain` 是账号级只读大类，只从配置文件读取，不由单题临时参数决定；
缺失或非法时停止，联系管理员处理，不能自行猜测。旧 sologsb 的 Key 在新网关可能返回 401，不会被读取。
升级到本版本后，合法的领域/子方向选择会保留，不会把其他领域统一改成数据工程；
只有已取消的旧「数据工程 / 其他」会迁移到「数据工程 / 数仓与数据湖」。

## 工作流

1. **init**：

   ```bash
   python3 $S init --task-name <名> --source <本地仓库或目录>
   # 或 --git URL [--ref]
   ```

   生成 `TASK_ROOT/workspace` 并提交初始快照。

2. **prompt-history**：

   ```bash
   python3 $S prompt-history --task-root ROOT
   python3 $S prompt-history --task-root ROOT --text "候选题面"
   ```

   通过本地工作台查询当前工程用过的提示词；`prompt --init` 会再次附带历史，
   `prompt --file` 安装前会强制重查并合并技能本地历史。工作台托管任务查询失败时阻断安装。

3. **prompt --init**：

   ```bash
   python3 $S prompt --task-root ROOT --init
   ```

   生成 `delivery/题面草稿.md`、`monitor/prompt/difficulty-review.json`、
   `monitor/prompt/ra-renhua-review.json`。

4. **prompt**：

   ```bash
   python3 $S prompt --task-root ROOT \
     --file ROOT/delivery/题面草稿.md \
     --task-type 功能开发 --difficulty 困难 \
     --lang "Python, PostgreSQL, Redis"
   # --domain 只能与配置中的账号大类一致；--sub-direction 必须是该大类下且与工程真实匹配。
   ```

   默认读取上一步两份 JSON。门禁包括长度、技术栈/端口/文件路径、硬性措辞、历史连续片段、
   难度证据、P4 责任边界合同、真实源码文件、跨对象不变量、状态迁移、独立风险、实质复杂度、
   中等形态自辩和 ra-人话 hash。P4 合同不完整时提示词不会安装，后续 `repo`、`run` 不能开始。
   脚本会实际调用 `~/.codex/skills/solo-annotation-loop/scripts/validate-prompt.py` 与
   `audit-human-writing.py`；固定版本校验器缺失时直接阻断，不能只靠手填审核 JSON。
   确有必要超过 170 字时加 `--allow-over-170`，仍不能超过 240 字。
   通过后题面写入 `delivery/提示词.md`，首轮执行后不能再改。
   所属领域必须与 `submission.lockedDomain` 一致，任务初始化后不得通过参数改大类。
   如果找不到能真实匹配当前工程的同大类子方向，停止并换题；不要依赖表单里显示的其他领域选项。

5. **repo**：

   ```bash
   python3 $S repo --task-root ROOT
   ```

   用 `github.accessTokens` 新建公开仓库并把初始快照推到 `main`。Token 不写进任何文件。
   仓库名不再直接使用中文任务名：技能会先把常见业务词转成英文，剩余中文转拼音，再加 4 位随机后缀，
   确保名称可读且不会被 GitHub 压成 `--xxxx`。该规则只作用于后续新建仓库，不改动历史仓库。

6. **run**：

   ```bash
   python3 $S run --task-root ROOT
   ```

   默认用受管交互模式启动官方镜像：`run` 在后台 tmux 中逐字发送题面后立即返回，
   监督器先确认首轮题面确实进入实时轨迹；窗口已打开但题面未送达时按 8/20/36/50 秒节点自动
   补按回车或清空重发同一题面。确认送达后继续轮询实时轨迹，对 429、5xx、截断等中断按规则
   发送字面 `继续`，最多 2 次。
   调用方断开不影响监督器与容器。用 `run-status --json` 查看进度，用 `resume` 接回。
   受管模式前置要求 `tmux >= 3.0` 且宿主机不是 Windows；不满足时显式阻断，可切回 print。
   `claude.openTerminalOnRun=true` 时，新会话会另开一个可见的 macOS Terminal 窗口并接入 tmux；
   关闭终端窗口只退出查看，不停止任务。默认 `false`，避免无人值守和录屏时弹窗。
   新机器首次进入 REPL 时，技能通过只读 managed settings 预接受镜像既定的 Bypass Permissions 模式；
   若未来版本仍显示确认页，会在 12 秒内尝试多种选择键，超时即重启并记录屏幕快照，不能一直卡在确认页。
   总时长默认 10800 秒，无进展或静默默认 1200 秒；退出开机会话后导出完整 Claude projects 目录，
   校验题面、继续次数、最终轮次、SessionID 和 PromptID，再打产物快照、生成轨迹时间线并推送。

   回退到无头模式时运行 `run --mode print`；该模式仍只有一次机会，没有自动继续能力。
   需要手工终端流程时用 `interactive`，结束后运行 `collect`。

7. **读证据并真实复核**：

   读取：

   - `traces/-workspace/<sessionId>.jsonl`
   - `delivery/轨迹时间线.md`
   - 初始快照到产物快照的 diff
   - 本机重新执行产物得到的构建、启动、任务输出、接口回读或对账结果

   只把真实执行、文件变更和客观输出写进结论。模型自述“已完成”但没有对应修改或运行证据时，
   按虚假成功处理。

8. **record**：

   先生成并检查录屏计划。先对照题面和产物 diff 判断是否存在用户可见页面功能，再确认应用是否真能启动并
   到达页面。页面可达且存在页面功能变化时使用 `web`，填写具体的 `pageSteps`。真实启动失败且页面不可达时
   使用 `terminal`、保留完整失败输出并设 `expectedAppFailure=true`，不要为满足页面形式另造成功画面。

   ```bash
   python3 $S record plan --task-root ROOT --mode terminal   # 纯后端，或真实启动失败且页面不可达
   python3 $S record plan --task-root ROOT --mode web        # 页面可达且有页面功能变化
   python3 $S record check-plan --task-root ROOT --file ROOT/monitor/record-plan.json
   ```

   Terminal 录制：

   ```bash
   python3 $S record terminal --task-root ROOT   # 新开干净的录屏终端，返回 windowId
   python3 $S record start --task-root ROOT --window-id <返回的windowId> \
     --plan ROOT/monitor/record-plan.json
   # 在录屏终端中执行计划里的真实命令
   python3 $S record stop --task-root ROOT
   python3 $S record terminal-stop --task-root ROOT  # 手工 Terminal 段结束后立即关闭
   ```

   录屏里不能出现宿主机的登录账号、家目录路径或主机名。不要录日常用的终端窗口；`record start`
   会拒绝标题或内容带账号信息的窗口，`record stop` 发现录制期间打印过这些信息会作废本段并要求重录。
   计划里的 `startCommand`、`commands` 用相对路径，避免 `pwd` 和不带 `-q` 的 `pytest`。

   页面真实可达的功能题必须启动本次独立 Chrome，并录制真实页面入口、关键操作和功能结果：

   ```bash
   python3 $S record chrome-start --task-root ROOT --url http://127.0.0.1:3000
   python3 $S record list
   python3 $S record start --task-root ROOT --window-id <Chrome窗口ID> \
     --plan ROOT/monitor/record-plan.json
   python3 $S record stop --task-root ROOT
   python3 $S record chrome-stop --task-root ROOT
   ```

   `mode=web` 的计划必须填写 `pageSteps`，说明登录/进入页面、触发的业务操作和应看到的页面结果。
   Terminal 段必须在镜头里执行实际应用启动命令，不能把服务已在 `preflightCommands` 启动好之后只录一段测试命令。
   依赖安装、镜像构建和数据清空可以提前完成，但启动、健康检查、页面登录、业务操作和最终结果应按真实时间顺序
   进入成片。Web 方案下最终视频必须同时出现 Terminal 和对应页面功能，只有 HTTP 健康检查或一段终端测试的
   录屏按未通过处理；真实启动失败、页面不可达时按上面的 Terminal 失败规则处理。
   Terminal 段写完后必须先关闭该录屏终端，再开始 Chrome 段，不能把用过的终端留在桌面。
   必须使用显式 `CGWindowID` 和 ScreenCaptureKit 窗口级采集；不录整屏，不按标题猜窗口，
   严禁调用 `activate`、`open -a`、AX 提窗或前台切换来“帮助”录制；不得激活、置前录制窗口。
   页面自动化只通过后台 CDP 执行，全程不抢宿主鼠标。输出 1280x720、≤90 秒；失败也录，不剪失败片段。
   `record stop` 写 `运行录屏-window-capture.json` 与 `运行录屏-cursor-guard.json`。
   Web 录屏按 [references/web-recording-runbook.md](references/web-recording-runbook.md) 一次准备、两段录制、
   一次拼接；禁止边录边试和逐项补录。详细约束见 `references/recording.md`。

9. **verify → feedback**：

   先造证据，再动笔。Feedback 里的每个数字、文件、报错和功能结论，机器都会拿去和真实执行记录对；
   没有对应记录的内容会被阻断，不是只给警告。

   ```bash
   python3 $S verify --task-root ROOT --label '重跑同一批看是否新增' -- python3 checks/verify.py
   python3 $S verify --task-root ROOT --label '启动服务后回读接口' -- npm start && curl -s localhost:3000/api/health
   ```

   `verify` 在产物工作区里真实执行命令，把退出码、stdout、stderr 和哈希固定到
   `monitor/runtime-verification.json`，每条输出另存一份到 `monitor/verification/run-NN.log`。
   失败的命令照样记录，作为失败证据；`echo`、`python -c`、`bash -c` 这类只回显或内联执行字符串的写法
   会被拒绝，因为结果不来自产物。一份 Feedback 至少要有一条真实执行记录；打 4–5 分要有一次成功退出；
   写了报错或失败表现，就必须有真实的失败输出，不能凭设计推测补写。

   ```bash
   python3 $S feedback --task-root ROOT --init
   ```

   Agent 按 `references/feedback-guide.md` 起草 `delivery/feedback.md`：
   先写交付完整性 1–5 整数，再写过程与产物各一两句关键事实；默认 180–280 个非空字，
   最多 360 字，代码和具体数值少写，必要时保留一两处。过程段只写与题目相关的测试场景和业务不变量；
   读写忽略等计数必须解释业务含义。起草后对照指南的「通顺度」正反例和自检清单改一遍：
   过程段写实现时的决定和验证行为，不写复核步骤；参数和内部字段名换成业务说法；口径、事后、对平这类行话换成直白说法（见指南「用词」一节）。然后由专家逐条复核。

   考虑评 `5` 前必须读 `references/score-calibration.md`，逐项填写
   `monitor/score-review.json`。常规完成且没有阻断问题但仍有已知小缺口时评 `4`；
   只有严格审计所有条件均通过时才允许 `5`。

   ```bash
   python3 $S feedback --task-root ROOT --check
   ```

   机器门禁分两层。**文风层**检查分数、篇幅、代码与数值出现情况、完整运行 ID、评分表抄写、
   过程/产物覆盖、负面触发与后果、高分运行证据、分数矛盾、工作流信息泄露、非空备注和 ra-人话常见模板句。
   评分层还要求：`4` 分必须写出少量残余缺口；`5` 分必须存在通过校验的
   `monitor/score-review.json`，覆盖全部明示需求、关键边界、至少两条真实运行、回归和跨层回读，
   且 `knownGaps` 为空。缺任一条件即阻断满分。
   **事实层**由 `scripts/evidence.py` 自己从原始轨迹、产物快照和 `runtime-verification.json` 建索引，
   把正文里的每个数值、文件路径、反引号里的标识符与报错原文逐个回查：查不到就阻断，并指出对不上的那一处。
   差值这类由两次真实输出推出的数字同样认可，不必凑到原文。
   同时执行**轨迹能力对齐审计**：如果轨迹明确未运行后端、数据库或完整业务链路，正文不得写对应的
   “验证了/跑通/真实回读”。审核结果写入 `monitor/feedback-trace-audit.json`；越级表述会直接阻断。
   错误必须改掉；代码和数值的警告逐个判断，能换成业务说法的换掉，删了就定位不了问题的保留。
   核对结果写在 `monitor/feedback-evidence.json`，含索引摘要和逐条声明的核对状态。

10. **export**：

   ```bash
   python3 $S export --task-root ROOT
   ```

   生成 `delivery/交付表.xlsx`（官方 19 列 + 内部记录）和 `delivery/字段说明.md`。
   缺快照 permalink、轨迹、录屏、Feedback 或关键字段时拒绝导出。
   导出时会扫描最终主会话轨迹；命中明文 API Key 风险时，只在「备注」列写入固定说明
   `轨迹中有存在明文apikey暴露风险`，其他情况保持空值。
   每日汇总用 `export-batch`，会自动跳过未完成题并检查 SessionID/题面重复。

11. **submit / repair**：

   提交平台地址、账号和认证会话只从本地工作台获取。工作台负责使用其保存的平台凭据建立新会话；
   技能不得直接读取系统 Keychain、读取认证环境变量或复用缓存 Cookie。每次提交、查询和返修都先校验
   工作台配置账号、平台返回账号和会话平台地址三者一致，任一不一致立即停止。
   提交上传当前主会话 JSONL 和正式录屏两个文件，
   其余字段按服务端动态 schema 从交付表映射。完整协议见
   [references/platform-submission.md](references/platform-submission.md)。
   预检以配置中的账号大类为准，再用账号历史的首条提交领域交叉核验。大类不一致、配置缺失，
   或题目无法匹配该大类的任一合法子方向时，都在上传前阻断；禁止修改大类标签后硬提交，
   也不能通过返修或另建记录绕过。

   ```bash
   python3 $S submit --task-root ROOT               # 默认直接提交并轮询
   python3 $S submit --task-root ROOT --dry-run     # 仅在排障时预览，不提交
   python3 $S submission-status --task-root ROOT --submission-id ID
   ```

   `PENDING_FIX` 时读取质检原文和锁定字段，修订允许修改的 Feedback、评分或录屏；
   重新导出时使用 `export --no-submit`，随后调用：

   ```bash
   python3 $S repair --task-root ROOT --submission-id ID \
     --comment "按质检要求补充证据" --execute --watch [--refresh-video]
   ```

   `QC_PASSED` 时才结束平台流程；返修始终更新原记录，不能另建重复记录。
   新建和返修会重新扫描当前主会话轨迹。只有命中明文 API Key 风险时，才把固定备注
   `轨迹中有存在明文apikey暴露风险` 写入 `other_issues`；否则该字段继续从 payload 中剔除，
   不沿用陈旧预览或服务端旧备注。

12. **status / cleanup**：

    `status` 检查十一项交付；`cleanup` 只删除本题容器，交互容器未 collect 时拒绝。
    无论最终是全部通过、真实阻断还是按要求停在导出前，结束本轮前都必须按
    `references/final-delivery-format.md` 呈现已有产物、录屏、Feedback 正文和未解决问题。

单题完成后不得省略 `references/final-delivery-format.md` 规定的任何交付段落。

## 目录

```text
TASK_ROOT/
├── workspace/                       # 容器 /workspace；初始快照 → 产物快照
├── traces/                          # 被接受会话的完整 projects 目录
│   └── -workspace/<sessionId>.jsonl
├── recording/
│   ├── 运行录屏.mp4
│   ├── 运行录屏-window-capture.json
│   ├── 运行录屏-cursor-guard.json
│   ├── chrome-instance.json         # Web 题
│   └── chrome-profile-cleanup.json  # Web 题
├── delivery/
│   ├── 题面草稿.md
│   ├── 提示词.md
│   ├── 轨迹时间线.md
│   ├── feedback.md
│   ├── 交付表.xlsx
│   └── 字段说明.md
└── monitor/
    ├── state.json
    ├── record-plan.json
    ├── prompt/
    │   ├── difficulty-review.json
    │   ├── ra-renhua-review.json
    │   └── validation.json
    ├── runs/attempt-NN/
    ├── recording.json
    ├── submission/
    │   ├── preflight.json
    │   ├── payload-preview.json
    │   ├── api-result.json
    │   └── qc-history.json
    ├── terminal-cleanup.json
    ├── chrome-session.json
    ├── runtime-verification.json        # verify 记录的真实命令、退出码与输出
    ├── verification/run-NN.log          # 每条运行的完整原始输出
    ├── feedback-check.json
    ├── feedback-evidence.json           # 事实核对：索引摘要 + 逐条声明是否回查得到
    ├── feedback-trace-audit.json        # V2 轨迹能力上限与越级表述审计
    └── score-review.json                # 仅考虑 5 分时填写；严格满分审计
```

## CLI 速查

```bash
python3 $S init --task-name NAME (--source DIR | --git URL [--ref REF]) [--tasks-dir DIR]
python3 $S prompt-history --task-root ROOT [--text "候选题面"] [--local-only]
python3 $S prompt --task-root ROOT --init
python3 $S prompt --task-root ROOT --file F --task-type T --difficulty 困难|地狱 --lang "..." \
  [--domain 领域 --sub-direction 子方向 --review FILE --difficulty-review FILE --allow-over-170]
# 子方向必须属于所选领域；数据工程可选采集/RPA、数仓与数据湖、数据分析与数据应用。
python3 $S repo --task-root ROOT [--name 前缀] [--private]
python3 $S run --task-root ROOT [--mode supervised|print] [--follow] [--timeout 10800] [--skip-repo]
python3 $S run-status --task-root ROOT [--json] [--lines 20]
python3 $S resume --task-root ROOT [--follow]
python3 $S continue --task-root ROOT [--note TEXT]
python3 $S recover-round --task-root ROOT --round 1 --reason "误将正常 end_turn 判为截断"
python3 $S recover-round --task-root ROOT --round 1 --reason "同上" --execute
python3 $S abort --task-root ROOT --reason TEXT [--by human|auto]
python3 $S resume-all [--tasks-dir DIR] [--dry-run]
python3 $S doctor baseline [--tasks-dir DIR] [--out FILE]
python3 $S doctor --fix-stale [--tasks-dir DIR]
python3 $S interactive --task-root ROOT
python3 $S collect --task-root ROOT [--container NAME]
python3 $S reset --task-root ROOT --yes
python3 $S snapshot|digest|bundle|status --task-root ROOT
python3 $S record plan --task-root ROOT --mode terminal|web [--force]
python3 $S record check-plan --task-root ROOT --file FILE
python3 $S record list
python3 $S record terminal --task-root ROOT
python3 $S record terminal-stop --task-root ROOT
python3 $S record auto --task-root ROOT [--plan FILE]      # 无人值守录 Terminal 段
python3 $S record chrome-start --task-root ROOT --url http://127.0.0.1:...
python3 $S record chrome-stop --task-root ROOT
python3 $S record start|for --task-root ROOT --window-id N [--plan FILE] [--max 90] [--seconds S]
python3 $S record stop --task-root ROOT
python3 $S record import --task-root ROOT --file FILE
python3 $S verify --task-root ROOT --label '验证了什么' -- <命令及参数>
python3 $S feedback --task-root ROOT --init|--check
python3 $S submit --task-root ROOT [--dry-run|--execute] [--watch]
python3 $S submission-status --task-root ROOT --submission-id ID
python3 $S repair --task-root ROOT --submission-id ID --comment TEXT [--execute] [--watch]
python3 $S export --task-root ROOT [--allow-incomplete]
python3 $S export-batch [--tasks-dir DIR] [--date YYYY-MM-DD] [--out FILE]
python3 $S cleanup --task-root ROOT [--force]
python3 $S config init|show|set|test|path
python3 $S version
```

## 参考资料

- `references/annotation-spec.md`：26.11 规范摘要、19 列字段和质检点。
- `references/data-engineering-topics.md`：数据工程各子方向的真实工程形态、容器限制和雷同禁区。
- `references/prompt-standard.md`：题面设计、长度、自然表达与机器门禁。
- `references/difficulty-standard.md`：困难/地狱判定、G16/G17 经验和强制证据。
- `references/difficulty-review-template.json`：难度论证模板。
- `references/ra-renhua-review-template.json`：固定版本 ra-人话审核记录。
- `references/recording.md`：录屏计划、窗口 ID、独立 Chrome 和数据工程录法。
- `references/web-recording-runbook.md`：页面功能题的一次成片顺序、分段保存、拼接和抽帧验收。
- `references/feedback-guide.md`：证据化 Feedback 起草与专家复核。
- `references/score-calibration.md`：交付完整性评分校准、普通 4 分上限与严格满分条件。
- `references/score-review-template.json`：`monitor/score-review.json` 的满分审计模板。
- `references/container-spec.md`：镜像、容器和轨迹结构。
- `references/field-guide.md`：交付表字段来源。
- `references/platform-submission.md`：SOLO2 认证、双文件上传、开关、轮询与返修。
- `references/final-delivery-format.md`：单题完成汇报格式。
- `references/lessons-learned.md`：实测记录。
- `references/graphics-topics.md`：上一期图形方向资料，仅作兼容保留，不是本期默认方向。

## 完成判定

`status` 十一项全绿：

1. 初始快照
2. GitHub 初始快照
3. 提示词（含难度与 ra-人话门禁）
4. 单轮会话
5. PromptID
6. 产物快照
7. GitHub 产物快照
8. 轨迹时间线
9. 运行录屏（720p、≤90 秒；优先 window-id 证据，手工导入必须明确标为 manual-import；
   页面可达且有页面功能变化时必须有对应页面的真实操作与结果，并使用独立 Chrome 实例且 profile 清理通过；
   真实启动失败且页面不可达时允许 Terminal 失败录屏；使用过的终端和临时会话都必须关闭并清理）
10. Feedback（基于证据起草、专家复核且格式检查通过；`5` 分必须通过严格满分审计）
11. Excel

自动提交开关打开时，平台阶段还要求 submission API 最终返回 `QC_PASSED`。本地十一项通过但平台
仍在 `SUBMITTED`/质检中、`PENDING_FIX` 或终态失败时，整项任务不得标记完成。

录屏画面、轨迹、产物复核、交付完整性分数和 Feedback 必须描述同一事实。视频显示失败时不能写成功；
产物显示需求未完成时不能靠模型自述翻案。

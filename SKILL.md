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

- 一题 = 一个新容器 = 一个 session = 一轮有效交互 = 一条数据。提示词只发一次，不追问、不纠错、不发“继续”。
- 全程 `auto` 模型。镜像固定 `ANTHROPIC_MODEL=auto_model/urm`、`CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000`，
  技能不覆盖模型或上下文配置。
- SOLO2 账号只允许一个所属领域，子方向不锁。新建任务、写提示词和提交前都必须读取配置中的
  `submission.lockedDomain` 作为唯一账号大类；该值一经设置即只读，不能为了适配单题用
  `config set`、直接修改任务状态或更换交付表标签。
- 账号大类决定可选子方向集合。必须先读真实工程，再从该大类的合法子方向中选择最匹配的一项；
  如果工程无法真实归入任一同大类子方向，直接拒绝提交。可以匹配时，只把大类保持为配置值，
  子方向填写实际匹配项。平台历史中的首条提交领域只用于交叉核验，不能反向授权跨域提交。
- 难题只收「困难」「地狱」。首轮 170 字以内优先，绝对上限 240 字。
- 结构不完整（401/API/网关报错、无最终 `end_turn`、无故中断）按规范重新 roll：
  恢复初始快照 → 新容器 → 新会话，默认最多 3 次；仍失败就换题。结构完整但质量差的产物不重跑，如实评价。
  结束标志不等于真的做完：`run`/`collect` 还会读最终回复和轨迹本身，最后一条还在向用户提问、
  还在说"接下来我会…"、有工具调用没拿到结果、出现上下文耗尽或超时痕迹、回复断在句子中间的，
  一律按未完成处理并重新 roll。判定结果写进 `attempts[].completion`。
- 录屏按用户可见功能判定，不按题目自称“纯后端”或改动文件位置判定。页面能够启动时，新增/修改页面、
  页面交互、页面展示字段或状态，或者后端结果需要在页面呈现时，正式录屏必须包含该页面从入口到对应功能结果的
  真实操作；只打开页面、页面返回 200 或仅验证接口都不算页面证据。若应用因真实启动错误在页面可达前就失败，
  则使用 `terminal` 计划并设 `expectedAppFailure=true`，完整保留实际失败过程即可，不要求启动 Chrome；
  不得另起一套成功环境或拼接另一条路径的页面来凑页面证据。
- 初始快照在首轮前提交并 push；产物快照在运行后提交并 push。初始快照必须是产物快照祖先，
  模型自己的提交保留，不 squash，不 force-push/rebase。
- `.gitignore` 自动补齐 `.env`、密钥、依赖目录和构建产物；发现疑似凭据被跟踪时 `init` 直接失败。
- 轨迹和录屏当前在本地，Excel 填绝对路径；上传对象存储后再替换链接。
- 「备注」列在任何情况下都保持为空。交付表固定写空值，SOLO2 新建、上传和返修 payload 都剔除
  `other_issues`，不得把环境异常、运行过程或补充说明写进备注。
- SOLO2 自动提交默认关闭。开关关闭时只生成本地交付并允许人工核验后执行 `submit --execute`；
  打开后 `export` 自动上传轨迹和录屏、提交并轮询质检。
- `SUBMITTED` 不是完成；只有 `QC_PASSED` 才算平台通过。`PENDING_FIX` 必须返修原记录，
  `QC_REJECTED`、`QC_DISCARDED`、`QC_FAILED` 或轮询超时都必须保留证据并停止。

## 证据与文案边界

本期明确要求「AI 给出的结论必须经过验证，文案自然流畅、无 AI 痕迹」。因此：

- Agent 必须读取原始轨迹、产物 diff、真实运行结果和录屏后再形成判断。模型最终回复、自述测试通过、
  文件看起来存在，都不能单独当证据。
- Agent 可以起草交付完整性分数和 Feedback，但每个文件名、函数、命令、报错、接口状态和前后计数
  都要回查到轨迹或本机真实复核结果。
- 代码支撑不等于在反馈里罗列代码文件名。产物段优先把核心代码行为写成“成功路径、失败路径、
  状态切换和可见结果”；代码文件、函数和完整调用链留在审核材料。无论是否写出代码标识，
  结论都必须先经过代码审查和真实回读，不能用功能措辞替代证据。
- 已经由代码和运行结果确认的能力要使用确定表达，例如“能够”“可以”“能”“完成了”“已实现”，
  不要只留下“统一切换”“清楚展示”这类名词化短语，让读者猜实现是否真的完成。未验证或未完成的部分
  仍按事实描述，不能为了让句子肯定而拔高结论。
- 标注专家必须复核分数、事实和措辞。Agent 不得把未经运行验证的猜测写成结论，也不得为了“看起来自然”
  编造触发步骤或业务影响。这条不靠自觉：`feedback --check` 的事实层会把正文里的数值、文件路径、
  反引号标识符和报错原文逐条回查轨迹、产物快照和 `verify` 记录，查不到就阻断。
  要把一个说法变成结论，补一次真实运行；不要改数字去贴近已有输出。
- Feedback 同时覆盖过程与产物。负面判断写清触发节点、实际行为、客观结果和业务影响；高分也要写
  逐项核对的需求和真实运行结果。
- Feedback 是给人读的短复盘，不是验收报告：默认 180–280 个非空字，语句通畅，读一遍就能懂。
  代码信息（文件名、函数名、字段名、命令）和具体数值少写，能换成业务行为、前后变化和页面或接口可见结果就换；
  删掉后读者定位不了问题的（出错的具体位置、页面或接口直接出现的报错原文、题面点名的接口、本身就是缺陷的数字）
  可以保留，一般一两处。其余细节留在轨迹、diff 与 `monitor/runtime-verification.json`。
- 交付完整性默认高质量上限为 `4`。`5` 不是“没触发错误”的奖励，必须满足
  `references/score-calibration.md` 的严格条件，并填写 `monitor/score-review.json`；
  存在未固化的回归、未覆盖边界、分页/规模限制或任何已知缺口时评 `4`。
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

## 出题规则

开始设计题面前：

1. 按实际领域读取对应出题参考：数据工程读 `references/data-engineering-topics.md`，
   游戏图形与多媒体参考 `references/graphics-topics.md`，安全围绕漏洞攻防、身份认证、
   通信安全或系统安全的真实工程边界。任一大类下的全部合法子方向都可以用于出题。
2. 读真实源码，至少找到 2–3 个相关业务文件、现有数据流和一条可观察的失败后果。
3. 读 `references/prompt-standard.md` 和 `references/difficulty-standard.md`。
4. 运行 `prompt --init` 生成题面草稿、`difficulty-review.json` 和 `ra-renhua-review.json`。
5. 只有难度论证能逐项回指题面原句和真实源码文件时才允许安装提示词。

困难题至少满足平台三项特征中的两项，并且必须有关键设计取舍或复杂技术关注点之一；
地狱题三项全满足，另有开放方案、隐蔽约束、架构级取舍、深水调试或跨系统恢复证据。
“在现有模块加字段/筛选/结果表”不能靠文件数自称困难。

提示词像业务方交代问题，不写文件路径、函数名、修法、端口、容器方案或具体技术栈。
硬性措辞最多 3 处，分号最多 2 个；不写轮次和模板收尾。固定版本 `ra-人话` 审核记录必须与最终题面 hash 一致。

## 一次性配置

```bash
S=~/.codex/skills/solo-2611/scripts/solo2611.py
python3 $S config init
python3 $S config set claude.baseUrl=<网关地址> claude.apiKey=<本期团队Key>
python3 $S config test
python3 $S config show
python3 $S config set submission.autoSubmit=false
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

2. **prompt --init**：

   ```bash
   python3 $S prompt --task-root ROOT --init
   ```

   生成 `delivery/题面草稿.md`、`monitor/prompt/difficulty-review.json`、
   `monitor/prompt/ra-renhua-review.json`。

3. **prompt**：

   ```bash
   python3 $S prompt --task-root ROOT \
     --file ROOT/delivery/题面草稿.md \
     --task-type 功能开发 --difficulty 困难 \
     --lang "Python, PostgreSQL, Redis"
   # --domain 只能与配置中的账号大类一致；--sub-direction 必须是该大类下且与工程真实匹配。
   ```

   默认读取上一步两份 JSON。门禁包括长度、技术栈/端口/文件路径、硬性措辞、历史连续片段、
   难度证据、真实源码文件、跨对象不变量、实质复杂度、中等形态自辩和 ra-人话 hash。
   脚本会实际调用 `~/.codex/skills/solo-annotation-loop/scripts/validate-prompt.py` 与
   `audit-human-writing.py`；固定版本校验器缺失时直接阻断，不能只靠手填审核 JSON。
   确有必要超过 170 字时加 `--allow-over-170`，仍不能超过 240 字。
   通过后题面写入 `delivery/提示词.md`，首轮执行后不能再改。
   所属领域必须与 `submission.lockedDomain` 一致，任务初始化后不得通过参数改大类。
   如果找不到能真实匹配当前工程的同大类子方向，停止并换题；不要依赖表单里显示的其他领域选项。

4. **repo**：

   ```bash
   python3 $S repo --task-root ROOT
   ```

   用 `github.accessTokens` 新建公开仓库并把初始快照推到 `main`。Token 不写进任何文件。
   仓库名不再直接使用中文任务名：技能会先把常见业务词转成英文，剩余中文转拼音，再加 4 位随机后缀，
   确保名称可读且不会被 GitHub 压成 `--xxxx`。该规则只作用于后续新建仓库，不改动历史仓库。

5. **run**：

   ```bash
   python3 $S run --task-root ROOT
   ```

   官方镜像 `print` 模式逐字发送题面，默认超时 7200 秒。结束后导出完整 Claude projects 目录，
   校验恰好一个主会话、题面字节一致、最终 `end_turn`、SessionID 和 PromptID；随后自动打产物快照、
   生成 `delivery/轨迹时间线.md` 并快进推送同一仓库。

   需要手工终端流程时用 `interactive`，结束后运行 `collect`。

6. **读证据并真实复核**：

   读取：

   - `traces/-workspace/<sessionId>.jsonl`
   - `delivery/轨迹时间线.md`
   - 初始快照到产物快照的 diff
   - 本机重新执行产物得到的构建、启动、任务输出、接口回读或对账结果

   只把真实执行、文件变更和客观输出写进结论。模型自述“已完成”但没有对应修改或运行证据时，
   按虚假成功处理。

7. **record**：

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

8. **verify → feedback**：

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
   错误必须改掉；代码和数值的警告逐个判断，能换成业务说法的换掉，删了就定位不了问题的保留。
   核对结果写在 `monitor/feedback-evidence.json`，含索引摘要和逐条声明的核对状态。

9. **export**：

   ```bash
   python3 $S export --task-root ROOT
   ```

   生成 `delivery/交付表.xlsx`（官方 19 列 + 内部记录）和 `delivery/字段说明.md`。
   缺快照 permalink、轨迹、录屏、Feedback 或关键字段时拒绝导出。
   每日汇总用 `export-batch`，会自动跳过未完成题并检查 SessionID/题面重复。

10. **submit / repair**：

   从本地工作台获取 Solo2 账号，实际密码从 Keychain `solo-auto/solo2.password` 读取；
   登录后 Cookie 和 CSRF 只放 Keychain。提交上传当前主会话 JSONL 和正式录屏两个文件，
   其余字段按服务端动态 schema 从交付表映射。完整协议见
   [references/platform-submission.md](references/platform-submission.md)。
   预检以配置中的账号大类为准，再用账号历史的首条提交领域交叉核验。大类不一致、配置缺失，
   或题目无法匹配该大类的任一合法子方向时，都在上传前阻断；禁止修改大类标签后硬提交，
   也不能通过返修或另建记录绕过。

   ```bash
   python3 $S submit --task-root ROOT               # 默认 dry-run；开关打开时自动执行
   python3 $S submit --task-root ROOT --execute     # 人工确认后提交
   python3 $S submission-status --task-root ROOT --submission-id ID
   ```

   `PENDING_FIX` 时读取质检原文和锁定字段，修订允许修改的 Feedback、评分或录屏；
   重新导出时使用 `export --no-submit`，随后调用：

   ```bash
   python3 $S repair --task-root ROOT --submission-id ID \
     --comment "按质检要求补充证据" --execute --watch [--refresh-video]
   ```

   `QC_PASSED` 时才结束平台流程；返修始终更新原记录，不能另建重复记录。
   备注 `other_issues` 永远不进入新建或返修 payload，即使服务端 schema 或原记录中存在该字段。

11. **status / cleanup**：

    `status` 检查十一项交付；`cleanup` 只删除本题容器，交互容器未 collect 时拒绝。

单题完成后按 `references/final-delivery-format.md` 汇报。

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
    └── score-review.json                # 仅考虑 5 分时填写；严格满分审计
```

## CLI 速查

```bash
python3 $S init --task-name NAME (--source DIR | --git URL [--ref REF]) [--tasks-dir DIR]
python3 $S prompt --task-root ROOT --init
python3 $S prompt --task-root ROOT --file F --task-type T --difficulty 困难|地狱 --lang "..." \
  [--domain 领域 --sub-direction 子方向 --review FILE --difficulty-review FILE --allow-over-170]
# 子方向必须属于所选领域；数据工程可选采集/RPA、数仓与数据湖、数据分析与数据应用。
python3 $S repo --task-root ROOT [--name 前缀] [--private]
python3 $S run --task-root ROOT [--attempts 3] [--timeout 7200] [--force] [--skip-repo]
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

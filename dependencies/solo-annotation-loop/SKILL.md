---
name: solo-annotation-loop
description: "管理人工步进的多轮 Solo 项目评审：可直接从 Solo Manager 查询并选用可用项目、下载任务包，也可接收用户已下载的 ZIP；读取项目真实结构，按 Bug 修复、0-1 代码生成、Feature 迭代、代码理解、代码重构、工程化或代码测试生成不超过 240 字的中文提示词，强制通过 ra-人话去 AI 味审核、字符与语义双重去重和本地多维提示词缓存，定位 Claude Code/Codex CLI 真实轨迹，核验产物、评分，并在结束后审核、上传 Solo Manager 与提交 SOLO2。用户需要出题、继续一轮评审、定位轨迹、评分、提交平台或维护满意度交付表时使用。"
---

# Solo 多轮评审

## 总原则

按“项目画像 → 任务类型选题 → 提示词确认 → 用户手动执行 → 真实轨迹定位 → 产物核验 → 五维评分 → Excel 追加 → 等待确认”的顺序工作。

只根据实际文件、命令输出和原生轨迹下结论。不能把模型最终回复当成唯一证据，也不能猜测没有验证过的过程或结果。

默认中文、人工步进、Claude Code。不得自动发送模型消息、创建会话、覆盖初始快照、提交代码或推送仓库。GitHub 发布只有用户明确要求时才调用 `pg-code` 规则。

提示词和评分文案写作必须使用固定版本的 `ra-人话`，也就是每轮都要实际过一次“说人话”。提示词不能从模板或分析稿直接展示给用户，必须先按 `ra-人话` 的 Rewrite Workflow 改写，再保存审核 JSON，并通过 `scripts/audit-human-writing.py` 和 `scripts/validate-prompt.py`。只有审核通过后，才允许生成语义去重审核、运行 `platform_bridge.py check-prompt` 和向用户展示提示词。字符级检查通过后，语义审核仍为 `DUPLICATE`、`REVIEW_REQUIRED`、缺失或过期时，一律不得放行。只写审核记录或只跑扫描不算过一次“说人话”。审核不过就重写，最多三轮；三轮后仍不过时停止提交。完整口径见 `references/ra-renhua-gate.md`。

## 平台直选与任务包模式（优先）

进入平台模式后，不要要求用户手工打开管理页找项目、下载 ZIP，也不要要求用户再提供项目路径或任务 ID。技能有两种入口：

1. **技能直选（默认）**：用户给出任务类型，或再给出项目编号、关键词、领域、分类等条件，运行：
   `python3 scripts/platform_bridge.py select --workdir <当前目录> --task-type <类型> [--project-code <编号>] [--keyword <关键词>] [--domain <领域>] [--category <分类>]`
   脚本会查询 Solo Manager，按「先我的项目、后项目池」的顺序选用：
   1. 先查 `GET /projects/mine`（归属自己的项目列表），按项目编号顺序挑第一个符合条件的；
   2. 我的项目里没有可用的，再查 `GET /projects`（公共项目池）；
   3. 两种情况都跳过当前有容器正在运行的项目，判定来源为 `docker ps` 的容器名 `claude-<项目编号>-<时间戳>`；docker 无法判定时退回作业目录记录的容器状态，宁可少选也不并发跑同一项目。
   可用条件保持不变：未禁用、`RUNNABLE`、有源码快照、对应任务类型剩余配额大于零；同名多变体优先使用目录名排序后的第一个可用变体。选中后下载平台生成的本地任务包，调用 bootstrap 创建任务并占用配额，把选中记录（含 `candidateStage`、`excludedRunningProjects`）写入 `PROJECT_DIR/platform/selection.json`，把任务包保留在 `PROJECT_DIR/platform/packages/`。显式指定 `--project-code`/`--project-id` 时若该项目正在容器中执行，直接报错拒绝，不静默改选其他项目。
2. **用户提供 ZIP**：用户已经把 Solo Manager 下载的任务包交给技能时，直接运行下面的 bootstrap。

无论从哪个入口进入，都必须遵守下面的平台约束：

1. bootstrap 会校验 ZIP、读取 `.solo-platform.json`、解出 `source/`、复制 `history_prompt/`，创建或复用项目使用目录；如果下载包来自“下载本地任务包”预览且尚无任务 ID，脚本会通过平台 API 自动创建任务并占用配额。
2. 平台令牌只从环境变量 `SOLO_MANAGER_TOKEN`、macOS Keychain `solo-manager-token` 或 `~/.codex/solo-manager.json` 读取，不得从 ZIP、日志或对话中读取。首次使用运行 `scripts/configure-platform-keychain.sh` 完成一次性配置。
3. 源码必须在 `PROJECT_DIR/source/` 独立初始化 Git，首个提交固定为 `chore: initial environment snapshot`；GitHub 的创建与推送由最终 `platform_bridge.py submit` 统一处理。
4. 技能必须先读取 `history_prompt/index.json` 和各提示词文件，并运行 `python3 scripts/platform_bridge.py solo2-history --project-dir PROJECT_DIR` 拉取 SOLO2 同项目全部历史 `user_prompt`，写入 `checks/platform/solo2-prompt-history.json`，并每次刷新项目缓存 `checks/platform/prompt-history-cache.json` 和全局缓存 `~/.codex/solo2-auto/prompt-cache/`；缓存包含项目、repo、任务编号、轮次、任务类型、提示词哈希和四字片段索引。SOLO2 访问受阻时改用当前项目缓存继续去重，缓存也不可用才停机。起草时必须参考历史列表避开重复。候选提示词先实际过一次 `ra-人话` 并生成 `checks/human-writing/rNN-prompt.json`，再用 `validate-prompt.py` 校验通过。随后读取本地 `history_prompt/*.txt` 和 SOLO2 历史缓存，完成语义审核并写入 `checks/semantic-dedup/rNN.json`；可先用 `python3 scripts/platform_bridge.py prompt-semantic-init --project-dir PROJECT_DIR --round N` 生成模板。之后必须运行：
   `python3 scripts/platform_bridge.py check-prompt --project-dir PROJECT_DIR --prompt PROJECT_DIR/prompts/00N.txt`
   提示词去重同时执行字符级和语义级门禁：检查 `ra-人话` 审核、本地历史、SOLO2 同项目历史和语义审核记录。没有字符审核或语义审核、任一历史 `user_prompt` 读取不完整、提示词哈希或历史数量不一致，或任一来源完全一致、连续 18 个有效字符重复、达到雷同阈值、语义重复/待复核时，都必须重写或停机。
5. 轮次完成后先按原流程核验、评分和 ra-人话审核，然后运行：
   `python3 scripts/platform_bridge.py submit --project-dir PROJECT_DIR --round N`
   该命令会发布 GitHub 初始快照与本轮提交、追加项目专属 Excel、执行交付审核，调用 solo-push 提交 SOLO2，并尽力上传标准轮次包到 Solo Manager。
6. SOLO2 成功后才进入 `waiting_confirmation`；Solo Manager 失败时保留本地证据和成功平台结果，报告原始失败原因，不得伪造成功状态。

## 作业目录与项目目录（强制）

技能工作目录默认是用户启动技能时的当前目录，除非用户明确指定。每个项目每次独立使用都必须创建单独的项目使用目录：

`<workdir>/<projectId>[-<taskTypeSlug>]-<sequence>`

- 用户提供任务类型时带任务类型标签，例如 `ld-601-代码生成-1`；未提供时只用项目编号和序号，例如 `ld-601-1`。
- 任务类型标签：`0-1代码生成 → 代码生成`、`feature迭代 → 功能迭代`，其余枚举保持原名。
- `sequence` 表示该项目在作业目录下第几次独立使用，从一递增；优先查看已有项目使用目录，不得复用其他项目的序号。
- 同一容器和会话的多轮任务必须复用同一个项目使用目录；新的独立任务要创建新的序号目录。
- 项目目录固定结构：

```text
<project-dir>/
├── workspace.json
├── state.json
├── source/
├── project-profile.json
├── prompts/
│   ├── 001.txt
│   ├── 001-draft.json
│   └── 001-spec.json
├── rounds/
│   └── r01/
│       ├── round-locator.json
│       ├── round-score.json
│       └── operation-manual.md
├── traces/
│   └── <sessionId>.jsonl
├── delivery/
│   ├── <projectId>-用户满意度交付.xlsx
│   └── delivery.json
└── checks/
    ├── human-writing/
    │   ├── r01-prompt.json
    │   └── r01-comments.json
    └── comment-evidence/
        └── r01.json
```

提示词、画像、状态、轨迹副本、轮次定位、评分、操作手册和项目专属交付表必须保存在项目目录内，禁止继续散落在作业目录根目录。全局交付表只能作为可选汇总，不作为新任务的主存储。已有散落数据一并迁移到对应项目使用目录后再继续。

## 触发后的工作顺序

1. 读取 `references/workflow.md`、`references/safety-rules.md` 和 `references/project-workspace.md`。
2. 如果用户没有直接提供 ZIP，优先运行 `platform_bridge.py select` 从平台获取可用项目包并 bootstrap；如果用户直接提供平台下载的 ZIP，则运行 bootstrap。两种平台入口都不再手工调用 `init-project-workspace.py`。其他情况下才用 `scripts/init-project-workspace.py` 创建或复用 `PROJECT_DIR`。用户给了任务类型就写入目录名，没给就不要补造。有源码时使用 `--source` 和 `--clone-source` 把源码独立克隆到项目目录的 `source/`。多轮续跑使用脚本的 `--reuse`。
3. 运行 `scripts/inspect-project.py` 建立项目画像，输出必须写入 `PROJECT_DIR/project-profile.json`。至少检查 README、依赖清单、入口文件、核心业务文件和测试文件。
4. 按 `references/disabled-projects.md` 检查禁用范围；命中即拒绝该项目并停止本轮，不生成提示词。可运行 `scripts/check-disabled.py` 辅助判定。
5. 确认七类任务中的一种，再读取 `references/prompt-strategies.md` 对应段落。根据项目真实功能选择题点，禁止用固定模板硬套；选题与提示词同样不得落入禁用范围。
6. 平台任务包模式先读取 `history_prompt/` 并运行 `platform_bridge.py solo2-history` 刷新历史，再用 `scripts/draft-prompt.py` 生成草稿，按 `references/ra-renhua-gate.md` 执行去 AI 味改写并生成 `checks/human-writing/<轮次>-prompt.json`。完成语义审核并写入 `checks/semantic-dedup/rNN.json` 后，运行 `platform_bridge.py check-prompt` 同时执行字符级和语义级去重。草稿、最终提示词和配置必须分别写入 `PROJECT_DIR/prompts/001-draft.json`、`PROJECT_DIR/prompts/001.txt`、`PROJECT_DIR/prompts/001-spec.json`。用 `scripts/validate-prompt.py <prompt> --review <审核JSON>` 校验，审核不过先重写，最多三轮。提示词必须是中文纯文本、不超过 240 字，不写路径、类名、函数名、代码块或直接修复步骤，也不写端口配置、约束技术栈的描述、指定 Docker 或容器方案的要求和「用户xxx」主语句式。
7. 初始化状态到 `PROJECT_DIR/state.json`，并在 `PROJECT_DIR/delivery/` 建立项目专属交付表。首轮回复可以直接贴出完整提示词正文和完整 Docker 启动命令，再附项目目录、提示词文件、源码路径和当前 SessionID，由用户手动执行。第二轮及以后，用户面回复只给一行任务类型和完整提示词，不输出容器、SessionID、PromptID、文件路径等键值信息。状态进入 `waiting_user`，此时停止，不替用户启动容器或发送消息。
8. 用户通知本轮结束后，运行 `scripts/locate-trace.py --project-dir PROJECT_DIR --copy-trace`，将定位 JSON 写入 `PROJECT_DIR/rounds/r01/round-locator.json`，并把轨迹 JSONL 复制到 `PROJECT_DIR/traces/`。必须找到本轮 user 消息、PromptID、时间和最终 `end_turn`；找不到就报告不可核验。
9. 运行 `scripts/verify-round.py` 或项目专用真实命令，核对 diff、测试、构建、健康检查和任务验收项。页面行为只有真实浏览器或自动化测试验证过才可写入结论。
10. 生成轮次评分 JSON 和操作手册，分别写入 `PROJECT_DIR/rounds/rNN/round-score.json` 和 `PROJECT_DIR/rounds/rNN/operation-manual.md`。评分前先建立 `checks/comment-evidence/rNN.json`，把每个维度的每句事实绑定到本轮轨迹行、文件行、命令输出或源码覆盖范围。评语写完按 `ra-人话` 改写并生成 `checks/human-writing/<轮次>-comments.json`，再用 `scripts/validate-comments.py <round-score.json> --review <审核JSON>` 校验。五维评语的每一条都必须写明具体操作步骤或工具调用，并引用文件名、函数名或可执行命令；只有抽象结论的描述会被校验脚本拒绝。评语与轨迹无关、方向相反、把失败写成成功或无依据的缺失判断一律禁止提交。评分前必须做一次反例审视，重点检查业务不变量、状态边界、权限归属、并发竞争、失败路径和事务回滚；评分 JSON 必须填写 `gate`。任何四分或五分都要经过 `scripts/validate-score-gate.py` 校验。SOLO2 不接受五维均分大于 `4.2` 的记录；原始均分大于 `4.2` 时必须先做高分解剖，找到真实缺陷后把最终均分降到 `4.2` 或以下，没有真实扣分点时不得造假，改为放弃该条数据并结束任务。评分规则见 `references/scoring-rubric.md`，证据索引格式见 `references/comment-evidence.md`。
11. 平台任务包模式不单独执行本步，由 `platform_bridge.py submit` 统一创建或复用 GitHub 仓库、提交初始快照和本轮 commit 并校验可达。其他模式仍按固定提交信息 `round <N>: <sessionId>` 提交，作者和提交者使用 `~/.codex/pg-code/github-context.json` 的 `gitAuthor`；只有用户明确要求发布时才推送。
12. 平台任务包模式由 `platform_bridge.py submit` 自动追加项目专属交付表、提交 SOLO2 并尽力上报 Solo Manager，不重复调用 `update-delivery.mjs`。其他模式使用 `scripts/update-delivery.mjs` 追加项目专属交付表。先查重，写入后必须回读目标范围、扫描公式错误并渲染预览。只有用户明确要求维护全局汇总表时，才额外追加到全局表。推送前运行 `scripts/audit-delivery.py` 做交付前最终审核，检查串题、轨迹路径、提交时间、初始环境快照、操作系统等枚举、评语格式和 ra-人话审核。审核不通过时，文案问题必须重写并重新审核，不能只改交付表绕过。
13. 平台任务包模式在 SOLO2 提交成功后，状态进入 `waiting_confirmation`，并把两平台结果写入 `state.json` 和 `rounds/rNN/`；Solo Manager 失败只作为上报失败保留。只有用户明确确认后才生成下一轮提示词。后续轮次继续写入同一 `PROJECT_DIR` 的 `rounds/rNN/`，不得新建项目使用目录；新的独立使用才创建新序号目录。

## 后续轮次输出格式（强制）

第二轮及以后，用户面回复只保留两段：

第一行写成 `任务类型 <类型>`，不使用冒号，例如 `任务类型 Bug修复`。

空一行后直接给完整提示词。提示词必须独立成立，不得出现“第几轮、本轮、上一轮、下一轮、轮次”等表述。不得出现 `键：值`、`key: value` 或类似的逐项键值格式，不得输出容器名、SessionID、PromptID、文件路径、父记录等元数据。完整信息仍写入项目使用目录，由技能内部维护。

## 首轮交接输出（强制）

进入 `waiting_user` 的回复必须让用户不打开文件也能直接执行：

1. 原样贴出经校验的完整提示词正文，不能只给路径，不能摘要或临时改写。
2. 直接贴出完整的隔离 Claude Code Docker 启动命令，严格按 `references/isolated-claude-docker.md` 生成。命令必须创建 `run-<项目编号>-<随机后缀>` 运行目录和 `claude-<项目编号>-<时间戳>` 容器名；bind mount 的工作区启动时必须完全为空，包括隐藏文件；容器里的 Claude 进程就绪后再自动导入初始代码，并在导入完成后提示用户发送题目；同时保留 `--init`、`--restart=no`、`--cap-drop ALL`、`--security-opt no-new-privileges`。
3. 并行任务必须给每个容器独立终端、独立运行目录和唯一容器名，不得共享终端变量或运行目录。
4. 同一回复内再贴出会话结束后的轨迹导出和容器清理命令，确保容器不自动删除且轨迹可在删除前取回。Key 只在运行时从 macOS Keychain 读取。
5. 不得输出或持久化真实 API Key。每台设备必须有独立用户名和独立 Keychain service，启动命令从 `~/.codex/benzhi-claude-device.json` 解析当前设备身份，再从对应 Keychain 条目读取。不能复用共享的 `benzhi-claude-code` 条目。
6. 再列运行目录、提示词文件、项目路径和 SessionID。SessionID 尚未产生时写明待启动会话后从原生轨迹提取。
7. Docker 命令和提示词都只交给用户手动执行，不替用户启动容器、发送消息或创建会话。

## 初始环境快照（必做：仅本地提交）

每个新项目在生成首轮提示词前，必须先在本地把原始源码仓库固定为初始环境快照。本步骤只做本地 git 提交，不发布 GitHub；GitHub 发布是可选步骤，只在用户明确要求后执行。

1. 目标项目必须已有本地 git 仓库和一次干净初始提交，提交信息固定为 `chore: initial environment snapshot`，工作区无未提交变更。
2. 提交作者与提交者严格使用 `~/.codex/pg-code/github-context.json` 的 `gitAuthor.name` / `gitAuthor.email`，不要用全局默认身份；提交前确认 `.gitignore` 已排除 `.env`、依赖目录、构建产物、日志和缓存。
3. 记录完整 40 位 commit SHA 到状态文件和交付字段。交付表「初始环境快照」在本阶段填该 commit SHA；只有完成 GitHub 发布后，才替换为可达 permalink。
4. 只有用户明确要求发布 GitHub 时，才读取 `~/.codex/pg-code/github-context.json` 获取凭据，永远不输出 token；仓库名优先用项目英文短名，被占用时用 `<短名>-solo`，保持 public、默认分支 `main`。
5. 推送后必须用宿主 API 校验提交 URL 返回 HTTP 200，并把仓库 URL 与提交 URL 回填到状态文件和交付字段。

## 禁用项目范围

游戏和图形、本地和桌面工具、平台和业务系统、数据可视化和前端四类项目必须拒绝；完整清单与判定口径见 `references/disabled-projects.md`。画像命中即拒绝该项目；选题和生成提示词时也必须避开这些范围，避免把题目改写成这些常见类型。

## 任务类型选择

任务类型只能使用以下枚举：`Bug修复`、`0-1代码生成`、`feature迭代`、`代码理解`、`代码重构`、`工程化`、`代码测试`。提示词重点和验收项见 `references/prompt-strategies.md`。

项目画像和提示词配置使用模板 `templates/project-profile.json`、`templates/prompt-spec.json`。项目事实不足时输出“信息不足”，不能补写虚构的报错、接口、用户或测试结果。

选题必须避开禁用范围；提示词生成后回读 `promptText` 再对一遍 `references/disabled-projects.md`，命中即丢弃该题，更换真实候选点重做。

提示词正文有四条硬规则，命中即重写，`scripts/human_writing.py` 与 `scripts/validate-prompt.py` 会机器拦截：

1. 不写任何端口配置：不出现端口号、`监听`、`localhost`、`127.0.0.1`、`PORT=`，也不要求执行者选择或修改端口；端口沿用项目现有配置，验证按实际配置执行。
2. 不约束技术栈：不指定语言、框架、中间件、数据库、构建工具、依赖和版本，也不写「技术栈」「技术选型」「依赖版本」这类约束口径；技术栈事实只写入项目画像，题目沿用项目现有工程。
3. 不指定 Docker 或容器方案：不要求使用 Docker、Docker Compose、Kubernetes、镜像、容器化或容器编排完成任务，也不把新增或改造容器环境写成验收条件。Docker 只属于本技能的隔离运行 Harness，不能写进任务提示词。
4. 不用「用户xxx」主语句式：不把「用户」「使用者」当第三人称主语（如「用户可以…」「用户点击…」），改写成使用者视角的直接需求；「候补用户」「多用户」这类名词用法和管理员、教师、学生这类真实业务角色不受限。

所有轮次的任务难度只允许填写“困难”或“地狱”，当前禁止使用“简单”和“中等”。难度定义如下：

- “困难”：需要整合多个模块或系统约束，做关键设计取舍，并处理复杂状态、兼容性、权限、并发、性能或异常链路。
- “地狱”：需要极高决策复杂度。需求通常开放或约束隐蔽，依赖深度项目理解、架构级判断、多步复杂调试验证，或大量边界场景；即使 SOTA 模型也大概率失败。

首轮就要围绕跨模块、跨流程或跨系统的主要难点设计，不能通过缩小范围来降档；后续轮次也必须保持困难或以上。写提示词前先按定义逐项核对，不能把中等题只改标签。`地狱`题仍要有真实可验证目标，不能靠含糊、缺信息或不可复现制造失败。历史记录里的“中等”在重新提交前按“困难”迁移和填报，迁移后仍要按困难定义复核。用户明确指定同一容器多轮或总轮数时，按用户机制继续。后续轮次可以做关联功能、扩展能力或新的验证目标，不要求每轮都来自上一轮缺陷；但后续轮次如果是在检查上一轮已交付范围内的功能，发现问题时必须回溯修正上一轮评分。

## 平台返修

交付表写入后，如果 SOLO2 返回 `PENDING_FIX`，不要新建一条提交，也不要沿用旧提交路径。先修正交付表中的评分描述，重新执行 ra-人话审核并更新对应审核 JSON，再用 `solo-push/scripts/fix_submission.py` 调用 `PUT /submissions/{id}` 提交返修并轮询重新质检。`GET /submissions/{id}` 只用于读取详情，不会触发返修。

## 轮次和交付约定

状态文件固定放在项目使用目录的 `state.json`，状态固定为：

`prepared → project_profiled → prompt_ready → waiting_user → round_located → verified → scored → delivery_updated → waiting_confirmation`

每轮单独记录 SessionID、PromptID、轨迹绝对路径、开始时间、结束时间和结束标志。断联后重新触发的输入只能是“继续”两个字，并且不算轮次：不递增轮次编号、不新增交付表行、不改变本轮提示词和 PromptID，只在轨迹里记录续跑次数。同一任务的多轮继续使用当前容器和当前终端，不退容器重建；新任务必须新建空运行目录、唯一容器名和独立容器。第一轮父记录留空，后续轮次填写上一轮 PromptID。项目专属交付表默认采用当前 28 列模板（含「轮次」和「提交地址」）和五维评分，保存在项目使用目录的 `delivery/`；全局汇总表只在用户明确要求时维护。字段映射与枚举可通过配置覆盖。每轮核验评分后提交一次产物 commit，并把 commit 地址写入交付表「提交地址」；同一上下文（同一容器/会话）所有轮次的「初始环境快照」保持一致，只填一次初始基线。

## 资源导航

- 禁用项目范围：`references/disabled-projects.md`
- 项目画像和证据：`references/project-profiling.md`
- 七类提示词策略：`references/prompt-strategies.md`
- 五维评分和评论：`references/scoring-rubric.md`
- 去 AI 味写作与审核：`references/ra-renhua-gate.md`
- Claude/Codex 轨迹适配：`references/trace-adapters.md`
- Excel 字段和写入：`references/delivery-schema.md`
- 项目目录规则：`references/project-workspace.md`
- 多轮状态：`references/workflow.md`
- 隔离 Claude Code Docker 命令：`references/isolated-claude-docker.md`
- 安全边界：`references/safety-rules.md`
- 当前设备身份：`scripts/device-identity.py`
- macOS Claude Keychain：`scripts/configure-macos-keychain.sh`
- macOS 平台令牌：`scripts/configure-platform-keychain.sh`
- 平台直选项目并下载任务包：`scripts/platform_bridge.py select`
- 平台桥接：`scripts/platform_bridge.py`
- 平台接入说明：`references/platform-integration.md`
- 提示词语义去重：`references/prompt-semantic-dedup.md`
- 标准脚本：`scripts/init-project-workspace.py`、`scripts/init-project-delivery.py`、`scripts/inspect-project.py`、`draft-prompt.py`、`audit-human-writing.py`、`validate-prompt.py`、`validate-comments.py`、`locate-trace.py`、`verify-round.py`、`round-status.py`、`init-round.py`、`check-disabled.py`、`commit-round.py`、`audit-delivery.py`、`update-delivery.mjs`

# 工作流

## 作业目录规则

每个项目使用实例都必须放在独立目录：

`<workdir>/<projectId>[-<taskTypeSlug>]-<sequence>`

项目目录内固定保存提示词、画像、状态、轨迹副本、轮次定位、评分、操作手册和项目专属交付表。命名、目录结构、序号规则和迁移方式见 `project-workspace.md`。

## 平台任务包模式

用户直接提供 Solo Manager 下载的 ZIP 时，先运行：

`python3 scripts/platform_bridge.py bootstrap --package <zip> --workdir <workdir>`

提示词生成前必须运行 `platform_bridge.py check-prompt`。评分和 ra-人话审核完成后，不得再单独调用 `commit-round.py` 或 `update-delivery.mjs`，统一运行：

`python3 scripts/platform_bridge.py submit --project-dir <PROJECT_DIR> --round <N>`

该命令负责 GitHub 快照与轮次提交、Excel 追加和 SOLO2 提交，并尽力上报 Solo Manager。SOLO2 成功即可进入 `waiting_confirmation`；Solo Manager 失败只保留报告。完整说明见 `platform-integration.md`。

## 状态机

每个项目和轮次都维护一个 `state.json`。状态只能按下面顺序前进，失败时保留失败证据并停在当前状态：

`prepared`：确认项目目录和交付表。

`project_profiled`：画像包含真实技术栈、入口、测试、构建和候选模块。

`prompt_ready`：提示词已保存，通过字数、格式、事实检查和 ra-人话审核。

`waiting_user`：等待用户在已有 Harness 会话里手动执行。

`round_located`：从原生轨迹找到对应 PromptID 和 `end_turn`。

`verified`：完成真实源码、运行、测试和构建核验。

`scored`：写好本轮五维评分和简短评论，并通过 ra-人话审核。

`delivery_updated`：Excel 已追加并回读确认。

`waiting_confirmation`：等待用户决定是否进入下一轮。

## 初始化

先运行：

`python3 scripts/init-project-workspace.py --workdir <workdir> --project-id <id> [--task-type <type>] [--source <source> --clone-source]`

得到 `PROJECT_DIR` 后再运行：

`python3 scripts/init-round.py <project> --project-id <id> --task-type <type> --output <PROJECT_DIR>/state.json`

初始化只写项目目录内的状态文件，不发送模型消息。脚本发现已有相同 SessionID + PromptID 时必须拒绝覆盖。

项目专属交付表使用：

`python3 scripts/init-project-delivery.py --template <template.xlsx> --output <PROJECT_DIR>/delivery/<id>-用户满意度交付.xlsx`

## 人工节点

提示词生成后，回复必须直接包含经校验的完整提示词正文和完整的隔离 Claude Code Docker 运行块，不能只给文件路径。Docker 只属于运行 Harness，不能写进提示词或作为任务验收要求。运行块按 `references/isolated-claude-docker.md` 生成，必须创建 `run-<项目编号>-<随机后缀>` 运行目录、使用 `claude-<项目编号>-<时间戳>` 容器名并保留 `--init`、`--restart=no`、`--cap-drop ALL`、`--security-opt no-new-privileges`；bind mount 的工作区启动时必须完全为空；容器里的 Claude 进程就绪后再自动导入初始代码，导入完成后用户再发送题目。并行任务必须各占一个终端、一个独立运行目录和一个唯一容器名。同一回复还要给出会话结束后的轨迹导出和容器清理命令。API Key 统一从当前设备独立 macOS Keychain 条目读取，不能回显或写入命令历史，也不能复用其他设备的共享条目。随后再列运行目录、提示词文件、项目路径和 SessionID，SessionID 尚未产生时写明待会话启动后从原生轨迹提取。所有命令只由用户手动执行，不替用户启动容器、发送消息或创建会话。用户说“第一轮结束”或“第二轮结束”只是触发核验的信号，不能替代轨迹检查。轨迹定位时必须提供 `--project-dir` 和 `--copy-trace`，定位结果和轨迹副本都保存在项目使用目录。

提示词初稿必须先过 ra-人话。按 `ra-renhua-gate.md` 改写后生成 `checks/human-writing/rNN-prompt.json`，再运行 `validate-prompt.py <提示词文件> --review <审核JSON>`。不通过就重写，三轮仍不通过时停止，不得进入 `prompt_ready`。

同一任务的多轮留在当前容器和当前终端继续；新任务必须新建空运行目录和唯一容器，不能复用已退出容器。

评分前必须填写评分 JSON 的 `gate` 并通过 `scripts/validate-score-gate.py`。任何四分或五分都要有反例审视和证据，五分还要有关键路径及对抗性验证记录。五维描述必须先写 `checks/comment-evidence/rNN.json`，把每句事实绑定到轨迹行、文件行、命令输出或源码覆盖范围，再过 ra-人话，并在每条中写明具体操作或工具调用，同时引用文件名、函数名或命令，生成 `checks/human-writing/rNN-comments.json`，并用 `validate-comments.py <round-score.json> --review <审核JSON>` 校验，否则不能进入 `scored`。评语与轨迹无关或方向相反时直接阻断提交。评分前必须主动做反例审视。后续轮次发现前轮真实缺陷时，必须回溯修正前轮评分、状态历史、操作手册和交付表，不得保留虚高分。

第二轮及以后，用户面只输出两段：第一行用 `任务类型 <类型>`，不使用冒号；空一行后直接输出完整提示词。禁止使用 `键：值`、`key: value` 或类似键值格式，不再重复容器、SessionID、PromptID 和路径等信息。

SOLO2 不接受五维均分大于 `4.2` 的记录；原始均分大于 `4.2` 时仍按停止规则视为最后一轮，但提交前必须先做高分解剖。找到真实缺陷后把最终均分降到 `4.2` 或以下；没有真实扣分点时不得造假，改为放弃该条数据并结束任务。评分、提交和交付表写入必须全部指向当前 `PROJECT_DIR`，不得散落到作业目录根目录。评分完成后不自动生成下一轮。用户确认后，使用上一轮缺口或新的真实候选点生成下一轮提示词。所有轮次按困难或地狱难度设计。困难题要整合多个模块或系统约束，做关键设计取舍，并处理复杂状态、兼容性、权限、并发、性能或异常链路；地狱题要有极高决策复杂度，依赖开放或隐蔽约束下的深度项目理解、架构级判断、多步复杂调试验证或大量边界场景，即使 SOTA 模型也大概率失败。难度要按真实复杂度判定，不能只换标签；地狱题仍要可客观验证。历史“中等”在重新提交前迁移为“困难”并按困难定义复核；首轮拿到 4 到 5 分只说明本轮完成质量较好，不再自动推定难度不足。用户明确要求同一容器多轮或指定总轮数时，应继续执行该机制，也可以安排关联的新功能或扩展目标。若后续轮次发现上一轮已交付范围内的严重缺陷，必须回溯修正上一轮评分和交付记录，避免前后矛盾。



## 平台返修

SOLO2 打回后的返修使用 `PUT /submissions/{id}`，不是重新调用新建接口 `POST /submissions`。先修正交付表字段，文案有变化时重新跑 ra-人话审核并更新对应 JSON，再调用 `solo-push/scripts/fix_submission.py`，并轮询到最终质检状态。读取详情使用 `GET /submissions/{id}`。

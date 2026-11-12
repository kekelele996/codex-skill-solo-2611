# 交付表

## 存储位置

项目专属交付表是默认主存储，固定放在项目使用目录：

`<project-dir>/delivery/<projectId>-用户满意度交付.xlsx`

同一项目的多轮记录写入同一文件。只有用户明确要求时才额外维护全局汇总表，不能把全局汇总表当成分散项目数据的默认落点。

## 默认字段

默认 28 列：

`User Prompt, SessionID, TurnID/PromptID, 轮次, 初始环境快照, 提交地址, 轨迹文件, 环境可复现等级, Harness, Harness 版本, 操作系统, 任务类型, 任务难度, 语言/框架, 交付完整性, 交付完整性 - 描述, 指令遵循, 指令遵循 - 描述, 任务规划, 任务规划 - 描述, 推理能力, 推理能力 - 描述, 执行能力, 执行能力 - 描述, 其他问题, 提交人, 提交时间, 父记录`

- `轮次`：本轮是第几轮，从 1 开始；后续轮次递增。
- `初始环境快照`：整个上下文（同一容器/会话）的初始基线快照的可达 permalink；同一上下文所有轮次填同一个值，不随轮次变化。每轮自己的产物提交写进 `提交地址`。
- `提交地址`：本轮产物提交到 GitHub 仓库的 commit 网页地址；每轮核验评分后提交一次，用 `scripts/commit-round.py` 生成。
- `语言/框架`：也就是交付表的技术栈列，只写四个主要项，按重要程度排序（语言 → 核心框架 → 主要存储 → 主要前端），超过四项的其余依赖不要写。写表脚本和提交脚本都会自动截断到四项，`audit-delivery.py` 会在超过四项时报错。

字段按表头名称定位，不写死列号。新模板可通过 `delivery-config.json` 覆盖字段映射和枚举，但不得静默丢字段。

## 写入流程

使用 `@oai/artifact-tool` 导入现有工作簿。第一次编辑前运行：

`node container_tools/mark_artifact_operation_started.mjs --operation-kind edit --expected-output-count 1 --output-format xlsx`

追加前检查 `repoId + SessionID + PromptID` 是否重复；复制上一行格式后写入数据；调用 `workbook.recalculate()`；检查目标区域和公式错误；渲染预览；导出后重新导入回读。

Excel 写入失败不得覆盖原文件，应保留备份并报告失败原因。轨迹文件一律写项目目录内 `traces/` 副本的绝对路径。项目专属交付表创建可运行 `scripts/init-project-delivery.py`。


## 字段枚举

服务端 SOLO2 校验的枚举必须按原值填写，不能自行改写：

- `操作系统`：`MacOS/Linux`、`Windows`。Docker 容器里跑 Claude Code 时，宿主是 macOS 就填 `MacOS/Linux`，不要填 `Linux (Debian 12, Docker)` 这类描述文本。
- `环境可复现等级`：`无外部依赖`、`有外部依赖，未容器化`、`已容器化，可一键起环境`
- `Harness`：`Claude Code`、`Codex CLI`
- 五维均分不能大于 `4.2`。原始均分大于 `4.2` 时，必须先完成高分解剖并真实降分；找不到真实扣分点时放弃该条数据，不能硬扣分或造假。
- `任务难度`：只允许 `困难`、`地狱`。所有轮次都不能使用 `简单` 或 `中等`；历史记录里的 `中等` 在重新提交前按 `困难` 迁移，不能直接沿用旧值提交，迁移后仍按困难定义复核。
- `困难`：需要整合多个模块或系统约束，做关键设计取舍，并处理复杂状态、兼容性、权限、并发、性能或异常链路。
- `地狱`：需要极高决策复杂度。需求通常开放或约束隐蔽，依赖深度项目理解、架构级判断、多步复杂调试验证，或大量边界场景；即使 SOTA 模型也大概率失败。
- `任务类型`：`Bug修复`、`0-1代码生成`、`feature迭代`、`代码理解`、`代码重构`、`工程化`、`代码测试`

## 交付前最终审核

每轮追加交付表后、推送前，运行 `scripts/audit-delivery.py` 做最终审核。审核目录默认是项目根下的 `checks/human-writing`，一轮需要 `rNN-prompt.json` 和 `rNN-comments.json`：

```bash
python3 scripts/audit-delivery.py --delivery <project-dir>/delivery/<projectId>-用户满意度交付.xlsx --sheet 交付记录 --human-review-root <project-dir>/checks/human-writing
```

审核项：串题（轨迹 user 消息与轮次逐字对应）、轨迹存在且为绝对路径、提交时间为 ISO 文本、初始环境快照为 GitHub 四十位 commit 永久链接、关键字段非空、操作系统等字段枚举正确、评语不少于六十字且无括号引号反引号和阿拉伯数字。每条五维评语还必须同时提供可回指的证据：写明第几步操作或工具调用，并引用文件名、函数名或可执行命令；只有抽象结论、没有这些具体线索的评语一律不合格。提示词和五维评语还要重新做 ra-人话硬禁扫描，并核对审核 JSON 的版本、哈希和未解决问题。文案审核不过先重写并重新审核，不能只修交付表或绕过审核记录；不得带问题推送。`--allow-missing-human-review` 只允许用于查看历史记录，不能用于新提交。

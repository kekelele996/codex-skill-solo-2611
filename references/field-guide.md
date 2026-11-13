# 交付表字段说明（solo-2611）

| 字段 | 来源 | 说明 |
|---|---|---|
| User Prompt | `delivery/提示词.md` 原文 | 与轨迹里的真人消息逐字一致（`run`/`collect` 校验） |
| 所属领域 | `prompt --domain`，默认 数据工程 | 必须与 SOLO2 账号的 `submission.lockedDomain` 一致；账号按第一条提交锁定领域 |
| 子方向 | `prompt --sub-direction`，默认 数仓与数据湖 | 必须选择该领域下的任意合法子方向；数据工程有三类可选 |
| 任务类型 | `prompt --task-type` | 11 个一级标签之一，按本轮主要意图 |
| 任务难度 | `prompt --difficulty` | 只允许 困难 / 地狱 |
| 语言/框架 | `prompt --lang` | 逗号分隔，如 `Python, PostgreSQL, Celery`；题面本身不写技术栈 |
| Harness | 固定 Claude Code | |
| Harness 版本 | 轨迹 user 消息的 `version` | 当前镜像为 2.1.303 |
| 操作系统 | 固定 MacOS/Linux | Mac 宿主 + Linux 容器 |
| 环境可复现等级 | `prompt --repro`（选填） | 无外部依赖 / 有外部依赖，未容器化 / 已容器化，可一键起环境 |
| 初始环境快照 | `init` 时的提交，`repo` 推到 GitHub 后的 permalink | `https://github.com/<owner>/<repo>/commit/<40位SHA>`；未推送时留空并阻断导出 |
| SessionID | 轨迹 `sessionId` | |
| TurnID/PromptID | 轨迹真人消息的 `promptId` | |
| 轨迹文件 | `traces/-workspace/<sessionId>.jsonl` 绝对路径 | 本地交付阶段填本机路径；上传对象存储后替换为下载链接。整个 `traces/` 目录保留原结构 |
| 产物快照 | 运行结束后的 HEAD，自动快进推送后的 permalink | 模型自己提交过就在其后补一个提交收尾，不 squash；初始快照必为其祖先 |
| 运行录屏 | `recording/运行录屏.mp4` 绝对路径 | 720p、≤90 秒；窗口 ID 证据和 cursor guard 同时保留 |
| 交付完整性 | `delivery/feedback.md`（Agent 证据化起草、专家复核） | 1–5 整数；常规高质量上限为 4，5 需要严格满分审计 |
| Feedback | `delivery/feedback.md`（Agent 证据化起草、专家复核） | 过程与产物都要有可核验事实 |
| 备注 | 最终主会话轨迹的明文 API Key 风险扫描 | 默认空值；命中时固定为 `轨迹中有存在明文apikey暴露风险` |

SOLO2 实际提交时，`轨迹文件` 通过 `kind=trace` 上传，`运行录屏` 通过 `kind=video` 上传；
其余字段按服务端 `form-schema` 的动态 label 映射。`other_issues`（备注）默认从 payload 中剔除；
只有当前主会话轨迹命中明文 API Key 风险时才写入固定说明，且不得写入 Key 原文。
登录 Cookie/CSRF 只存 Keychain，不写交付表。

第二个工作表「内部记录」只用于团队管理（任务目录、尝试次数、SHA、录屏时长、Feedback 检查警告等），提交时按需删除。

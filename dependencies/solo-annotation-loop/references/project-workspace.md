# 作业目录与项目使用目录

## 目标

同一个作业目录下可能先后评审多个项目。项目画像、提示词、状态、轨迹、轮次定位、评分、操作手册和交付表必须按项目使用实例隔离，禁止堆在作业目录根目录。

## 目录命名

项目使用目录固定为：

`<workdir>/<projectId>[-<taskTypeSlug>]-<sequence>`

- `workdir` 默认是用户启动技能时的当前目录。
- 用户给出任务类型时保留任务类型标签。`0-1代码生成` 简写为 `代码生成`，`feature迭代` 简写为 `功能迭代`，其余枚举保持原名。
- 用户没有给出任务类型时只使用 `<projectId>-<sequence>`。
- `sequence` 是该项目在同一个作业目录下第几次独立使用，从一递增。
- 同一个容器和会话的多轮任务复用一个项目使用目录；新的独立任务才递增序号。

示例：

```text
ld-601-代码生成-1
ld-601-代码生成-2
gb-537-1
gb-537-Bug修复-2
```

## 固定结构

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

后续轮次继续放在同一个项目目录下：

```text
prompts/002.txt
prompts/002-spec.json
rounds/r02/...
```

## 初始化

创建新项目使用目录：

```bash
python3 scripts/init-project-workspace.py \
  --workdir <workdir> \
  --project-id <projectId> \
  --task-type <taskType> \
  --source <sourcePath> \
  --clone-source
```

同一会话继续下一轮时复用：

```bash
python3 scripts/init-project-workspace.py \
  --workdir <workdir> \
  --project-id <projectId> \
  --task-type <taskType> \
  --reuse
```

未提供任务类型时省略 `--task-type`。提供源码时建议同时使用 `--clone-source`，把当前使用实例的源码独立克隆到项目目录的 `source/`。

## 平台任务包

从 Solo Manager 下载的 ZIP 由 `platform_bridge.py bootstrap` 展开。项目目录额外包含：

- `history_prompt/`：历史提示词副本。
- `info.md`：首轮任务类型与源码快照信息。
- `.solo-platform.json`：平台项目、任务、轮次和 API 地址。

源码必须位于 `PROJECT_DIR/source/`，Git 仓库只初始化在该目录，不要覆盖已有用户提交。

## 交付表

每个项目使用目录内维护独立交付表，优先写入：

`<project-dir>/delivery/<projectId>-用户满意度交付.xlsx`

不要默认追加到全局汇总表。只有用户明确要求时才维护全局汇总表，并且局部交付文件仍然是主存储。

可用下面脚本从现有 28 列模板创建工作簿：

```bash
python3 scripts/init-project-delivery.py \
  --template <template.xlsx> \
  --output <project-dir>/delivery/<projectId>-用户满意度交付.xlsx
```

## 轨迹

运行 `locate-trace.py` 时使用 `--project-dir` 和 `--copy-trace`，保证轨迹 JSONL 在项目目录内留有副本，定位 JSON 中的 `tracePath` 必须指向该副本。

## 迁移旧数据

发现历史任务仍散落在作业目录根目录时，先按项目编号和任务序号确定项目使用目录，再迁移以下内容：

- 提示词和提示词配置进入 `prompts/`
- 状态和项目画像进入项目根目录
- 定位、评分和操作手册进入对应 `rounds/rNN/`
- 提示词和评语的 ra-人话 审核记录进入 `checks/human-writing/`
- 五维评语的逐句证据索引进入 `checks/comment-evidence/`
- 轨迹副本进入 `traces/`
- 项目专属交付表进入 `delivery/`
- 迁移完成后更新 JSON 中的绝对路径，不能只移动文件不改引用

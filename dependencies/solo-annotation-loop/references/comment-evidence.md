# 五维评语证据索引

## 红线

每个维度的每句事实都必须能在本轮轨迹、真实文件、命令输出或源码覆盖范围中回指。评语与轨迹完全无关、方向相反、把失败写成成功、把未执行写成已执行，均禁止提交。

评分前先把本轮 `promptId` 范围内的证据整理成：

```text
<project-dir>/checks/comment-evidence/rNN.json
```

`validate-comments.py` 和 `audit-delivery.py` 会硬校验该文件。缺失、句事实无绑定、引用行不存在、引文不匹配、正负极性相反时，提交直接失败。

## 写作顺序

1. 从本轮原始 JSONL、diff、源码、测试和运行输出中逐条抄出原文。
2. 给每条事实写一个不跨句的 `claim`，文字必须逐字出现在维度描述里。
3. 用 `trace_quote`、`file_quote`、`command_result` 或 `source_scope` 绑定证据。
4. 检查 claim 与引文的极性。正面结论只能引用通过或成功原文，失败原文只能支持负面结论。
5. 写完维度描述后运行 `validate-comments.py`，再运行提交前审核。

## 文件格式

```json
{
  "schemaVersion": 1,
  "round": 2,
  "sessionId": "本轮 SessionID",
  "promptId": "本轮原始 PromptID",
  "dimensions": {
    "delivery": [
      {
        "claim": "go test 中的迁移用例覆盖空环境",
        "kind": "trace_quote",
        "path": "traces/本轮轨迹.jsonl",
        "lineStart": 710,
        "lineEnd": 730,
        "quote": "TestMigrateFreshEnvironment",
        "polarity": "positive"
      }
    ],
    "instruction": [],
    "planning": [],
    "reasoning": [],
    "execution": []
  }
}
```

`path` 可以使用项目目录相对路径或绝对路径。轨迹、文件和命令结果必须填写真实存在的起止行，`quote` 必须逐字出现在该范围内。每个维度的每一句事实都要有覆盖它的 claim，纯分数收束句可以省略绑定。

`round`、`sessionId` 和 `promptId` 必须与当前轮次、交付行和 `round-score.json` 一致。跨轮复用同一份证据索引会被硬拦截。

## 缺失类判断

“没有、未覆盖、未进入、缺少、不存在”这类判断不能只靠检索不到一句话来证明。必须使用 `source_scope`，并列出目标文件实际包含的完整符号集合：

```json
{
  "claim": "迁移失败后的恢复路径未进入本轮交付用例",
  "kind": "source_scope",
  "path": "source/backend/internal/database/migrate_test.go",
  "symbols": [
    "TestMigrateFreshEnvironment",
    "TestMigrateUpgradeFromLegacy"
  ],
  "complete": true,
  "polarity": "negative"
}
```

脚本会重新扫描目标文件。`complete` 为 true 时，文件中实际测试符号必须与 `symbols` 完全一致，防止用“检索不到”代替覆盖证明。

## 已发生打回对应的禁止项

- 轨迹只有 `docker: command not found` 和 SQLite 验证时，不得写真实 MySQL 补验完成。
- 轨迹没有浏览器工具调用时，不得写浏览器复跑成功或页面已真实复现。
- 轨迹只执行一次依赖安装时，不得写出现过重试。
- 一次测试先失败、修复后才通过时，不得笼统写所有命令一次退出成功。
- 非满分必须写出已核验的扣分点；只列完成项不能提交。
- 非满分维度至少要有一条 `negative` 或 `mixed` 证据；全部为 `positive` 时，扣分依据缺失，直接阻断提交。
- 执行能力只写“退出码为零、真实接口调用正常、页面展示正常”属于典型错误；必须补充具体失败、异常、行为偏差、返工或未验证后果。
- 负面缺失判断必须给出文件级完整覆盖范围，不能只写“轨迹里没有”。

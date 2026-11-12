# 提示词语义去重门禁

提示词去重包含字符级检测和语义级检测。字符级通过不等于最终通过。

## 本地缓存与回退

每次获取 SOLO2 或本地提示词时，必须同步构建多维缓存：

- 项目缓存：`PROJECT_DIR/checks/platform/prompt-history-cache.json`
- 全局缓存：`~/.codex/solo2-auto/prompt-cache/<项目键哈希>.json`

缓存维度：

- `projectCode`
- `repoId`
- `taskNo`
- `roundNo`
- `taskType`
- `promptSha256`
- 提示词四字片段关键词

SOLO2 网络、认证、超时或服务异常时，按当前项目匹配键优先从缓存恢复同项目 SOLO2 记录继续去重；缓存没有 SOLO2 记录时再使用缓存中的本地历史。结果必须标记：

```json
{
  "source": "local_cache",
  "cacheStale": true,
  "fallbackReason": "TimeoutError: ..."
}
```

缓存没有当前项目记录时直接停止，不能因为 SOLO2 不可用就跳过去重。

## 检查顺序

1. 运行 `platform_bridge.py solo2-history`，刷新 SOLO2 同项目历史。
2. 读取本地 `history_prompt/*.txt` 和 `checks/platform/solo2-prompt-history.json`。
3. 对候选提示词和历史提示词做语义比对，写入 `checks/semantic-dedup/rNN.json`。
4. 运行 `platform_bridge.py check-prompt`。
5. 只有字符级和语义级都通过，才允许启动模型。

## 语义比对维度

逐条比较以下内容：

- 业务目标
- 目标模块
- 修改动作
- 交付物
- 使用边界和验收重点

只替换同义词、调整语序、拆分或合并句子，但核心需求相同，判为语义重复。  
同一模块下提出不同的缺陷、功能、范围或交付目标，不算语义重复。  
无法确定时写 `REVIEW_REQUIRED`，不得直接放行。

## 同仓库缺陷族红线

同一仓库内，只要候选与历史提示词满足下列组合，就视为同一缺陷族：

- 业务目标相同：都要求修正同一字段、同一金额语义或同一回填逻辑。
- 目标模块相同：都落在同一实体、接口、页面或业务流程。
- 修改动作相同：都要求回填、补算、修正或同步同一组数据。
- 交付物相同：用户可见结果仍是同一组金额、状态、列表、详情或结算数据。

触发时机、边界状态、错误文案、首次或再次发生，只改变描述，不构成新主题。  
例如“旧记录第一次退款前回填”与“旧记录已部分退款后启动回填”仍属于同一缺陷族，必须判为重复，不能拆成新轮次。

判定为同一缺陷族时：

- 不得启动新轮次，不得只改措辞后再次提交。
- 能并入当前轮次验证范围的，应先并入当前轮次再提交。
- 已经形成独立提交且平台判废后，必须更换到不同模块、不同数据对象或不同交付物，并重新做全部去重。

字符级额外门禁：候选和历史都包含修复、回填、补回、兼容等修订动作时，整体字符相似度达到 `0.42` 或四字片段重合度达到 `0.34`，直接按 `SIMILAR` 阻断。

## 审核文件

路径：

```text
PROJECT_DIR/checks/semantic-dedup/rNN.json
```

可以先运行模板命令：

```bash
python3 scripts/platform_bridge.py prompt-semantic-init \
  --project-dir PROJECT_DIR \
  --round N
```

模板生成后，必须完成语义审核并修改 `decision`、`summary`、`matches`。

结构：

```json
{
  "schemaVersion": 1,
  "roundNo": 1,
  "promptPath": "/absolute/path/prompts/001.txt",
  "promptSha256": "当前提示词归一化后的 SHA256",
  "reviewedSourceCounts": {
    "local": 3,
    "solo2": 2
  },
  "decision": "OK",
  "summary": "逐条比较后，没有发现业务目标、模块、动作、交付物和边界相同的需求。",
  "matches": []
}
```

发现语义重复时：

```json
{
  "schemaVersion": 1,
  "roundNo": 1,
  "promptPath": "/absolute/path/prompts/001.txt",
  "promptSha256": "当前提示词归一化后的 SHA256",
  "reviewedSourceCounts": {
    "local": 3,
    "solo2": 2
  },
  "decision": "DUPLICATE",
  "summary": "候选提示词与历史提交属于同一需求和同一交付范围。",
  "matches": [
    {
      "source": "solo2",
      "reference": "submission:11484",
      "overlap": ["业务目标", "目标模块", "修改动作", "交付物"],
      "reason": "两轮都要求为现有地勤任务模块增加同一条超时升级流程。"
    }
  ]
}
```

## 硬校验

`check-prompt` 会校验：

- 语义审核文件存在且是 JSON 对象。
- `schemaVersion` 为 `1`。
- `roundNo` 与当前轮次一致。
- `promptSha256` 与当前提示词一致，防止审核后偷改提示词。
- `reviewedSourceCounts.local` 与当前本地历史数量一致。
- `reviewedSourceCounts.solo2` 与当前 SOLO2 同项目历史数量一致。
- `decision` 只能是 `OK`、`DUPLICATE` 或 `REVIEW_REQUIRED`。
- `summary` 和每条 `matches` 证据完整。

字符级 `EXACT` 或 `SIMILAR` 会直接阻断。字符级没有命中时，语义审核必须是 `OK` 才能通过。  
语义 `DUPLICATE`、`REVIEW_REQUIRED`、审核文件缺失、哈希过期或历史数量不一致，都会阻断启动和提交。

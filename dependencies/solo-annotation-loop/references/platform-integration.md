# Solo Manager 平台接入

## 目标

用户只给任务类型或筛选条件，技能可以直接从 Solo Manager 查询、选用并下载任务包；用户也可以把一个已经下载的 ZIP 任务包交给技能。技能负责：

1. 解包并创建标准 `PROJECT_DIR`。
2. 把源码放入 `source/`，历史提示词放入 `history_prompt/`。
3. 读取 `.solo-platform.json`，补齐项目、任务和轮次关系。
4. 生成首轮提示词时做历史去重。
5. 轮次结束后，审核并提交 SOLO2，同时尽力上报 Solo Manager；Manager 失败只记录报告。

## 凭证

平台令牌按以下顺序读取：

1. 环境变量 `SOLO_MANAGER_TOKEN`
2. macOS Keychain service `solo-manager-token`
3. `~/.codex/solo-manager.json` 的 `token` 字段

录入 Keychain：

```bash
bash scripts/configure-platform-keychain.sh
```

也可以手工执行：

```bash
security add-generic-password -a "$USER" -s solo-manager-token -U -w
```

这是一次性配置。之后用户可以直接给出任务类型、项目编号或筛选条件，也可以把已经下载的 ZIP 交给技能，不再需要手工设置任务 ID。

不得把令牌写进任务包、项目文件、日志或用户回复。

## 直接从平台选用项目

用户只给任务类型，或再给项目编号、关键词、领域、分类时，技能不需要先让用户手工下载 ZIP。直接运行：

```bash
python3 scripts/platform_bridge.py select \
  --workdir /absolute/path/workdir \
  --task-type 0-1代码生成 \
  --project-code gb-532
```

可用筛选项：

- `--project-code`：精确指定项目编号。
- `--project-id`：精确指定平台项目 ID。
- `--keyword`：按项目编号、名称或业务领域搜索。
- `--domain`、`--category`：按业务领域或项目主题筛选。
- `--dry-run`：只查询并显示将选中的项目和变体，不下载、不创建任务、不扣配额。
- `--package-output`：额外把平台任务包保留到指定路径。

选项目顺序固定为「我的项目 → 项目池」：

1. 先查 `GET /api/v1/projects/mine?page=1&size=100`，这是归属自己的项目列表；按项目编号顺序取第一个符合条件的项目。JWT 通常已识别当前用户；平台要求显式用户名时可设置 `SOLO_MANAGER_USERNAME`，请求会自动追加 `username` 参数。
2. 自有项目里存在原本符合当前任务类型、但当前正在容器或项目占用锁中执行的候选时，必须等待释放后重查，不得回退公共池。只有自有项目为空、`/projects/mine` 不可用，或自有项目确实没有符合当前任务类型的候选时，才回退查 `GET /api/v1/projects`（公共项目池），规则相同。
3. 两个阶段都跳过当前有容器正在执行的项目。判定方式优先用 `docker ps` 的容器名 `claude-<项目编号>-<时间戳>` 反解项目编号；docker 不可用时退回作业目录 `*/auto/state.json` 里未清理的容器记录，此时会把「容器未标记 removed」的项目一并跳过，宁可少选也不并发跑同一项目。

符合条件的项目必须同时满足：

- 项目未禁用。
- 状态为 `RUNNABLE`。
- 存在 `sourceAvailable=true` 且带源码快照的变体。
- 指定任务类型的剩余配额大于零。

选中的来源阶段和跳过的运行中项目会写进 `PROJECT_DIR/platform/selection.json` 的 `candidateStage` 与 `excludedRunningProjects` 字段。显式指定 `--project-code` 或 `--project-id` 时，若目标项目正在容器中执行，命令直接报错拒绝，不会改选其他项目。

命令会下载 Solo Manager 的本地任务包，调用 bootstrap，创建平台任务并占用配额，最后把任务包保存到：

```text
PROJECT_DIR/platform/packages/<项目编号>-<任务编号>-source-package.zip
```

选中依据、任务 ID、轮次 ID、源码快照和包哈希保存在：

```text
PROJECT_DIR/platform/selection.json
```

## 启动

```bash
python3 scripts/platform_bridge.py bootstrap \
  --package /absolute/path/to/local-task-package.zip \
  --workdir /absolute/path/to/workdir \
  [--base-url http://192.168.31.147:8080]
```

平台下发的任务包里，`.solo-platform.json` 的 `baseUrl` 是服务端自己写的地址，经常是 `http://localhost:8080`。bootstrap 会按下面的优先级改写元数据，避免后续接口调用指回不可达的本机地址：

1. 传了 `--base-url` 就用它（`select` 下载后会自动传实际使用的地址）；
2. 没传时，包内地址不是 `localhost`／`127.0.0.1`／`0.0.0.0` 就沿用包内地址；
3. 否则回落到默认平台地址（`SOLO_MANAGER_BASE_URL` 可覆盖，默认 `http://192.168.31.147:8080`）。

改写结果写进 `PROJECT_DIR/.solo-platform.json`、`state.json` 和 `workspace.json`，bootstrap 输出里的 `packageBaseUrl`、`baseUrlSource`、`baseUrlRewritten` 记录改写前后的对比。`select` 保留下来的 `platform/packages/*.zip` 也会同步改写包内元数据。

脚本行为：

- 校验 ZIP 路径安全和解压后大小。
- 必读根目录 `.solo-platform.json` 和 `info.md`。
- 归一化元数据里的平台地址（见上）。
- 如果任务包有 `taskId`，复用对应任务和首轮。
- 如果任务包来自 preview 且没有 `taskId`，调用平台创建任务，正式占用配额。
- 调用 `init-project-workspace.py` 创建或复用 `PROJECT_DIR`。
- 把包内 `source/` 解到 `PROJECT_DIR/source/`。
- 把包内 `history_prompt/` 解到 `PROJECT_DIR/history_prompt/`。
- 复制 `.solo-platform.json` 和 `info.md` 到项目目录。
- 在 `source/` 初始化 Git 并创建 `chore: initial environment snapshot`。
- 建立项目专属交付 Excel 和 `state.json`。

## 提示词去重

生成候选提示词后，先生成并按真实历史完成语义审核：

```bash
python3 scripts/platform_bridge.py prompt-semantic-init \
  --project-dir PROJECT_DIR \
  --round N
```

语义审核规则和 JSON 结构见 `references/prompt-semantic-dedup.md`。完成 `checks/semantic-dedup/rNN.json` 后运行：

```bash
python3 scripts/platform_bridge.py check-prompt \
  --project-dir PROJECT_DIR \
  --prompt PROJECT_DIR/prompts/001.txt
```

脚本会：

- 读取本地 `history_prompt/`。
- 调用 SOLO2 历史接口，按当前项目编号和 repo 名搜索全部提交，读取完整历史 `user_prompt`。历史列表保存到 `checks/platform/solo2-prompt-history.json`，起草前必须参考。
- 每次取得提示词后，同时更新项目缓存 `checks/platform/prompt-history-cache.json` 和全局缓存 `~/.codex/solo2-auto/prompt-cache/<项目键哈希>.json`。缓存按 projectCode、repoId、taskNo、roundNo、taskType、promptSha256 和四字片段建立多维索引。
- SOLO2 网络、认证、超时或服务异常时，自动按当前项目键检索本地缓存继续去重；结果标记 `source=local_cache`、`cacheStale=true` 和 `fallbackReason`。缓存也没有命中时直接停止，不能跳过去重。
- NFKC、大小写、空白归一化后完全一致时拒绝。
- 去掉空白和标点后，连续 18 个及以上有效字符重复时拒绝。
- 整体字符相似度达到 `0.88`，或四字片段重合度达到 `0.82` 时按雷同拒绝。
- 字符级未命中时，还必须校验语义审核文件 `checks/semantic-dedup/rNN.json`；`DUPLICATE`、`REVIEW_REQUIRED`、缺失、哈希过期或历史数量不一致都拒绝。
- 返回码 `0` 表示字符级和语义级都通过；返回码 `1` 表示重复、语义重复或语义待复核。
- 除本地 `history_prompt/` 外，还必须比对 SOLO2 同项目历史提示词；任一来源判定 EXACT、SIMILAR 或语义重复都禁止继续。
- 检查结果保存到 `PROJECT_DIR/checks/platform/prompt-check.json` 和按轮次的 `prompt-check-rNN.json`。

重复时必须重写提示词，不能继续执行。

## 最终双提交

轮次核验、评分、`ra-人话` 审核和评分门禁都通过后运行：

```bash
python3 scripts/platform_bridge.py submit \
  --project-dir PROJECT_DIR \
  --round N
```

执行顺序固定为：

1. 再次校验 prompt、comments 和 score gate。
2. 创建或复用 GitHub 仓库，提交并推送初始快照。
3. 提交并推送本轮 commit。
4. 将初始快照和 commit 地址回填 locator、score 和 state。
5. 追加项目专属交付 Excel。
6. 运行 `solo-push/scripts/audit_before_push.py`。
7. 运行 `solo-push/scripts/push_delivery.py --round N --submit` 提交 SOLO2，这是硬提交目标。
8. 同时尽力构建轮次包并上传 Solo Manager；失败时保留错误报告，不阻断 SOLO2。
9. 把结果写入：
   - `rounds/rNN/platform-submit-result.json`
   - `state.json` 的 `platformSubmission`
   - `state.json` 的 `solo2Submission`

SOLO2 成功即可进入 `waiting_confirmation`。Solo Manager 失败时保留成功结果与原始错误报告，不把失败伪装成成功，也不阻断 SOLO2。

## 标准轮次包

由桥接脚本生成并上传，包含：

```text
manifest.json
prompt.txt
round-locator.json
round-score.json
verification.json
reviews/prompt.json
reviews/comments.json
traces/trace.jsonl
workspace.zip
delivery/delivery.xlsx
```

平台侧仍会执行任务归属、源码快照、文件 Hash、路径安全和提示词重复校验。

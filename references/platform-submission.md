# SOLO2 提交、轮询与返修

## 认证

- 平台：`https://solo2.jzxhnh.com`
- 表单：`GET /api/v1/submissions/form-schema`
- 上传：`POST /api/v1/submissions/upload`
- 新建：`POST /api/v1/submissions`
- 详情与轮询：`GET /api/v1/submissions/{id}`
- 返修：`PUT /api/v1/submissions/{id}`

提交平台地址、账号和密码存在性只从 `http://127.0.0.1:8099/api/system/config` 读取；认证会话只从
`POST http://127.0.0.1:8099/api/system/solo2/session` 获取。工作台是凭据的唯一所有者，并由工作台
使用其保存的密码完成登录。

严格禁令：

- 技能进程禁止执行 `security`、禁止直接读取系统 Keychain，也禁止读取或复用
  `SOLO2_COOKIE` / `SOLO2_CSRF` / `SOLO2_PASSWORD`。
- 禁止持久化 Cookie、CSRF 或密码，禁止跨进程复用登录会话。
- 每次提交、查询和返修都必须重新从工作台取得临时会话。
- 会话返回的账号必须与 `solo2.username` 完全一致，平台地址必须与 `solo2.base_url` 的同源地址一致。
  任一不一致立即停止；不能把“还能访问”当作身份正确的证据。

## 上传

只上传两个原始文件：

- 轨迹：multipart `file`，`kind=trace`，必须是当前主会话唯一的 `.jsonl`，上限 27MB。
- 录屏：multipart `file`，`kind=video`，必须是 1280x720、不超过 90 秒的视频，上限 500MB。

轨迹上传结果写入 `trace_file` 数组；视频上传结果的 URL 写入 `screencast`。

## 表单

表单由服务端动态返回，不能写死字段。`solo-2611` 从交付表第二行读取官方 19 列表头，
按 schema 的 `label` 映射到 `field_key`；同时校验必填项、选项、URL、数字范围和父子字段联动。
当前表单 fingerprint 每次提交前现场读取。

`other_issues`（备注）无论服务端是否返回、是否标记必填，都先从动态 schema 和最终 payload 中剔除。
交付表对应列固定为空，上传、新建和返修都不得携带该字段。

## 账号领域锁定（L1）

- 每个 SOLO2 账号只允许一个大类，子方向不锁。`submission.lockedDomain` 是唯一账号大类来源，
  新建任务、提示词预检、提交和返修都必须先读取它；表单显示全部领域不代表当前账号可以跨大类。
- `submission.lockedDomain` 一经配置即为账号级只读值。禁止为了本次题目执行
  `config set submission.lockedDomain=...`、直接修改 `monitor/state.json`、重写交付表标签或
  新建另一条记录来绕过。缺失、非法或需要调整时，停止并联系管理员。
- 选题和归类时先检查真实工程，再从配置大类的合法子方向中选择最匹配项。能真实匹配时，
  payload 的 `domain` 必须等于配置大类，`sub_direction` 填写匹配的子方向。
  如果任何同大类子方向都无法真实描述该工程，拒绝提交，不要硬套分类。
- 预检会读取账号已有提交，按最早记录交叉核验配置大类。历史与配置冲突、配置缺失，
  或当前题目跨出配置大类时，都在上传前阻断；历史记录只能佐证，不能授权跨域。
- 平台首条记录会锁住账号大类。提交后不能再换到其他大类；返修也只能保留原大类，
  需要调整账号必须由管理员处理。

## 自动提交开关

默认关闭：

```bash
python3 scripts/solo2611.py config set submission.autoSubmit=false
```

关闭时，`export` 只生成本地交付物；`submit` 默认执行 preflight 和 dry-run，人工检查
`monitor/submission/preflight.json`、`payload-preview.json` 后再执行：

```bash
python3 scripts/solo2611.py submit --task-root ROOT --execute
```

打开后，`export` 成功会立即上传、提交并轮询质检：

```bash
python3 scripts/solo2611.py config set submission.autoSubmit=true
```

轮询间隔和上限：

```bash
python3 scripts/solo2611.py config set \
  submission.pollIntervalSeconds=300 \
  submission.pollTimeoutSeconds=86400
```

## 质检状态

- `QC_PASSED`：成功结束，保存完整详情到 `monitor/submission/api-result.json`。
- `PENDING_FIX`：保存 `monitor/submission/fix-request.json`，不得直接重提新记录。
- `QC_REJECTED` / `QC_DISCARDED` / `QC_FAILED`：终态失败，停止。
- `SUBMITTED` / `QC_WAITING` / `QC_RUNNING`：按配置间隔继续查询。

## 返修

1. 读取 `fix-request.json` 的 `qc_summary`、`lockedFields` 和完整详情。
2. 只修改允许修改的字段；`env_snapshot`、Harness、操作系统、复现等级、SessionID、TurnID
   等锁定字段保持服务端原值。
3. 如果改的是评分或 Feedback，先更新 `delivery/feedback.md`，运行 `feedback --check`，
   再运行 `export --no-submit` 更新交付表。
4. 如果平台指出录屏问题，重新录制并通过 `record` 与 `status` 门禁。
5. 先生成返修预览：

```bash
python3 scripts/solo2611.py repair --task-root ROOT \
  --submission-id ID --comment "按质检要求补充反馈证据"
```

6. 确认后提交并继续轮询：

```bash
python3 scripts/solo2611.py repair --task-root ROOT \
  --submission-id ID --comment "按质检要求补充反馈证据" \
  --execute --watch
```

视频本身需要更新时加 `--refresh-video`。返修只能调用原提交 ID 的 `PUT`，不能创建新记录，
也不能改写锁定字段。服务端原记录即使带有 `other_issues`，返修 payload 也必须省略该字段。

## 本地结果

```text
monitor/submission/preflight.json
monitor/submission/payload-preview.json
monitor/submission/api-result.json
monitor/submission/qc-history.json
monitor/submission/fix-request.json
monitor/submission/repair-payload.json
```

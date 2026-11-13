# codex-skill-solo-2611

`solo-2611` v0.6.0：CC/Codex 26.11 三领域单轮 pointwise 本地交付技能。

支持安全、游戏图形与多媒体、数据工程三套最新分类，默认领域为「数据工程 / 数仓与数据湖」。一题使用一个新容器、一个 session；结构中断时只在原会话发送字面“继续”，最多 2 次。技能负责题面门禁、快照、受管会话、轨迹、真实运行验证、窗口录屏、证据化 Feedback 和官方 19 列交付表。

## 安装

```bash
git clone https://github.com/kekelele996/codex-skill-solo-2611.git
cd codex-skill-solo-2611

mkdir -p "$HOME/.codex/skills/solo-2611" "$HOME/.codex/skills/solo-annotation-loop"
rsync -a ./ "$HOME/.codex/skills/solo-2611/"
rsync -a ./dependencies/solo-annotation-loop/ "$HOME/.codex/skills/solo-annotation-loop/"
```

Python 运行时需要：

系统还需要 `tmux >= 3.0`；受管交互是 v0.6.0 的默认运行模式。

```bash
python3 -m pip install -r requirements.txt
python3 "$HOME/.codex/skills/solo-2611/scripts/solo2611.py" version
```

## 配置

```bash
S="$HOME/.codex/skills/solo-2611/scripts/solo2611.py"
python3 "$S" config init
python3 "$S" config set claude.baseUrl="<网关地址>"
python3 "$S" config set claude.apiKey="<本期团队Key>"
python3 "$S" config set submission.lockedDomain="<账号唯一所属领域>"
python3 "$S" config set submission.autoSubmit=true
python3 "$S" config test
```

推荐通过 `SOLO2611_API_KEY`、`SOLO2611_BASE_URL` 和 `GITHUB_TOKEN` 提供模型与快照仓库秘密。SOLO2 的提交地址、账号和认证会话只能从本地工作台获取；技能不直接读取系统 Keychain、认证环境变量或缓存 Cookie。

自动提交已按要求开启：`export` 通过后直接上传、新建提交并轮询质检，不再做本地二次确认。只有 `QC_PASSED` 才算通过。

## 最新分类

| 所属领域 | 子方向 |
|---|---|
| 安全 | 漏洞检测与攻防、身份认证、网络与通信安全、系统安全、其他 |
| 游戏、图形与多媒体 | 音视频处理、图形渲染、游戏开发、图像处理 |
| 数据工程 | 数据采集、爬虫与 RPA；数仓与数据湖；数据分析与数据应用 |

`submission.lockedDomain` 是 Solo2 账号级只读大类。先读真实工程，再从该大类的合法子方向中选择实际匹配项；如果工程无法匹配该大类，应换题，不能跨域提交。

## 核心要求

- 备注默认留空，SOLO2 payload 不携带 `other_issues`。发生继续时备注写固定中断说明；
  最终主会话轨迹命中明文 API Key 风险时，再接中文分号追加
  `轨迹中有存在明文apikey暴露风险`，不记录 Key 原文。
- Feedback 必须基于真实轨迹、产物快照和 `verify` 运行记录；机器会做事实回查。
- Feedback 还会按平台 V2 的轨迹能力上限审计；轨迹未运行完整业务链路时，不能写成已经验证或跑通。
- 重要问题按“触发节点 → 实际行为 → 业务后果 → 证据锚点”写，允许保留 1–2 个最有用的文件名、函数、报错或需求锚点。
- 困难题的难度论证必须使用 P4 强制合同：`schemaVersion: 2`、`difficultyContract.platform-p4-v2`、
  至少三个真实运行边界、两个独立责任边界、两个源码文件和两类原句风险。
- 默认交付完整性上限为 4；打 5 必须通过严格满分审计。
- 4 分必须清楚写出残余缺口，并对已经确认完成的能力与扣分点分别说明。
- 页面可达的页面题必须录真实页面操作；真实启动失败且页面不可达时，录制完整失败过程。
- 完成、暂停、阻断或结束本轮时，最终回复必须完整保留固定六段交付格式。

## 配套项目

- 工作台：<https://github.com/kekelele996/solo-2611>
- 工程台：<https://github.com/kekelele996/project-hub>
- 上级 GSB 参考：<https://github.com/kekelele996/codex-skill-sologsb-0917>

官方规范和上级参考资料已包含在 `official-specs/`、`upstream/` 与 `dependencies/`。

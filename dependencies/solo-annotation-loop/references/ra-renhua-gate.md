# ra-人话 写作与审核硬门槛

提示词、五维描述、其他问题和不满意说明属于用户可见文案，写完必须过 `ra-人话`。作用范围和原来的写作审核一致，不扩展到代码核验、轨迹定位或评分事实判断。

## 固定版本与授权

上游仓库是 `https://github.com/Pluviobyte/rnskill`。技能内固定使用：

- `vendor/rnskill/ra-人话/SKILL.md`

实际版本记录在 `vendor/rnskill/SOURCE.json`，当前 revision 为 `7cbf47b57cbae0596d46c10c4a1445cbb27904b3`，上游版本为 `1.2.0`。审核 JSON 必须写同一个 revision。

该技能采用 CC BY-NC 4.0，作者是雪踏乌云，署名与许可证保存在 `vendor/rnskill/CREDITS.md` 和 `vendor/rnskill/LICENSE`。使用前按项目所在组织的要求确认是否符合非商业许可。

每次审核都要实际读取 `vendor/rnskill/ra-人话/SKILL.md`，不能只写一个通过 JSON 走形式。脚本负责核验文本、硬禁表达和记录，人机判断仍以该技能为准。

## 写作口径

先写清事实、判断、证据和动作，再按 `ra-人话` 的改写流程处理。保留数据、产品名、模型名、日期、技术名词、作者立场和不确定性。不要为了像真人而补经历、补例子、补数字或把结论改软。

提示词仍须满足原有限制，不超过 240 字，不写路径、类名、函数名、代码块和直接修复步骤；同时不写任何端口配置（端口号、`监听`、`localhost`、`PORT=` 等），不写约束技术栈的描述（语言、框架、中间件、数据库、依赖、版本，以及「技术栈」「技术选型」这类约束口径），不指定 Docker 或容器方案，也不把「用户」「使用者」当第三人称主语，改写成使用者视角的直接需求。Docker 只用于本技能的隔离运行 Harness，不是任务要求。评分描述仍须满足每项至少 60 字、无括号引号反引号和阿拉伯数字等现有规则。

终稿重点清掉这些硬伤：

- 二元对比壳：不是甲而是乙、并非甲而是乙、不在于甲而在于乙、不只是甲更是乙、不仅甲还乙、与其甲不如乙。
- 命令模板开头：别急着甲先乙、先别甲先乙、顺序别反了、别搞反了、记住这句话。
- 伪洞察标记：真正、其实、本质上、核心在于、关键在于、说白了、归根结底、更重要的是、这说明、这背后。
- 冒号讲义腔：我的结论是冒号、原因很简单冒号、重点是冒号、分成三类冒号。
- 模糊指代和空泛比较：东西、这件事、这些、一类、几个方向；没有具体比较对象的更适合、更像、更自然、更高级。
- 抽象压力和口号收尾：差距会突然变得很难看、差距会被迅速拉开、成为新的分水岭、更值得关注的是某类对象。
- 生硬比喻和伪造口语：临时绕路、没有绕路，以及为了避开模板硬写出的别扭说法。
- 与完成状态不符的时态，以及没有事实支撑的比喻和总结句。

## 提示词硬时序

给用户的每轮提示词都必须完整走一次“说人话”：

1. 根据项目事实写出技术初稿。
2. 实际读取 `vendor/rnskill/ra-人话/SKILL.md`，按其 Rewrite Workflow 改写正文。
3. 扫描硬禁表达，生成 `checks/human-writing/rNN-prompt.json`。
4. 运行 `validate-prompt.py <提示词文件> --review <审核JSON>`，必须通过。
5. 结合本地历史和 SOLO2 同项目历史完成语义审核，写入 `checks/semantic-dedup/rNN.json`。
6. 运行 `platform_bridge.py check-prompt`，同时做字符级去重并校验语义审核。
7. 以上全部通过后，才允许把提示词展示给用户或写入待执行状态。

缺少审核 JSON、审核哈希不匹配、硬禁表达未清、或 `validate-prompt.py` 不通过时，都不能给用户提示词，也不能进入去重。只运行扫描或手写通过记录不算过一次“说人话”。

## 审核流程

1. 写出初稿。
2. 按 `ra-人话` 的 Rewrite Workflow 提取事实、判断、经历和动作，删掉空框架，再改写成短段落。技术评审文案不强制第一人称。
3. 按 `ra-人话` 的 Before Returning 清单逐项扫描。
4. 运行 `python3 scripts/audit-human-writing.py --text-file <提示词文件> --target-type prompt` 或运行 `python3 scripts/audit-human-writing.py --score-json <round-score.json>` 做机器扫描。
5. 生成审核 JSON，记录 `targetType`、`textSha256`、`upstreamRevision`、`hardBans`、`unresolvedFindings`、`rewriteRounds`、`reviewedBy` 和 `passed`。模板见 `templates/human-writing-review.json`。
6. 把提示词审核写到 `PROJECT_DIR/checks/human-writing/rNN-prompt.json`，把五维评语审核写到 `PROJECT_DIR/checks/human-writing/rNN-comments.json`；五维评语还要把逐句证据写到 `PROJECT_DIR/checks/comment-evidence/rNN.json`。
7. 用 `validate-prompt.py --review ...` 或 `validate-comments.py ... --review ...` 校验。机器扫描、评语证据或审核记录任一不过，返回第 2 步重写，最多重写三轮。
8. 三轮后仍不过时停止，不得写入交付表或提交平台，向用户报告具体未解决问题。

## 通过线

只有同时满足以下条件才算通过：

- `passed` 为 `true`。
- `hardBans` 和 `unresolvedFindings` 都为空。
- `rewriteRounds` 在 0 到 3 之间。
- `reviewedBy` 包含 `ra-人话`。
- `textSha256` 与当前提示词或五维评语逐字一致。
- 审核 JSON 的 `upstreamRevision` 与 `vendor/rnskill/SOURCE.json` 一致。

提交前运行 `scripts/audit-delivery.py` 时，它会把交付表里的提示词和五维描述重新做一次硬禁扫描，并核对对应的审核 JSON。审核不通过先改文案，改完重新审核；不能只改交付表绕过。

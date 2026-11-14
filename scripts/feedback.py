"""Feedback 文本的证据、自然度和一致性门禁。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from common import Solo2611Error, non_space_len, read_json, sha256_bytes, utc_now, write_json
import evidence
from spec import ANCHOR_PATTERN, ARTIFACT_HINT, NEGATIVE_SIGNALS, PROCESS_HINT, RUBRIC_PHRASES, VAGUE_PHRASES

SOFT_MAX_FEEDBACK_CHARS = 280
HARD_MAX_FEEDBACK_CHARS = 360
MAX_RECOMMENDED_ANCHORS = 3
MAX_ANCHORS = 6
# 代码信息和数值必要时可以写；重要问题通常保留一两处，超过才提示改成业务说法。
MAX_QUIET_CODE_TOKENS = 2
MAX_QUIET_NUMBERS = 2
FULL_RUN_ID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
FULL_SCORE_POLICY = "strict-full-score-v1"
FULL_SCORE_CHECK_KEYS = (
    "allExplicitRequirementsVerified",
    "edgeCasesVerified",
    "endToEndRunsVerified",
    "crossLayerReadbackVerified",
    "regressionVerified",
    "noKnownGaps",
    "deliveryEvidenceComplete",
)
FULL_SCORE_COVERAGE = {"success", "negative", "regression", "cross-layer"}
MIN_FULL_SCORE_REQUIREMENTS = 3
MIN_FULL_SCORE_EDGE_CASES = 2
MIN_FULL_SCORE_RUNS = 2
NUMBER_TOKEN = re.compile(r"(?<!\w)\d+(?:[./:-]\d+)*(?:%|ms|s|MB|GB)?(?!\w)")
GENERIC_TEST_COUNT = re.compile(r"(?<!\w)\d+\s*/\s*\d+\s*(?:通过|passed)", re.IGNORECASE)
CERTAIN_COMPLETION = re.compile(
    r"(?:能够|可以|完成了|已实现|实现了|做到了|支持|"
    r"可(?=(?:成功|清楚|统一|正确|稳定|正常|完整|自动|展示|切换|保留|读取|识别|处理))|"
    r"(?<![可性功])能(?=(?:够|成功|清楚|统一|正确|稳定|正常|完整|自动|展示|切换|保留|读取|识别|处理|完成|保证|避免)))"
)
# 不出现代码和数字时，靠真实操作后的可见结果证明结论经过验证。
BUSINESS_EVIDENCE = re.compile(
    r"(重跑|重算|重新运行|再次运行|回读|刷新后|核对|报告里|报告中|页面上|页面里|列表里|接口返回|查询结果|"
    r"[^。；]{1,20}后[，,]?[^。；]{0,12}(?:仍|依然|保持|变成|显示|报错|失败|推进不了|没有))"
)
BEHAVIOR_HINT = re.compile(
    r"(修改|改成|只改|漏改|跳过|忽略|覆盖|写入|读取|查看|调用|执行|运行|删除|移除|返回|"
    r"处理|扫描|判定|报告完成|没有运行|未运行|没有读取|未读取|没有处理|未处理|没有实现|未实现)"
)
RESIDUAL_GAP = re.compile(
    r"(问题(?:是|仍|在)|仍需|尚未|没有整理|未覆盖|缺少|只(?:能|显示|处理)|"
    r"受限于|不足|缺口|待补|需要人工|人工复跑|后续仍需)"
)
# 4 分的残余缺口必须落在可定位的业务页面、功能点或验证环节上。
# 「完整流程缺少回归」这类整段式描述不算具体位置。
RESIDUAL_LOCATION = re.compile(
    r"(?:页面|接口|数据库|查询|保存|解析|草稿|发布|切换|回滚|血缘|影响分析|"
    r"错误输入|失败保存|丢弃|导入|导出|迁移|重跑|恢复|报告|列表|详情|"
    r"按钮|表单|字段|校验|回归用例|验证环节)",
    re.IGNORECASE,
)
CODE_TOKEN = re.compile(
    r"(`[^`]+`|[A-Za-z0-9_./-]+\.(?:ts|tsx|js|jsx|mjs|cjs|py|rs|go|java|cs|json|html|css|vue|sql|toml|yaml|"
    r"yml|csv|tsv|parquet|ipynb|c|cc|cpp|h|hpp)(?![A-Za-z0-9_])|(?<![A-Za-z0-9_])[A-Za-z_][A-Za-z0-9_]*\(\)|"
    r"(?<![A-Za-z0-9_])[a-z]+_[a-z0-9_]+(?![A-Za-z0-9_]))"
)
# 读者不熟悉的行话和生造词，换成直白说法；括号里是建议替换。
JARGON_WORDS = {
    "题面": "需求、业务要求",
    "宣称": "报告完成",
    "口径": "计算方式、特征定义、统计规则",
    "事后": "之后、后来",
    "对平": "结果一致、能对上",
    "落地": "实现、做到",
    "闭环": "写清具体做了什么",
    "兜底": "失败时怎么处理",
    "链路": "流程、这一步",
    "抓手": "写清具体做法",
}
CONCISE_EVIDENCE = re.compile(
    r"(?:\d+\s*/\s*\d+\s*(?:通过|passed)|"
    r"(?:前端|后端|接口|数据库|页面).{0,12}(?:检查|复核)(?:也|已)?通过)"
)
WORKFLOW_LEAK_PATTERN = re.compile(
    r"(?:模型|助手|Claude|Codex|容器|Docker|镜像|临时目录|临时依赖|依赖安装|"
    r"本地验证脚本|本地测试脚本|本地脚本|验收脚本|自动化脚本|测试脚本|"
    r"Playwright|Puppeteer|录制|录屏|视频|截图|画面|镜头|浏览器|终端|鼠标|光标|分辨率|"
    r"宿主|运行机器|验收机|验收宿主|提交|质检|返修|评审|评分|交付完整性|提示词|工作流|"
    r"Session\s*ID|Prompt\s*ID|Turn\s*ID|轨迹)",
    re.I,
)
USER_TERM_PATTERN = re.compile(r"用户")

TEMPLATE = """\
<!--
Feedback 写成短复盘，不抄录验收报告：默认 180–280 个非空字，句子要通顺，读一遍就能懂。
动笔前先跑 `verify --label '验证了什么' -- <真实命令>`，把真实输出固定下来；没有执行记录会被阻断。
正文里的数字、文件、反引号标识符和报错原文都会被拿去和轨迹、产物快照、运行输出逐条比对，查不到就阻断。
差值这类由两次真实输出算出来的数字同样认可，不要为了凑原文改数字。
重要问题可以保留 1–2 个最必要的代码或运行锚点：文件名、函数名、步骤、报错原文或未实现的需求点。
固定按“触发节点 → 实际行为 → 业务后果 → 证据锚点”写清；其余代码和数值能换成业务说法就换。
过程段写实现时的决定和验证行为，不写复核步骤；产物段写成功路径、失败路径和页面或接口本该呈现什么。
已经由代码和运行结果确认的能力要写“能够、可以、能、完成了、已实现”等确定表达。
测试只写与题目需求直接相关的场景和结论，不写“x/x 条测试通过”，也不拿无关的前端类型检查充数。
4 分的残余缺口必须点到具体页面、功能点或验证环节，不能只写“整体流程缺少回归”。
常规高质量完成的最高分是 4；考虑给 5 时必须先填写并通过 monitor/score-review.json 的严格满分审计。
读取、写入、忽略等计数要解释业务含义。完整正例见 solo-2611 技能的 references/feedback-guide.md。
先读原始轨迹、产物 diff 和录屏；Agent 起草后由标注专家复核事实、分数和措辞。
-->

## 交付完整性

（填 1–5 的整数）

## Feedback

过程问题：

产物问题：
"""

_SECTION = re.compile(r"^##\s*(交付完整性|Feedback|备注)\s*$", re.M)
_PLACEHOLDER = re.compile(r"（填 1–5 的整数）|（可空[^）]*）")
CONSISTENCY_REVIEW_REL = Path("monitor") / "submission" / "feedback-consistency-review.json"


def parse(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise Solo2611Error(f"缺少 {path}，先运行 `feedback --init` 生成模板，完成证据化起草和专家复核")
    text = re.sub(r"<!--.*?-->", "", path.read_text(encoding="utf-8"), flags=re.S)
    parts: dict[str, str] = {}
    matches = list(_SECTION.finditer(text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        parts[m.group(1)] = _PLACEHOLDER.sub("", text[m.end():end]).strip()
    return parts


def full_score_review_path(task_root: Path) -> Path:
    return task_root / "monitor" / "score-review.json"


def full_score_review_template() -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "policy": FULL_SCORE_POLICY,
        "candidateScore": 5,
        "checklist": {key: False for key in FULL_SCORE_CHECK_KEYS},
        "explicitRequirements": [],
        "edgeCases": [],
        "runtimeEvidence": [],
        "knownGaps": [],
        "maxScoreJustification": "",
        "reviewedBy": "",
        "reviewedAt": "",
    }


def ensure_full_score_review_template(task_root: Path) -> Path:
    path = full_score_review_path(task_root)
    if not path.exists():
        write_json(path, full_score_review_template())
    return path


def consistency_review_path(task_root: Path) -> Path:
    return task_root / CONSISTENCY_REVIEW_REL


def _consistency_fingerprint(score: int, feedback: str, alignment: dict[str, Any]) -> str:
    opinion = alignment.get("traceOpinion") if isinstance(alignment.get("traceOpinion"), dict) else {}
    payload = "\n".join(
        [
            str(score),
            re.sub(r"\s+", "", feedback),
            str(opinion.get("status") or ""),
            str(opinion.get("finalAssistantEvidence") or ""),
            *[str(item) for item in opinion.get("conflicts") or []],
        ]
    )
    return sha256_bytes(payload.encode("utf-8"))


def _review_consistency(
    task_root: Path,
    *,
    score: int,
    feedback: str,
    alignment: dict[str, Any],
) -> dict[str, Any]:
    """Require a local review when a high score contradicts a negative trace verdict."""
    opinion = alignment.get("traceOpinion") if isinstance(alignment.get("traceOpinion"), dict) else {}
    conflicts = [str(item) for item in opinion.get("conflicts") or [] if str(item).strip()]
    if not conflicts:
        return {"required": False, "status": "not_required", "path": str(consistency_review_path(task_root))}

    fingerprint = _consistency_fingerprint(score, feedback, alignment)
    path = consistency_review_path(task_root)
    try:
        record = read_json(path, {}) or {}
    except Exception:  # noqa: BLE001 - 损坏的核验记录不能当作已批准
        record = {}
    same = record.get("fingerprint") == fingerprint
    if not same or record.get("status") not in {"pending", "approved", "rejected"}:
        record = {
            "schemaVersion": 1,
            "kind": "feedback-trace-consistency",
            "status": "pending",
            "fingerprint": fingerprint,
            "score": score,
            "feedback": feedback,
            "conflicts": conflicts,
            "traceOpinion": opinion,
            "createdAt": utc_now(),
        }
        write_json(path, record, mode=0o600)
    return {
        "required": True,
        "status": str(record.get("status") or "pending"),
        "path": str(path),
        "fingerprint": fingerprint,
        "conflicts": conflicts,
        "reviewer": str(record.get("reviewer") or ""),
        "note": str(record.get("note") or ""),
    }


def review_consistency(
    task_root: Path,
    *,
    decision: str = "",
    reviewer: str = "",
    note: str = "",
) -> dict[str, Any]:
    """View or update the feedback-versus-trace local review record."""
    path = consistency_review_path(task_root)
    try:
        record = read_json(path, {}) or {}
    except Exception:  # noqa: BLE001
        record = {}
    if not record:
        return {"ok": True, "status": "not_required", "review": str(path)}
    if decision:
        if decision not in {"approved", "rejected"}:
            raise Solo2611Error(f"未知的核验结论: {decision}")
        if not reviewer.strip():
            raise Solo2611Error("登记本地核验结论必须提供 --reviewer")
        record.update(
            {
                "status": decision,
                "reviewer": reviewer.strip(),
                "note": note.strip(),
                "reviewedAt": utc_now(),
            }
        )
        write_json(path, record, mode=0o600)
    return {"ok": record.get("status") == "approved", "review": str(path), **record}


def _verified_items(data: dict[str, Any], key: str, label: str, minimum: int) -> list[str]:
    errors: list[str] = []
    items = data.get(key)
    if not isinstance(items, list) or len(items) < minimum:
        return [f"满分审计 {key} 至少需要 {minimum} 条{label}"]
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            errors.append(f"满分审计 {key}[{index}] 必须是对象")
            continue
        if item.get("verified") is not True:
            errors.append(f"满分审计 {key}[{index}] 尚未标记 verified=true")
        if non_space_len(str(item.get("evidence") or "")) < 12:
            errors.append(f"满分审计 {key}[{index}] 缺少至少 12 字的真实证据说明")
    return errors


def validate_full_score_review(task_root: Path, score: int) -> tuple[list[str], dict[str, Any]]:
    """5 分必须通过独立的严格满分审计；普通评分不受此文件约束。"""
    if score != 5:
        return [], {"required": False, "status": "not_applicable"}
    path = full_score_review_path(task_root)
    if not path.is_file():
        return [
            "5 分需要通过严格满分审计：缺少 monitor/score-review.json；"
            "常规高质量交付应评 4 分"
        ], {"required": True, "status": "missing", "path": str(path)}

    data = read_json(path, {}) or {}
    errors: list[str] = []
    if data.get("schemaVersion") != 1:
        errors.append("满分审计 schemaVersion 必须是 1")
    if data.get("policy") != FULL_SCORE_POLICY:
        errors.append(f"满分审计 policy 必须是 {FULL_SCORE_POLICY}")
    if data.get("candidateScore") != 5:
        errors.append("满分审计 candidateScore 必须是 5")

    checklist = data.get("checklist") if isinstance(data.get("checklist"), dict) else {}
    failed = [key for key in FULL_SCORE_CHECK_KEYS if checklist.get(key) is not True]
    if failed:
        errors.append("满分审计未通过的检查项: " + "、".join(failed))

    errors.extend(
        _verified_items(data, "explicitRequirements", "明示需求", MIN_FULL_SCORE_REQUIREMENTS)
    )
    errors.extend(
        _verified_items(data, "edgeCases", "关键边界场景", MIN_FULL_SCORE_EDGE_CASES)
    )

    actual_runs = {
        int(item["index"]): item
        for item in evidence.runs(task_root)
        if str(item.get("index", "")).isdigit()
    }
    runtime_entries = data.get("runtimeEvidence")
    runtime_entries = runtime_entries if isinstance(runtime_entries, list) else []
    used_runs: set[int] = set()
    coverage: set[str] = set()
    success_runs: set[int] = set()
    for index, item in enumerate(runtime_entries, start=1):
        if not isinstance(item, dict):
            errors.append(f"满分审计 runtimeEvidence[{index}] 必须是对象")
            continue
        try:
            run_index = int(item.get("runIndex"))
        except (TypeError, ValueError):
            errors.append(f"满分审计 runtimeEvidence[{index}] 缺少有效 runIndex")
            continue
        if run_index not in actual_runs:
            errors.append(
                f"满分审计 runtimeEvidence[{index}] 引用了不存在的执行记录 {run_index}"
            )
            continue
        used_runs.add(run_index)
        tags = item.get("covers")
        if not isinstance(tags, list) or not tags:
            errors.append(f"满分审计 runtimeEvidence[{index}] 缺少 covers 类型")
            continue
        unknown = sorted({str(tag) for tag in tags} - FULL_SCORE_COVERAGE)
        if unknown:
            errors.append(
                f"满分审计 runtimeEvidence[{index}] 含未知 covers: " + "、".join(unknown)
            )
        normalized_tags = {str(tag) for tag in tags}
        coverage.update(normalized_tags)
        if "success" in normalized_tags:
            success_runs.add(run_index)
        if non_space_len(str(item.get("observation") or "")) < 8:
            errors.append(f"满分审计 runtimeEvidence[{index}] 缺少观察结果")

    if len(used_runs) < MIN_FULL_SCORE_RUNS:
        errors.append(
            f"满分审计至少需要 {MIN_FULL_SCORE_RUNS} 条不同的真实执行记录，当前 {len(used_runs)} 条"
        )
    required_coverage = {"success", "regression"}
    missing_coverage = sorted(required_coverage - coverage)
    if missing_coverage:
        errors.append("满分审计缺少运行覆盖: " + "、".join(missing_coverage))
    if not ({"negative", "cross-layer"} & coverage):
        errors.append("满分审计至少还要覆盖 negative 或 cross-layer 中的一类")
    if not any(actual_runs[index].get("exitCode") == 0 for index in success_runs):
        errors.append("满分审计的 success 覆盖没有绑定到成功退出的真实执行记录")

    known_gaps = data.get("knownGaps")
    if not isinstance(known_gaps, list) or known_gaps:
        errors.append("存在已知缺口时不能评 5 分，knownGaps 必须为空数组")
    if non_space_len(str(data.get("maxScoreJustification") or "")) < 40:
        errors.append("满分审计需要至少 40 字说明为什么必须评 5 而不能评 4")
    if not str(data.get("reviewedBy") or "").strip():
        errors.append("满分审计缺少 reviewedBy")

    summary = {
        "required": True,
        "status": "passed" if not errors else "failed",
        "path": str(path),
        "runtimeRuns": sorted(used_runs),
        "coverage": sorted(coverage),
        "requirementCount": len(data.get("explicitRequirements") or []),
        "edgeCaseCount": len(data.get("edgeCases") or []),
    }
    return errors, summary


def check(path: Path, *, task_root: Path | None = None, state: dict[str, Any] | None = None) -> dict[str, Any]:
    """格式与文风门禁；给了 task_root 时再加一层事实核对，反馈里的数字、文件、报错必须回查得到。"""
    parts = parse(path)
    errors: list[str] = []
    warnings: list[str] = []
    score_raw = parts.get("交付完整性", "")
    score = int(score_raw) if re.fullmatch(r"[1-5]", score_raw) else 0
    if not score:
        errors.append(f"交付完整性必须是 1–5 的整数，当前为 {score_raw!r}")
    feedback = parts.get("Feedback", "")
    body = re.sub(r"(过程问题|产物问题)[:：]", "", feedback).strip()
    body_chars = non_space_len(body)
    if body_chars < 80:
        errors.append(f"Feedback 有效内容只有 {body_chars} 字，过程、产物和结果还没有写完整")
    if body_chars > HARD_MAX_FEEDBACK_CHARS:
        errors.append(
            f"Feedback 有 {body_chars} 个非空字，超过 {HARD_MAX_FEEDBACK_CHARS} 字上限；"
            "只保留决定分数的关键过程和产物结论"
        )
    elif body_chars > SOFT_MAX_FEEDBACK_CHARS:
        warnings.append(
            f"Feedback 有 {body_chars} 个非空字，建议压到 {SOFT_MAX_FEEDBACK_CHARS} 字内"
        )
    if FULL_RUN_ID.search(feedback):
        errors.append("Feedback 含完整运行 ID，应删掉或用运行名称代替")
    workflow_terms = sorted({match.group(0) for match in WORKFLOW_LEAK_PATTERN.finditer(feedback)})
    if workflow_terms:
        errors.append(
            f"Feedback 含运行流程信息 {workflow_terms}；只写产品实现、需求行为和页面或接口可见结果"
        )
    if USER_TERM_PATTERN.search(feedback):
        errors.append("Feedback 正文不得出现“用户”；直接写业务对象、页面、任务、样本或可观察结果")
    remark = parts.get("备注", "").strip()
    if remark:
        errors.append("备注必须留空；不要在 Feedback 文件中填写备注项")
    if GENERIC_TEST_COUNT.search(feedback):
        warnings.append("Feedback 用了总套件通过数；改写为与题目需求直接相关的测试场景、测试文件和业务不变量")
    anchors = ANCHOR_PATTERN.findall(feedback)
    if len(anchors) > MAX_ANCHORS:
        errors.append(f"Feedback 引用了 {len(anchors)} 个代码/命令锚点，最多保留 {MAX_ANCHORS} 个")
    elif len(anchors) > MAX_RECOMMENDED_ANCHORS:
        warnings.append(
            f"Feedback 引用了 {len(anchors)} 个代码/命令锚点，建议只留 1–2 个最关键证据"
        )
    code_tokens = sorted(set(CODE_TOKEN.findall(feedback)))
    if len(code_tokens) > MAX_QUIET_CODE_TOKENS:
        warnings.append(
            f"Feedback 出现 {len(code_tokens)} 处代码、文件名或字段名 {code_tokens[:5]}；"
            "能换成业务行为的换掉，只留定位问题必需的一两处"
        )
    number_tokens = NUMBER_TOKEN.findall(feedback)
    if len(number_tokens) > MAX_QUIET_NUMBERS:
        warnings.append(
            f"Feedback 含 {len(number_tokens)} 个具体数值 {number_tokens[:5]}；能写成前后变化或定性结论的换掉，"
            "只留本身就是缺陷的关键数字"
        )
    jargon = [word for word in JARGON_WORDS if word in feedback]
    if jargon:
        hints = "；".join(f"{word} → {JARGON_WORDS[word]}" for word in jargon)
        warnings.append(f"Feedback 用了读者不熟悉的行话 {jargon}，换成直白说法：{hints}")
    for phrase in RUBRIC_PHRASES:
        if phrase in feedback:
            errors.append(f"Feedback 照抄了评分表描述「{phrase}」，规范明确禁止")
    vague = [p for p in VAGUE_PHRASES if p in feedback]
    if vague:
        errors.append(f"出现无法核验的笼统表述 {vague}，需要换成具体文件/报错/需求点")
    if not (
        ANCHOR_PATTERN.search(feedback)
        or CONCISE_EVIDENCE.search(feedback)
        or BUSINESS_EVIDENCE.search(feedback)
    ):
        errors.append("没有找到可核验的证据（真实操作后的结果、重跑或回读结论、页面或报告表现等）")
    if not PROCESS_HINT.search(feedback):
        errors.append("看不出过程分析（第几步做了什么、是否验证），规范要求过程与产物都要分析")
    if not ARTIFACT_HINT.search(feedback):
        errors.append("看不出产物分析（运行结果、接口、文件或功能），规范要求过程与产物都要分析")
    negatives = sorted(set(NEGATIVE_SIGNALS.findall(feedback)))
    if score == 5 and negatives:
        errors.append(f"打了 5 分但文本里有失败/缺失表现 {negatives}，打分与描述不一致")
    elif score == 5 and RESIDUAL_GAP.search(feedback):
        errors.append("打了 5 分但文本仍描述了残余缺口或覆盖限制")
    elif score == 4 and any(
        word in feedback
        for word in ("无法运行", "跑不起来", "编译失败", "启动失败", "完全没实现", "一行代码没写")
    ):
        errors.append("打了 4 分但写的是阻断性失败；4 分只能有小瑕疵，需按实际结果重评")
    if score == 4 and not RESIDUAL_GAP.search(feedback):
        errors.append("打了 4 分但没有写明少量但不能忽略的残余缺口或覆盖限制")
    if score == 4:
        residual_clauses = [
            clause for clause in re.split(r"(?<=[。！？；\n])", feedback)
            if RESIDUAL_GAP.search(clause)
        ]
        if not any(
            RESIDUAL_LOCATION.search(clause)
            or CODE_TOKEN.search(clause)
            or ANCHOR_PATTERN.search(clause)
            for clause in residual_clauses
        ):
            errors.append(
                "4 分反馈的残余缺口没有具体位置；必须点名页面、功能点或验证环节，"
                "不能只写“完整流程/整体流程缺少回归”"
            )
    if score and score <= 2 and not negatives:
        errors.append(f"打了 {score} 分但文本里没有写明失败/缺失的具体表现")
    if task_root is not None and state is not None and score == 5:
        run = state.get("sessionRun") or {}
        used = int((run.get("continues") or {}).get("used") or 0)
        model_fault_kinds = {
            str(item.get("kind") or "")
            for item in run.get("interrupts") or []
            if isinstance(item, dict)
        }
        model_fault_kinds |= {
            str(item.get("kind") or "")
            for item in run.get("rounds") or []
            if isinstance(item, dict)
        }
        if used > 0 and model_fault_kinds & {"truncated", "incomplete", "empty"}:
            errors.append("模型自身中断过的题不得评 5 分")

    process_match = re.search(r"过程问题[:：](.*?)(?:\n\s*产物问题[:：]|$)", feedback, re.S)
    process_text = process_match.group(1) if process_match else ""
    trigger = re.search(
        r"(第\s*\d+\s*步|在[^。；]{0,40}(?:时|后)|[^。；]{2,30}(?:时|后)[，,]|执行[^。；]{0,30}|读取[^。；]{0,30}|修改[^。；]{0,30}|"
        r"调用[^。；]{0,30}|没有运行|未运行|没有读取|未读取|直接宣称|就报告完成)",
        process_text,
    )
    consequence = re.search(
        r"(导致|造成|结果|于是|因此|无法|返回|报错|缺少|未实现|没有实现|仍为空|被拒绝|服务退出|页面打不开)",
        feedback,
    )
    if score and score <= 4 and negatives:
        if not trigger:
            errors.append("写了负面问题，但没有交代是在哪个操作或处理环节出的问题")
        if not BEHAVIOR_HINT.search(process_text):
            errors.append("写了负面问题，但没有交代具体做了什么或漏做了什么（实际行为）")
        if not consequence:
            errors.append("写了负面问题，但没有交代客观结果或业务影响")
    if score >= 4 and not re.search(
        r"(运行|启动|构建|接口|回读|打开|点击|导入|同步|执行|生成|输出|查询|批次|记录|核对)",
        feedback,
    ):
        errors.append(f"打了 {score} 分但没有可核验的运行或功能结果")
    if score >= 4 and not re.search(r"(逐条|逐项|核对|检查了|验证了|运行了|回读了)", feedback):
        errors.append(f"打了 {score} 分但没有写明逐条核对或真实验证方式")
    if score >= 4 and not CERTAIN_COMPLETION.search(feedback):
        errors.append(
            f"打了 {score} 分但没有使用确定表达（能够、可以、能、完成了、已实现等）；"
            "已验证的能力不要只写成悬空的功能短语"
        )

    ai_phrases = [
        "真正", "其实", "本质上", "核心在于", "更重要的是", "这说明", "综上",
        "从录屏来看", "根据编写的测试", "测试结果表明", "复核结果显示", "整体不错",
    ]
    hit_ai = [phrase for phrase in ai_phrases if phrase in feedback]
    if hit_ai:
        errors.append(f"含有模板化或转述来源的表达 {hit_ai}，直接写实际行为与结果")
    if re.search(r"(不是[^。]{0,60}而是|并非[^。]{0,60}而是|不仅[^。]{0,80}(?:更|还|而且))", feedback):
        errors.append("出现 ra-人话 二元对比模板句，改写成直接事实")
    long_sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[。！？!?])", feedback)
        if non_space_len(sentence) > 100
    ]
    if long_sentences:
        warnings.append(f"有 {len(long_sentences)} 句超过 100 字，建议按触发、行为和后果拆开")
    if re.search(r"(^|\n)\s*(#|```|\|.*\|)", feedback):
        warnings.append("Feedback 含 Markdown 标题/代码块/表格，粘到表格里可能变形")

    verdict: dict[str, Any] | None = None
    full_score_errors: list[str] = []
    full_score_review: dict[str, Any] = {"required": False, "status": "not_applicable"}
    consistency_review: dict[str, Any] = {
        "required": False,
        "status": "not_required",
        "path": "",
    }
    if task_root is not None:
        full_score_errors, full_score_review = validate_full_score_review(task_root, score)
        errors.extend(full_score_errors)
    if task_root is not None:
        if state is None:
            raise Solo2611Error("事实核对需要任务状态，先 load_state 再传进来")
        verdict = evidence.check(task_root, state, feedback, score)
        errors.extend(verdict["errors"])
        warnings.extend(verdict["warnings"])
        consistency_review = _review_consistency(
            task_root,
            score=score,
            feedback=feedback,
            alignment=verdict.get("traceAlignment") or {},
        )
        if consistency_review.get("status") in {"pending", "rejected"}:
            state_text = "等待本地核验" if consistency_review.get("status") == "pending" else "本地核验未通过"
            errors.append(
                f"Feedback 与轨迹的总体结论出入较大，已{state_text}；"
                f"请先核对原始轨迹和本机运行结果，再执行 local-review --kind feedback"
            )
    result = {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "score": score,
        "feedback": feedback,
        "remark": "",
        "fullScoreReview": full_score_review,
        "localReviewRequired": bool(consistency_review.get("required")),
        "localReviewStatus": str(consistency_review.get("status") or "not_required"),
        "localReview": consistency_review,
    }
    if verdict is not None:
        result["evidence"] = {
            "ok": verdict["ok"],
            "summary": verdict["summary"],
            "claims": verdict["claims"],
            "index": verdict["index"],
            "checkedAt": verdict["checkedAt"],
        }
        result["traceAlignment"] = verdict.get("traceAlignment") or {}
    return result

#!/usr/bin/env python3
"""ra-人话 审核共用逻辑。"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

SKILL_ROOT = Path(__file__).resolve().parent.parent
SOURCE_PATH = SKILL_ROOT / "vendor" / "rnskill" / "SOURCE.json"
POLICY_VERSION = "solo-ra-renhua-v1"
DIM_KEYS = ("delivery", "instruction", "planning", "reasoning", "execution")
DIM_LABELS = {
    "delivery": "交付完整性",
    "instruction": "指令遵循",
    "planning": "任务规划",
    "reasoning": "推理能力",
    "execution": "执行能力",
}
COMMENT_EVIDENCE_SCHEMA_VERSION = 1
SENTENCE_SPLIT_RE = re.compile(r"(?<=[。；;！？!?])")
SCORE_ONLY_RE = re.compile(
    r"^(?:因此|所以)?[^。；;！？!?]{0,12}(?:扣|给|保留|计).{0,4}分[。；;！？!?]?$"
)
ABSENCE_CLAIM_RE = re.compile(
    r"(没有|未进入|未覆盖|不存在|未找到|缺少|未包含|未复跑|未执行|没有执行)"
)
POSITIVE_CLAIM_RE = re.compile(
    r"(通过|成功|完成|修复|覆盖|保持|一致|可用|返回|创建|补建)"
)
NEGATIVE_CLAIM_RE = re.compile(r"(没有|未|缺少|失败|不能|无法|相反|冲突|扣)")
FAIL_EVIDENCE_RE = re.compile(
    r"(?i)(?:\b(?:fail(?:ed|ure)?|error)\b|not found|command not found|失败|错误|不存在|未找到)"
)

# 这些是 ra-人话 明确要求终稿避免的表达。目标词和事实绑定得很紧时仍应改写。
HARD_BAN_PHRASES = (
    "真实用户",
    "实际用户",
    "顺序别反了",
    "别搞反了",
    "记住这句话",
    "真正",
    "其实",
    "本质上",
    "核心在于",
    "关键在于",
    "说白了",
    "归根结底",
    "更重要的是",
    "结果有点出乎意料",
    "这说明",
    "这背后",
    "更值得盯的是个人",
    "更值得关注的是",
    "差距会突然变得很难看",
    "差距会被迅速拉开",
    "会成为新的分水岭",
    "更适合",
    "更像",
    "更自然",
    "更高级",
    "临时绕路",
    "没有绕路",
)

HARD_BAN_PATTERNS = (
    ("二元对比壳", re.compile(r"(?:不是|并非).{0,80}(?:而是|而在于)", re.S)),
    ("二元对比壳", re.compile(r"不在于.{0,80}而在于", re.S)),
    ("二元对比壳", re.compile(r"不只是.{0,80}(?:更|还)", re.S)),
    ("二元对比壳", re.compile(r"不仅.{0,100}(?:更|还|而且)", re.S)),
    ("二元对比壳", re.compile(r"与其.{0,80}不如", re.S)),
    ("命令模板开头", re.compile(r"别急着.{0,50}先", re.S)),
    ("命令模板开头", re.compile(r"先别.{0,50}先", re.S)),
)


# 提示词设计四条硬规则：不写端口配置、不写技术栈、不指定 Docker 或容器方案、不用「用户xxx」第三人称口吻。
PORT_PATTERN = re.compile(
    r"(端口|监听|localhost|127\.0\.0\.1|0\.0\.0\.0|PORT\s*=|(?<![\d:]):\d{2,5}(?!\d))",
    re.I,
)
TECH_STACK_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"react|vue|angular|svelte|next\.?js|nuxt|vite|webpack|babel|tailwind|bootstrap|jquery|"
    r"typescript|javascript|node\.?js|deno|java|kotlin|scala|golang|php|laravel|ruby|rails|"
    r"python|django|flask|fastapi|celery|spring(?:boot|cloud)?|mybatis|hibernate|jpa|"
    r"mysql|postgres(?:ql)?|mariadb|sqlite|mongodb|mongo|redis|elasticsearch|kafka|rabbitmq|"
    r"zookeeper|nginx|minio|jenkins|prometheus|grafana|"
    r"maven|gradle|npm|yarn|pnpm|poetry|cargo|tomcat|jetty|"
    r"restful|graphql|grpc|websocket|jwt|oauth2?|swagger|openapi|sql"
    r")(?![A-Za-z0-9])",
    re.I,
)
CONTAINER_SOLUTION_PATTERN = re.compile(
    r"(?i)(docker(?:\s*compose)?|kubernetes|k8s|容器化|容器方案|容器编排|镜像(?:构建|部署|方案|化)?)"
)
TECH_STACK_CN = ("微服务", "前后端分离", "云原生")
# 只拦「用户可以…」这类把使用者当第三人称主语的说法，不拦「候补用户」「多用户」这类名词。
PERSONA_SUBJECT_PATTERN = re.compile(
    r"(用户|使用者)(?:"
    r"可以|可|能|会|需|需要|要|应|应当|必须|无法|"
    r"以|用|把|被|在|对|从|"
    r"点击|打开|看到|查看|进入|登录|注册|提交|申请|上传|下载|填写|选择|管理|操作|使用|访问|"
    r"发起|确认|取消|退出|收到|获取|编辑|删除|新增|设置|切换|搜索|浏览|"
    r"报名|作答|交卷|签到|递补|投递|写信|回复|收藏|跳过|举报|屏蔽|发布|批改"
    r")"
)
# 即使没有点名具体技术，出现这些词也属于在约束技术栈。
TECH_CONSTRAINT_PATTERN = re.compile(r"(技术栈|技术选型|技术方案|技术路线|技术架构|框架版本|依赖版本|版本要求)")

# 五维评语必须包含可回指真实轨迹与产物的线索。这里做保守的词法检查，
# 具体事实仍需结合轨迹、源码和命令输出人工核对。
COMMENT_OPERATION_PATTERN = re.compile(
    r"(第[一二三四五六七八九十百千万0-9]+步|步骤|工具调用|执行|运行|复跑|浏览器|接口|命令|测试|构建|迁移|启动|提交|调用|读取|写入|定位|查询|创建|修改|删除|重启|编译|静态检查)"
)
COMMENT_TOOL_PATTERN = re.compile(
    r"(go\s+test|go\s+build|go\s+vet|gofmt|pytest|npm|pnpm|yarn|cargo|mvn|gradle|curl|git|docker|python3?|playwright|浏览器|接口)",
    re.I,
)
# “浏览器”“接口”可以作为操作工具，但不能替代第二项要求中的
# 文件名、函数名或可执行命令证据。
COMMENT_COMMAND_PATTERN = re.compile(
    r"(go\s+test|go\s+build|go\s+vet|gofmt|pytest|npm|pnpm|yarn|cargo|mvn|gradle|curl|git|docker|python3?|playwright)",
    re.I,
)
COMMENT_FILE_PATTERN = re.compile(
    r"(?<![\w./-])[\w./-]+\.(?:go|py|js|jsx|ts|tsx|vue|sql|json|md|yaml|yml|sh|html|css|rs|c|cc|cpp|h|hpp|java|kt|rb|php|toml|ini|xml|txt|csv)(?![\w.-])",
    re.I,
)
COMMENT_FUNCTION_PATTERN = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\s*\(")
# 评语常以“调用 createReading 函数”描述轨迹，函数名后不一定带括号。
# 只在明确的函数/方法语境中接受裸函数名，避免把普通英文单词当成证据。
COMMENT_FUNCTION_NAME_PATTERN = re.compile(
    r"(?:函数|方法|调用)\s*[：: ]*\b[A-Za-z_][A-Za-z0-9_]*\b"
)
NON_PERFECT_NEGATIVE_PATTERN = re.compile(
    r"(未|没有|缺少|缺失|遗漏|漏掉|失败|报错|错误|异常|不符合|不足|只覆盖|仅覆盖|"
    r"未覆盖|未验证|未检查|未处理|未回归|无法|不能|问题|缺陷|超时|未通过|返回空|"
    r"行为不符|偏离|误判|重复|冗余|多余|残留|回归失败|崩溃)"
)
NON_PERFECT_CONSEQUENCE_PATTERN = re.compile(
    r"(导致|造成|结果是|表现为|最终|因此|从而|影响|无法|不能|返回|报错|失败|超时|"
    r"未通过|未覆盖|未验证|只覆盖|仅覆盖|残留|回归失败|行为不符|需要补|需补)"
)


def non_perfect_deduction_issues(
    comments: Mapping[str, Any],
    scores: Mapping[str, Any] | None,
) -> list[str]:
    """非满分维度必须明确写出负面判断和客观后果。"""
    if not scores:
        return []
    issues: list[str] = []
    for key in DIM_KEYS:
        try:
            score = float(scores.get(key))
        except (TypeError, ValueError):
            continue
        if score >= 5:
            continue
        text = str(comments.get(key, "")).strip()
        label = DIM_LABELS[key]
        if not NON_PERFECT_NEGATIVE_PATTERN.search(text):
            issues.append(
                f"{label} 得分 {score:g} 但缺少明确负面判断，必须写清扣分点，不能只列通过项"
            )
        if not NON_PERFECT_CONSEQUENCE_PATTERN.search(text):
            issues.append(
                f"{label} 得分 {score:g} 但缺少客观后果，必须写明问题在哪一步发生以及造成什么结果"
            )
    return issues


def comment_evidence_issues(text: str, label: str = "评语") -> list[str]:
    """检查评语是否含有操作/工具与文件/函数/命令引用。"""
    clean = text.strip()
    has_operation = bool(COMMENT_OPERATION_PATTERN.search(clean) or COMMENT_TOOL_PATTERN.search(clean))
    has_artifact = bool(
        COMMENT_FILE_PATTERN.search(clean)
        or COMMENT_FUNCTION_PATTERN.search(clean)
        or COMMENT_FUNCTION_NAME_PATTERN.search(clean)
        or COMMENT_COMMAND_PATTERN.search(clean)
        or re.search(r"(?:命令|接口路径|测试名)[：: ]*[A-Za-z0-9_./-]+", clean)
    )
    issues: list[str] = []
    if not has_operation:
        issues.append(f"{label}缺少具体操作步骤或工具调用，需写明第几步做了什么")
    if not has_artifact:
        issues.append(f"{label}缺少文件名、函数名或命令引用，需提供可回指证据")
    return issues


def prompt_design_issues(clean: str) -> list[dict[str, str]]:
    """提示词专属硬规则扫描：端口配置、技术栈、Docker 或容器方案、第三人称「用户」描述。"""
    issues: list[dict[str, str]] = []
    port = PORT_PATTERN.search(clean)
    if port:
        issues.append({
            "kind": "端口配置",
            "match": port.group(0),
            "message": f"出现端口定义「{port.group(0)}」：提示词不写任何端口配置，也不要求调整端口",
        })
    container = CONTAINER_SOLUTION_PATTERN.search(clean)
    if container:
        issues.append({
            "kind": "指定 Docker 或容器方案",
            "match": container.group(0),
            "message": f"出现容器方案要求「{container.group(0)}」：Docker 只用于隔离运行 Harness，提示词不得要求使用 Docker、容器化、镜像或容器编排完成任务",
        })
    tech = TECH_STACK_PATTERN.search(clean)
    tech_text = tech.group(0) if tech else ""
    if not tech_text:
        tech_text = next((name for name in TECH_STACK_CN if name in clean), "")
    if not tech_text:
        constraint = TECH_CONSTRAINT_PATTERN.search(clean)
        tech_text = constraint.group(0) if constraint else ""
    if tech_text:
        issues.append({
            "kind": "约束技术栈",
            "match": tech_text,
            "message": f"出现约束技术栈的描述「{tech_text}」：提示词不指定语言、框架、中间件、数据库、依赖和版本，只描述要达到的结果",
        })
    persona = PERSONA_SUBJECT_PATTERN.search(clean)
    if persona:
        issues.append({
            "kind": "用户主语句式",
            "match": persona.group(0),
            "message": f"出现「{persona.group(0)}」这类把使用者当主语的说法：改写成使用者视角的直接需求，例如「注册后能…」",
        })
    return issues


def upstream_revision() -> str:
    data = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
    revision = str(data.get("revision") or "").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError(f"rnskill/ra-人话 版本记录无效: {SOURCE_PATH}")
    return revision


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def comments_to_text(comments: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for key in DIM_KEYS:
        text = str(comments.get(key, "")).strip()
        parts.append(f"{DIM_LABELS[key]}\n{text}")
    other = str(comments.get("other", "")).strip()
    if other:
        parts.append(f"其他问题\n{other}")
    return "\n\n".join(parts)


def scan_text(text: str, target_type: str) -> list[dict[str, str]]:
    clean = text.strip()
    issues: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    matched_phrases: list[str] = []
    for phrase in sorted(HARD_BAN_PHRASES, key=len, reverse=True):
        if phrase not in clean:
            continue
        if any(phrase in matched for matched in matched_phrases):
            continue
        key = ("phrase", phrase)
        if key not in seen:
            issues.append({"kind": "硬禁表达", "match": phrase, "message": f"命中 ra-人话 硬禁表达: {phrase}"})
            seen.add(key)
            matched_phrases.append(phrase)
    for name, pattern in HARD_BAN_PATTERNS:
        match = pattern.search(clean)
        if not match:
            continue
        excerpt = match.group(0).replace("\n", " ")[:80]
        key = ("pattern", name, excerpt)
        if key not in seen:
            issues.append({"kind": name, "match": excerpt, "message": f"命中 {name}: {excerpt}"})
            seen.add(key)
    if target_type == "prompt" and clean.count("验证") >= 2:
        issues.append({"kind": "重复收束", "match": "多次验证收束", "message": "验证句连续重复，读起来像任务模板"})
    if target_type == "prompt":
        issues.extend(prompt_design_issues(clean))
    return issues


def validate_review(
    review: Mapping[str, Any],
    text: str,
    target_type: str,
    expected_revision: str | None = None,
) -> list[str]:
    errors: list[str] = []
    expected_revision = expected_revision or upstream_revision()
    required = (
        "policyVersion",
        "targetType",
        "textSha256",
        "upstreamRevision",
        "hardBans",
        "unresolvedFindings",
        "rewriteRounds",
        "reviewedBy",
        "passed",
    )
    for key in required:
        if key not in review:
            errors.append(f"审核记录缺少字段: {key}")

    if review.get("policyVersion") != POLICY_VERSION:
        errors.append(f"审核策略版本不正确: {review.get('policyVersion')}")
    if review.get("targetType") != target_type:
        errors.append(f"审核对象类型不匹配: {review.get('targetType')}")
    if review.get("upstreamRevision") != expected_revision:
        errors.append(f"rnskill/ra-人话 版本不匹配: {review.get('upstreamRevision')}")
    if review.get("textSha256") != text_sha256(text):
        errors.append("审核记录与当前文本不一致，需要重新审核")
    hard_bans = review.get("hardBans")
    if not isinstance(hard_bans, list):
        errors.append("hardBans 必须是数组")
    elif hard_bans:
        errors.append("仍有 ra-人话 硬禁表达")
    unresolved = review.get("unresolvedFindings")
    if not isinstance(unresolved, list):
        errors.append("unresolvedFindings 必须是数组")
    elif unresolved:
        errors.append("仍有未解决的 ra-人话 问题")
    if review.get("passed") is not True:
        errors.append("审核记录未标记通过")
    reviewed_by = str(review.get("reviewedBy") or "")
    if "ra-人话" not in reviewed_by:
        errors.append("审核记录未包含 ra-人话")
    try:
        rounds = int(review.get("rewriteRounds"))
    except (TypeError, ValueError):
        errors.append("rewriteRounds 不是整数")
    else:
        if not 0 <= rounds <= 3:
            errors.append(f"rewriteRounds 超出范围: {rounds}")
    return errors


def load_review(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("审核记录必须是 JSON 对象")
    return data


def round_comment_evidence_path(project_root: Path, round_no: int) -> Path:
    return project_root / "checks" / "comment-evidence" / f"r{round_no:02d}.json"


def split_comment_sentences(text: str) -> list[str]:
    return [
        part.strip()
        for part in SENTENCE_SPLIT_RE.split(text)
        if part.strip() and not SCORE_ONLY_RE.match(part.strip())
    ]


def _resolve_evidence_path(value: Any, project_root: Path) -> Path | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = project_root / path
    return path.resolve()


def _read_evidence_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def _extract_scope_symbols(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    patterns = (
        re.compile(r"^\s*func\s+(Test[A-Za-z0-9_]+)\s*\(", re.M),
        re.compile(r"^\s*def\s+(test_[A-Za-z0-9_]+)\s*\(", re.M),
        re.compile(r"^\s*(?:it|test)\s*\(\s*['\"]([^'\"]+)['\"]", re.M),
    )
    found: list[str] = []
    for pattern in patterns:
        found.extend(pattern.findall(text))
    return sorted(set(found))


def _validate_evidence_item(
    dimension: str,
    item: Any,
    comment_text: str,
    project_root: Path,
) -> list[str]:
    label = DIM_LABELS[dimension]
    errors: list[str] = []
    if not isinstance(item, Mapping):
        return [f"{label} 的证据条目必须是对象"]

    claim = str(item.get("claim") or "").strip()
    kind = str(item.get("kind") or "").strip()
    polarity = str(item.get("polarity") or "").strip().lower()
    if not claim:
        errors.append(f"{label} 的证据条目缺少 claim")
    elif claim not in comment_text:
        errors.append(f"{label} 的证据 claim 不是维度描述原文: {claim[:40]}")
    elif re.search(r"[。；;！？!?]", claim):
        errors.append(f"{label} 的 claim 只能写一个事实短句，不能跨句: {claim[:40]}")
    if kind not in {"trace_quote", "file_quote", "command_result", "source_scope"}:
        errors.append(f"{label} 的证据 kind 不支持: {kind or '缺少'}")
    if polarity not in {"positive", "negative", "mixed"}:
        errors.append(f"{label} 的证据 polarity 必须是 positive、negative 或 mixed")
    elif claim:
        claim_positive = bool(POSITIVE_CLAIM_RE.search(claim))
        claim_negative = bool(NEGATIVE_CLAIM_RE.search(claim))
        if claim_positive and not claim_negative and polarity == "negative":
            errors.append(f"{label} 的正面 claim 绑定了负面证据: {claim[:40]}")
        if claim_negative and not claim_positive and polarity == "positive":
            errors.append(f"{label} 的负面 claim 绑定了正面证据: {claim[:40]}")

    path = _resolve_evidence_path(item.get("path"), project_root)
    if path is None:
        errors.append(f"{label} 的证据缺少 path: {claim[:40]}")
    elif not path.is_file():
        errors.append(f"{label} 的证据文件不存在: {path}")

    if claim and ABSENCE_CLAIM_RE.search(claim):
        if kind != "source_scope":
            errors.append(
                f"{label} 的缺失类判断必须用 source_scope 证明文件中实际覆盖范围: {claim[:40]}"
            )
        elif item.get("complete") is not True:
            errors.append(f"{label} 的 source_scope 必须设置 complete 为 true: {claim[:40]}")

    if path and path.is_file() and kind in {"trace_quote", "file_quote", "command_result"}:
        quote = str(item.get("quote") or "").strip()
        try:
            line_start = int(item.get("lineStart"))
            line_end = int(item.get("lineEnd"))
        except (TypeError, ValueError):
            errors.append(f"{label} 的证据缺少有效 lineStart 或 lineEnd: {claim[:40]}")
        else:
            lines = _read_evidence_lines(path)
            if line_start < 1 or line_end < line_start or line_end > len(lines):
                errors.append(
                    f"{label} 的证据行号越界: {path.name}:{line_start}-{line_end}"
                )
            elif not quote:
                errors.append(f"{label} 的 quote 为空: {claim[:40]}")
            elif quote not in "\n".join(lines[line_start - 1:line_end]):
                errors.append(
                    f"{label} 的 quote 不在指定轨迹或文件范围内: {path.name}:{line_start}-{line_end}"
                )
            else:
                if polarity == "positive" and FAIL_EVIDENCE_RE.search(quote):
                    errors.append(
                        f"{label} 的正面判断引用了失败原文，除非这是修复后结果，否则必须更换证据: {claim[:40]}"
                    )
                if polarity == "negative" and not FAIL_EVIDENCE_RE.search(quote):
                    errors.append(
                        f"{label} 的负面判断引用了通过或无关原文，必须改用失败原文或 source_scope: {claim[:40]}"
                    )

    if path and path.is_file() and kind == "source_scope":
        symbols = item.get("symbols")
        if not isinstance(symbols, list) or not all(str(value).strip() for value in symbols):
            errors.append(f"{label} 的 source_scope 缺少 symbols: {claim[:40]}")
        else:
            expected = sorted(set(str(value).strip() for value in symbols))
            actual = _extract_scope_symbols(path)
            if item.get("complete") is True and actual != expected:
                errors.append(
                    f"{label} 的 source_scope 覆盖清单不一致，文件中实际为 {actual}: {path.name}"
                )
            elif item.get("complete") is not True:
                missing = [value for value in expected if value not in actual]
                if missing:
                    errors.append(
                        f"{label} 的 source_scope 未找到符号 {missing}: {path.name}"
                    )
    return errors


def validate_comment_consistency(
    comments: Mapping[str, Any],
    evidence_path: Path,
    project_root: Path,
    expected_round: int | None = None,
    expected_session_id: str = "",
    expected_prompt_id: str = "",
    scores: Mapping[str, Any] | None = None,
) -> list[str]:
    """校验每个维度每句事实都有可回指的轨迹、文件或命令证据。"""
    errors: list[str] = []
    if not evidence_path.is_file():
        return [f"缺少评语证据索引，禁止提交: {evidence_path}"]
    try:
        document = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"评语证据索引不可读: {evidence_path}: {exc}"]
    if not isinstance(document, Mapping):
        return [f"评语证据索引必须是 JSON 对象: {evidence_path}"]
    try:
        schema_version = int(document.get("schemaVersion") or 0)
    except (TypeError, ValueError):
        schema_version = 0
    if schema_version != COMMENT_EVIDENCE_SCHEMA_VERSION:
        errors.append(
            f"评语证据索引 schemaVersion 必须是 {COMMENT_EVIDENCE_SCHEMA_VERSION}: {evidence_path}"
        )
    if expected_round is not None:
        try:
            evidence_round = int(document.get("round"))
        except (TypeError, ValueError):
            evidence_round = -1
        if evidence_round != expected_round:
            errors.append(
                f"评语证据索引轮次不一致，期望 {expected_round}，实际 {evidence_round}: {evidence_path}"
            )
    for field, expected in (
        ("sessionId", expected_session_id),
        ("promptId", expected_prompt_id),
    ):
        if not expected:
            continue
        actual = str(document.get(field) or "").strip()
        if actual != expected:
            errors.append(
                f"评语证据索引 {field} 不一致，期望 {expected}，实际 {actual or '缺失'}: {evidence_path}"
            )
    dimensions = document.get("dimensions")
    if not isinstance(dimensions, Mapping):
        return errors + [f"评语证据索引缺少 dimensions: {evidence_path}"]

    for dimension in DIM_KEYS:
        label = DIM_LABELS[dimension]
        text = str(comments.get(dimension) or "").strip()
        items = dimensions.get(dimension)
        if not isinstance(items, list) or not items:
            errors.append(f"{label} 没有评语证据，禁止提交")
            continue
        item_errors: list[str] = []
        for item in items:
            item_errors.extend(
                _validate_evidence_item(dimension, item, text, project_root)
            )
        errors.extend(item_errors)
        valid_items = [
            item
            for item in items
            if isinstance(item, Mapping)
            and str(item.get("claim") or "").strip()
            and str(item.get("claim") or "").strip() in text
        ]
        for sentence in split_comment_sentences(text):
            if not any(str(item.get("claim") or "").strip() in sentence for item in valid_items):
                errors.append(f"{label} 有事实句没有绑定证据: {sentence[:60]}")
        if scores:
            try:
                score = float(scores.get(dimension))
            except (TypeError, ValueError):
                score = 5.0
            if score < 5 and not any(
                str(item.get("polarity") or "").strip().lower()
                in {"negative", "mixed"}
                for item in valid_items
            ):
                errors.append(
                    f"{label} 得分 {score:g} 但没有负面或混合证据，扣分依据不成立"
                )
    return errors


def round_review_path(root: Path, round_no: int, target_type: str) -> Path:
    return root / f"r{round_no:02d}-{target_type}.json"

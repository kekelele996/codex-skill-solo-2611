"""题面设计、难度证据与 ra-人话审核门禁。

本模块吸收 `sologsb-0917` 已经踩过的 G16/G17 规则，但按 26.11 的单轮
pointwise 形态收窄：题面仍然只读一次，领域由所选大类决定；难度论证必须
回指真实源码和题面原句，不能靠“步骤多、文件多”自证困难或地狱。
"""

from __future__ import annotations

import difflib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from common import SKILL_ROOT, non_space_len, sha256_bytes, write_json

DIFFICULTY_TEMPLATE = SKILL_ROOT / "references" / "difficulty-review-template.json"
RA_RENHUA_TEMPLATE = SKILL_ROOT / "references" / "ra-renhua-review-template.json"
RA_RENHUA_REVISION = "7cbf47b57cbae0596d46c10c4a1445cbb27904b3"
SOLO_ANNOTATION_SCRIPTS = SKILL_ROOT.parent / "solo-annotation-loop" / "scripts"

MAX_PROMPT_CHARS = 240
PREFERRED_PROMPT_CHARS = 170
MAX_RIGID_TERMS = 3
MAX_SEMICOLONS = 2

MARKDOWN_RE = re.compile(r"(^|\n)\s{0,3}(#{1,6}|[-*+]\s|\d+[.)]\s|```)|[`*_]{2,}|\[[^\]]+\]\([^)]*\)")
PATH_OR_CODE_RE = re.compile(
    r"(?:/Users/|/home/|[A-Za-z]:\\|\b(?:src|frontend|backend|internal|cmd)/[\w./-]+|"
    r"\b(?:class|function|def|func|import|require)\b)"
)
GUIDED_FIX_RE = re.compile(r"(?:改成|改为|修改为|换成|替换为|加上|加入|删除|删掉|加锁|改返回|使用.+修复)")
ROUND_RE = re.compile(r"(第[一二三四五六七八九十百0-9]+轮|本轮|上一轮|下一轮|首轮|轮次)")
RIGID_RE = re.compile(r"(?:只能|仅能|仅|必须|不得|不能|一律|禁止|严禁|务必|只允许)")
TEMPLATE_TAIL_RE = re.compile(r"(?:刷新|并发|重复)[^。！？]{0,24}(?:一致|不乱|只生效一次|仍能读回)[。！？]?$")
TEMPLATE_WORD_RE = re.compile(r"(?:整次|任一步失败全部回滚|保持不动)")
PORT_RE = re.compile(r"(端口|监听|localhost|127\.0\.0\.1|0\.0\.0\.0|PORT\s*=|(?<![\d:]):\d{2,5}(?!\d))", re.I)
CONTAINER_RE = re.compile(r"(?i)(docker(?:\s*compose)?|kubernetes|k8s|容器化|容器方案|容器编排|镜像(?:构建|部署|方案|化)?)")
TECH_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"react|vue|angular|svelte|next\.?js|nuxt|vite|webpack|tailwind|"
    r"typescript|javascript|node\.?js|deno|java|kotlin|scala|golang|php|ruby|rails|"
    r"python|django|flask|fastapi|celery|spring(?:boot|cloud)?|mybatis|hibernate|jpa|"
    r"mysql|postgres(?:ql)?|mariadb|sqlite|mongodb|redis|elasticsearch|kafka|rabbitmq|"
    r"zookeeper|nginx|minio|jenkins|prometheus|grafana|airflow|dbt|spark|flink|"
    r"maven|gradle|npm|yarn|pnpm|poetry|cargo|tomcat|"
    r"restful|graphql|grpc|websocket|jwt|oauth2?|swagger|openapi|sql"
    r")(?![A-Za-z0-9])",
    re.I,
)
TECH_CONSTRAINT_RE = re.compile(r"(技术栈|技术选型|技术方案|技术路线|技术架构|框架版本|依赖版本|版本要求)")
USER_PERSONA_RE = re.compile(
    r"(用户|使用者)(?:可以|可|能|会|需|需要|要|应|应当|必须|无法|点击|打开|看到|查看|进入|"
    r"登录|注册|提交|申请|上传|下载|填写|选择|管理|操作|使用|访问|发起|确认|取消)"
)
HARD_BAN_PHRASES = (
    "真实用户", "实际用户", "真正", "其实", "本质上", "核心在于", "关键在于",
    "更重要的是", "这说明", "这背后", "结果有点出乎意料", "说白了", "归根结底",
)
AI_TEMPLATE_RE = re.compile(
    r"(?:不是[^。]{0,60}而是|并非[^。]{0,60}而是|不仅[^。]{0,80}(?:更|还|而且)|"
    r"首先[^。]{0,50}其次|综上|总而言之|综上所述)"
)
DATA_TERMS = re.compile(
    r"(数据|采集|抓取|接口|解析|清洗|去重|入库|仓库|数据集|管道|任务|调度|同步|"
    r"增量|全量|批次|回填|重算|对账|血缘|质量|隔离|字段|分区|水位|事件|记录|ETL|CDC|RPA)",
    re.I,
)

PLATFORM_SIGNAL_KEYS = ("multiModule", "designTradeoff", "complexConcern")
COMPLEXITY_PATTERNS: dict[str, re.Pattern[str]] = {
    "concurrency": re.compile(r"(并发|同时(?:写入|提交|同步|重跑)|多任务.{0,12}(?:覆盖|冲突)|竞态|租约|加锁)"),
    "failure-recovery": re.compile(r"(失败后.{0,16}(?:恢复|重试|续跑|断点)|中断.{0,16}(?:续跑|恢复)|崩溃.{0,16}(?:恢复|续跑))"),
    "migration": re.compile(r"((?:旧数据|历史数据|旧字段|旧分区).{0,16}(?:迁移|升级|兼容|回填)|(?:迁移|升级).{0,16}(?:回退|兼容|历史数据))"),
    "permission-boundary": re.compile(r"(越权|租户|数据域|角色.{0,10}(?:权限|边界)|权限.{0,10}(?:拒绝|隔离))"),
    "capacity": re.compile(r"((?:大批量|超大文件|超时|容量|吞吐|内存).{0,16}(?:拒绝|降级|分批|排队|限流)|(?:拒绝|降级|分批|排队|限流).{0,16}(?:大批量|超大文件|容量))"),
    "offline-merge": re.compile(r"((?:离线|断网).{0,18}(?:合并|同步|恢复|冲突)|(?:网络恢复|重新连接).{0,18}(?:合并|同步|冲突))"),
    "state-invalidation": re.compile(r"((?:源数据|上游|字段|规则|水位).{0,16}(?:变化|更新).{0,16}(?:失效|重算|作废|重新跑)|(?:过期|失效).{0,16}(?:重算|重新跑|拒绝))"),
    "cross-system-reconciliation": re.compile(r"(对账|跨系统|源库.{0,16}(?:一致|不一致|冲突)|数仓.{0,16}(?:一致|不一致|冲突)|外部系统.{0,16}(?:返回|同步|不一致))"),
    "idempotency": re.compile(r"(幂等|重复(?:执行|运行|投递).{0,16}(?:不新增|不重复|只生效一次|覆盖)|断点续跑.{0,16}(?:不重复|不新增))"),
    "late-data": re.compile(r"((?:迟到|晚到|乱序).{0,16}(?:事件|记录|数据)|水位.{0,16}(?:迟到|晚到|乱序|回退))"),
    "schema-evolution": re.compile(r"((?:字段|Schema|表结构).{0,16}(?:新增|删除|变更|演进).{0,16}(?:兼容|迁移|隔离)|(?:兼容|迁移).{0,16}(?:字段|Schema|表结构))", re.I),
}

# 平台点名的“在既有页面补一层”形态。命中后必须解释为什么它不只是多一步。
MEDIUM_SHAPE_RE = re.compile(
    r"(?:(?:新增|补一个|补上|补一份|补一套|加一个|增加|做一个)[^。；！？]{0,16}"
    r"(?:视图|页面|面板|弹窗|预览|清单|列表|统计|导出|字段|按钮|标签|卡片|入口|台账|看板|图表|记录|流程|规则))"
    r"|(?:(?:视图|页面|面板|弹窗|预览|清单|列表|统计|导出|字段|按钮|标签|卡片|入口|台账|看板|图表|记录|流程|规则)"
    r"[^。；！？]{0,12}(?:新增|补一个|补上|补一份|补一套|加一个|增加|做一个))"
)


def normalize(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def prompt_sha256(text: str) -> str:
    return sha256_bytes(text.strip().encode("utf-8"))


def prompt_file_sha256(text: str) -> str:
    """Hash the exact bytes that will be sent to the model, including final newline."""
    return sha256_bytes(text.encode("utf-8"))


def _text_ok(value: Any, minimum: int) -> bool:
    return non_space_len(str(value or "")) >= minimum


def _load_json(path: Path) -> tuple[dict[str, Any], list[str]]:
    if not path.is_file():
        return {}, [f"文件不存在: {path}"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, [f"JSON 无法解析: {path}: {exc}"]
    if not isinstance(data, dict):
        return {}, [f"JSON 顶层必须是对象: {path}"]
    return data, []


def ensure_templates(task_root: Path, *, force: bool = False) -> dict[str, str]:
    """铺好题面难度论证和 ra-人话审核记录，不覆盖人工已填内容。"""
    out_dir = task_root / "monitor" / "prompt"
    out_dir.mkdir(parents=True, exist_ok=True)
    targets = {
        "difficultyReview": out_dir / "difficulty-review.json",
        "raRenhuaReview": out_dir / "ra-renhua-review.json",
    }
    sources = {
        "difficultyReview": DIFFICULTY_TEMPLATE,
        "raRenhuaReview": RA_RENHUA_TEMPLATE,
    }
    for key, target in targets.items():
        if force or not target.exists():
            shutil.copyfile(sources[key], target)
    return {key: str(path) for key, path in targets.items()}


def validate_ra_renhua_review(path: Path, prompt_text: str) -> list[str]:
    review, errors = _load_json(path)
    if errors:
        return errors
    required = (
        "policyVersion", "targetType", "textSha256", "upstreamRevision",
        "hardBans", "unresolvedFindings", "rewriteRounds", "reviewedBy", "passed",
    )
    errors.extend(f"ra-人话审核记录缺少字段: {key}" for key in required if key not in review)
    if review.get("policyVersion") != "solo-ra-renhua-v1":
        errors.append("ra-人话 policyVersion 必须是 solo-ra-renhua-v1")
    if review.get("targetType") != "prompt":
        errors.append("ra-人话 targetType 必须是 prompt")
    if review.get("textSha256") != prompt_sha256(prompt_text):
        errors.append("ra-人话审核记录与当前题面不一致，需要按最终题面重算 textSha256")
    if review.get("upstreamRevision") != RA_RENHUA_REVISION:
        errors.append("ra-人话版本不是本期固定版本，需按 rnskill/ra-人话 1.2.0 重新审核")
    if review.get("hardBans") != []:
        errors.append("ra-人话 hardBans 必须为空")
    if review.get("unresolvedFindings") != []:
        errors.append("ra-人话 unresolvedFindings 必须为空")
    try:
        rounds = int(review.get("rewriteRounds"))
    except (TypeError, ValueError):
        rounds = -1
    if not 0 <= rounds <= 3:
        errors.append("ra-人话 rewriteRounds 必须是 0–3 的整数")
    if "ra-人话" not in str(review.get("reviewedBy") or ""):
        errors.append("ra-人话 reviewedBy 必须包含 ra-人话")
    if review.get("passed") is not True:
        errors.append("ra-人话审核记录 passed 必须为 true")
    return errors


def validate_ra_renhua_external(prompt_path: Path, review_path: Path) -> list[str]:
    """调用固定版本的 ra-人话 校验器，避免只靠自填 JSON 绕过审核。"""
    validate_script = SOLO_ANNOTATION_SCRIPTS / "validate-prompt.py"
    audit_script = SOLO_ANNOTATION_SCRIPTS / "audit-human-writing.py"
    missing = [str(path) for path in (validate_script, audit_script) if not path.is_file()]
    if missing:
        return ["缺少固定版本 ra-人话 校验器: " + "、".join(missing)]
    errors: list[str] = []
    commands = [
        [sys.executable, str(validate_script), str(prompt_path), "--review", str(review_path), "--json"],
        [
            sys.executable,
            str(audit_script),
            "--text-file",
            str(prompt_path),
            "--target-type",
            "prompt",
            "--review",
            str(review_path),
            "--json",
        ],
    ]
    for command in commands:
        proc = subprocess.run(command, capture_output=True, text=True, check=False)
        try:
            result = json.loads(proc.stdout)
        except json.JSONDecodeError:
            errors.append(
                (proc.stderr or proc.stdout).strip()
                or f"ra-人话 校验器没有返回 JSON: {Path(command[1]).name}"
            )
            continue
        errors.extend(str(item) for item in (result.get("errors") or []))
    return list(dict.fromkeys(errors))


def _validate_difficulty(
    path: Path,
    *,
    task_root: Path,
    prompt_text: str,
    difficulty: str,
) -> dict[str, Any]:
    data, errors = _load_json(path)
    if errors:
        return {"ok": False, "errors": errors}
    if data.get("schemaVersion") != 1:
        errors.append("难度论证 schemaVersion 必须是 1")
    if data.get("difficulty") != difficulty:
        errors.append(f"难度论证 difficulty 必须与命令行一致: {difficulty}")
    if data.get("routineOnly") is not False:
        errors.append("routineOnly 必须明确为 false")
    if data.get("verdict") != difficulty:
        errors.append(f"难度论证 verdict 必须是 {difficulty}")
    if not str(data.get("reviewedBy") or "").strip():
        errors.append("难度论证缺少 reviewedBy")

    signals = data.get("signals") if isinstance(data.get("signals"), dict) else {}
    passed: dict[str, bool] = {}
    for key, label in zip(
        PLATFORM_SIGNAL_KEYS,
        ("多模块整合", "关键设计取舍", "复杂技术关注点"),
    ):
        item = signals.get(key) if isinstance(signals.get(key), dict) else {}
        passed[key] = item.get("passed") is True
        if passed[key] and not _text_ok(item.get("evidence"), 20):
            errors.append(f"signals.{key}.passed=true 时 evidence 至少 20 字，并回指真实源码或需求（{label}）")
    if passed.get("multiModule"):
        modules = [str(item).strip() for item in (signals.get("multiModule", {}).get("modules") or [])]
        if len([item for item in modules if item]) < 3:
            errors.append("多模块整合通过时至少列出 3 个已有模块或数据边界")
        for key in ("crossModuleInvariant",):
            if not _text_ok(signals.get("multiModule", {}).get(key), 20):
                errors.append(f"signals.multiModule.{key} 至少 20 字")
    if passed.get("designTradeoff"):
        for key in ("conflict", "decision", "whyNotRoutine"):
            if not _text_ok(signals.get("designTradeoff", {}).get(key), 20):
                errors.append(f"signals.designTradeoff.{key} 至少 20 字")
    if passed.get("complexConcern"):
        kinds = [str(item) for item in (signals.get("complexConcern", {}).get("kinds") or [])]
        if not kinds:
            errors.append("复杂技术关注点通过时 kinds 不能为空")
        unknown = sorted(set(kinds) - set(COMPLEXITY_PATTERNS))
        if unknown:
            errors.append("complexConcern.kinds 含未知类型: " + "、".join(unknown))
        for key in ("technicalRisk", "observableFailure"):
            if not _text_ok(signals.get("complexConcern", {}).get(key), 20):
                errors.append(f"signals.complexConcern.{key} 至少 20 字")

    passed_count = sum(passed.values())
    if passed_count < 2:
        errors.append("困难/地狱题至少满足多模块整合、关键设计取舍、复杂技术关注点中的两项")
    if not (passed.get("designTradeoff") or passed.get("complexConcern")):
        errors.append("至少满足关键设计取舍或复杂技术关注点之一，不能只靠多模块堆文件")
    if difficulty == "地狱" and passed_count < 3:
        errors.append("地狱题必须同时满足三项难度特征")
    if difficulty == "地狱":
        hell = data.get("hellSignal") if isinstance(data.get("hellSignal"), dict) else {}
        if hell.get("passed") is not True:
            errors.append("地狱题必须提供 hellSignal.passed=true")
        allowed_hell = {"open-ended", "hidden-constraints", "architecture-level", "multi-step-debugging", "cross-system-recovery"}
        kinds = {str(item) for item in (hell.get("kinds") or [])}
        if not kinds:
            errors.append("hellSignal.kinds 不能为空")
        elif sorted(kinds - allowed_hell):
            errors.append("hellSignal.kinds 含未知类型: " + "、".join(sorted(kinds - allowed_hell)))
        for key in ("reason", "evidence"):
            if not _text_ok(hell.get(key), 20):
                errors.append(f"hellSignal.{key} 至少 20 字")

    platform = data.get("platformSignals") if isinstance(data.get("platformSignals"), dict) else {}
    for key in PLATFORM_SIGNAL_KEYS:
        item = platform.get(key) if isinstance(platform.get(key), dict) else {}
        for field, minimum in (("answer", 20), ("ifViolated", 12)):
            if not _text_ok(item.get(field), minimum):
                errors.append(f"platformSignals.{key}.{field} 至少 {minimum} 字")
        quote = str(item.get("promptQuote") or "").strip()
        if non_space_len(quote) < 12:
            errors.append(f"platformSignals.{key}.promptQuote 至少引用 12 字题面原句")
        elif normalize(quote) not in normalize(prompt_text):
            errors.append(f"platformSignals.{key}.promptQuote 不是题面原句")

    invariant = data.get("crossObjectInvariant") if isinstance(data.get("crossObjectInvariant"), dict) else {}
    objects = [str(item).strip() for item in (invariant.get("objects") or []) if str(item).strip()]
    if len(objects) < 2:
        errors.append("crossObjectInvariant.objects 至少列出 2 个业务对象")
    for name in objects:
        if normalize(name) not in normalize(prompt_text):
            errors.append(f"crossObjectInvariant 对象“{name}”必须出现在题面里")
    for key, minimum in (("invariant", 20), ("divergenceFailure", 12)):
        if not _text_ok(invariant.get(key), minimum):
            errors.append(f"crossObjectInvariant.{key} 至少 {minimum} 字")

    project = data.get("projectEvidence") if isinstance(data.get("projectEvidence"), dict) else {}
    files = [str(item).strip() for item in (project.get("files") or []) if str(item).strip()]
    required_files = 3 if difficulty == "地狱" else 2
    if len(files) < required_files:
        errors.append(f"projectEvidence.files 至少列出 {required_files} 个真实源码文件")
    workspace = task_root / "workspace"
    for relative in files:
        if not (workspace / relative).is_file():
            errors.append(f"projectEvidence.files 引用了不存在的源码文件: {relative}")
    for key in ("existingFlow", "whyNotRoutine"):
        if not _text_ok(project.get(key), 20):
            errors.append(f"projectEvidence.{key} 至少 20 字")

    substantive = data.get("substantiveComplexity") if isinstance(data.get("substantiveComplexity"), dict) else {}
    kinds = [str(item) for item in (substantive.get("kinds") or [])]
    if not kinds:
        errors.append("substantiveComplexity.kinds 至少一项")
    unknown = sorted(set(kinds) - set(COMPLEXITY_PATTERNS))
    if unknown:
        errors.append("substantiveComplexity.kinds 含未知类型: " + "、".join(unknown))
    supported = [kind for kind in kinds if kind in COMPLEXITY_PATTERNS and COMPLEXITY_PATTERNS[kind].search(prompt_text)]
    if not supported:
        errors.append("题面没有出现可核对的实质复杂度信号，不能标困难或地狱")
    if difficulty == "地狱" and len(supported) < 2:
        errors.append("地狱题至少需要两类由题面原句支撑的实质复杂度信号")
    quote = str(substantive.get("promptQuote") or "").strip()
    if non_space_len(quote) < 12:
        errors.append("substantiveComplexity.promptQuote 至少引用 12 字题面原句")
    elif normalize(quote) not in normalize(prompt_text):
        errors.append("substantiveComplexity.promptQuote 不是题面原句")
    elif not any(COMPLEXITY_PATTERNS[kind].search(quote) for kind in kinds if kind in COMPLEXITY_PATTERNS):
        errors.append("substantiveComplexity.promptQuote 没有体现声明的复杂度")
    for key, minimum in (("whyHard", 20), ("visibleFailure", 12)):
        if not _text_ok(substantive.get(key), minimum):
            errors.append(f"substantiveComplexity.{key} 至少 {minimum} 字")

    if MEDIUM_SHAPE_RE.search(prompt_text) and not _text_ok(data.get("mediumShapeDefense"), 20):
        errors.append("题面命中“在既有模块补一层”的中等形态，mediumShapeDefense 必须写清为什么不是只加一层")

    return {
        "ok": not errors,
        "errors": errors,
        "signalsPassed": passed_count,
        "substantiveKinds": supported,
        "mediumShapeMatched": bool(MEDIUM_SHAPE_RE.search(prompt_text)),
    }


def _history_errors(history: list[dict[str, Any]], text: str, task_root: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    clean = normalize(text)
    shingles = {clean[index:index + 12] for index in range(max(0, len(clean) - 11))}
    for row in history:
        if row.get("taskRoot") == str(task_root):
            continue
        other = normalize(str(row.get("text") or ""))
        if not other:
            continue
        label = str(row.get("taskRoot") or "历史任务")
        if clean == other:
            errors.append(f"与历史题完全相同: {label}")
            continue
        overlap = sorted(shingles & {other[index:index + 12] for index in range(max(0, len(other) - 11))})
        if overlap:
            errors.append(f"与历史题存在连续片段复用: {label}: {overlap[0]}")
            continue
        ratio = difflib.SequenceMatcher(None, other, clean).ratio()
        if ratio >= 0.75:
            errors.append(f"与历史题相似度 {ratio:.2f}: {label}")
        elif ratio >= 0.55:
            warnings.append(f"与历史题相似度 {ratio:.2f}: {label}，确认不是雷同项目加雷同需求")
    return errors, warnings


def validate_candidate(
    *,
    task_root: Path,
    prompt_path: Path,
    prompt_text: str,
    domain: str,
    difficulty: str,
    review_path: Path,
    difficulty_review_path: Path,
    history: list[dict[str, Any]],
    allow_over_170: bool = False,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    clean = prompt_text.strip()
    if not clean:
        errors.append("提示词为空")
    chars = len(clean)
    if chars > MAX_PROMPT_CHARS:
        errors.append(f"提示词超过 {MAX_PROMPT_CHARS} 字，当前 {chars} 字")
    if chars > PREFERRED_PROMPT_CHARS and not allow_over_170:
        errors.append(
            f"首轮提示词优先控制在 {PREFERRED_PROMPT_CHARS} 字内，当前 {chars} 字；"
            "确有必要时显式加 --allow-over-170"
        )
    if MARKDOWN_RE.search(clean):
        errors.append("题面包含 Markdown；提交时要像业务方直接交代需求")
    if PATH_OR_CODE_RE.search(clean):
        errors.append("题面包含文件路径或代码术语；源码定位留在难度论证和轨迹证据里")
    if GUIDED_FIX_RE.search(clean):
        errors.append("题面包含引导性修复措辞，等于直接告诉模型怎么改")
    if ROUND_RE.search(clean):
        errors.append("题面包含轮次表述")
    port = PORT_RE.search(clean)
    if port:
        errors.append(f"题面出现端口配置「{port.group(0)}」；端口和启动方式由仓库自行决定")
    container = CONTAINER_RE.search(clean)
    if container:
        errors.append(f"题面出现容器方案要求「{container.group(0)}」；Docker 只用于隔离 Harness")
    tech = TECH_RE.search(clean)
    if tech:
        errors.append(f"题面指定了技术栈「{tech.group(0)}」；语言/框架只填交付表，不替模型做技术选型")
    tech_constraint = TECH_CONSTRAINT_RE.search(clean)
    if tech_constraint:
        errors.append(f"题面出现技术约束「{tech_constraint.group(0)}」；现有项目结构已经限定实现边界")
    persona = USER_PERSONA_RE.search(clean)
    if persona:
        errors.append(f"出现「{persona.group(0)}」这类第三人称用户句式，改成直接描述业务结果")
    rigid = RIGID_RE.findall(clean)
    if len(rigid) > MAX_RIGID_TERMS:
        errors.append(f"“只能/必须/不得”等硬性措辞出现 {len(rigid)} 次，最多 {MAX_RIGID_TERMS} 次")
    if clean.count("；") > MAX_SEMICOLONS:
        errors.append(f"分号超过 {MAX_SEMICOLONS} 个，规则被逐条罗列")
    if TEMPLATE_TAIL_RE.search(clean):
        errors.append("结尾是“刷新后/并发后……一致”式模板收尾")
    banned = sorted(set(HARD_BAN_PHRASES) & {phrase for phrase in HARD_BAN_PHRASES if phrase in clean})
    if banned:
        errors.append("命中 ra-人话 硬禁表达: " + "、".join(banned))
    ai_template = AI_TEMPLATE_RE.search(clean)
    if ai_template:
        errors.append(f"命中 ra-人话 模板句式: {ai_template.group(0)}")
    if domain == "数据工程" and len(DATA_TERMS.findall(clean)) < 2:
        warnings.append("题面里数据工程信号偏少，确认这是真实的数据/管道/自动化需求，而不是普通后台功能")

    difficulty_result = _validate_difficulty(
        difficulty_review_path,
        task_root=task_root,
        prompt_text=clean,
        difficulty=difficulty,
    )
    errors.extend(difficulty_result.get("errors") or [])
    errors.extend(validate_ra_renhua_review(review_path, clean))
    errors.extend(validate_ra_renhua_external(prompt_path, review_path))
    history_errors, history_warnings = _history_errors(history, clean, task_root)
    errors.extend(history_errors)
    warnings.extend(history_warnings)
    if TEMPLATE_WORD_RE.search(clean):
        warnings.append("出现历史题里反复使用的套话，换成本题自己的业务说法")

    errors = list(dict.fromkeys(item for item in errors if item))
    warnings = list(dict.fromkeys(item for item in warnings if item))
    return {
        "ok": not errors,
        "charCount": chars,
        "textSha256": prompt_sha256(prompt_text),
        "fileSha256": prompt_file_sha256(prompt_text),
        "errors": errors,
        "warnings": warnings,
        "difficultyReview": difficulty_result,
        "raRenhuaReview": str(review_path),
    }


def write_validation(task_root: Path, result: dict[str, Any]) -> Path:
    path = task_root / "monitor" / "prompt" / "validation.json"
    write_json(path, result)
    return path

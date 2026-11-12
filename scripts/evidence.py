"""Feedback 事实核对：反馈里的每个可核验事实都必须能回查到轨迹、产物快照或本机真实运行。

门禁分三层：

1. 证据索引：由脚本自己从原始轨迹（工具真实输出）、产物快照和
   `monitor/runtime-verification.json` 构建，不接受 Agent 手填的"证据"。
2. 声明回查：抽取 Feedback 里的数值、文件路径、代码标识和报错原文，逐个在索引里查；
   查不到直接阻断，而不是给警告。
3. 运行前提：Feedback 必须建立在本机真实执行过产物之上；高分要有一次成功退出；
   写了失败表现就必须有真实的失败输出。

模型自述、计划和推测都不进索引；模型在轨迹里真实读到的内容和命令输出才进。
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from common import Solo2611Error, git, read_json, utc_now, write_json
import trace as tr

VERIFICATION_REL = Path("monitor") / "runtime-verification.json"
LOG_REL = Path("monitor") / "verification"
MAX_RUN_OUTPUT = 20000
MAX_CODE_FILES = 120
MAX_CODE_FILE_CHARS = 200_000
MAX_DERIVED_NUMBERS = 20000
SKIP_CODE_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".tar", ".mp4", ".mov", ".mp3",
    ".woff", ".woff2", ".ttf", ".otf", ".so", ".dylib", ".dll", ".exe", ".bin", ".lock",
)
SKIP_CODE_NAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml"}

# 中文紧邻的数字也要能抽出来（"订单120行"），所以边界只排除 ASCII 标识符字符，
# 不能用 \w —— Python 的 \w 含汉字，会把最常见的中文写法整体漏掉。
CLAIM_NUMBER = re.compile(r"(?<![0-9A-Za-z_.])(\d+(?:\.\d+)?|\.\d+)(?![0-9A-Za-z_.])")
STEP_REF = re.compile(r"第\s*(\d+)\s*步")
PATH_CLAIM = re.compile(
    r"(?<![\w./-])([\w./-]+\.(?:ts|tsx|js|jsx|mjs|cjs|glsl|wgsl|hlsl|vert|frag|comp|c|cc|cpp|cxx|h|hpp|py|rs|go|"
    r"java|cs|json|html|css|vue|md|toml|yaml|yml|mk|cmake|shader|sql|csv|tsv|parquet|avro|proto|ipynb))\b"
)
BACKTICK = re.compile(r"`([^`\n]{2,160})`")
ERROR_LIKE = re.compile(r"[\s\"'「」：“”]|Error|Exception|error|invalid|denied|refused|not found|不存在|失败|报错")
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DOTTED = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:[.\-/][A-Za-z_][A-Za-z0-9_]*)+$")

# 明确断言"实际发生过失败"的表述。设计里说"失败时会怎样"不算，必须有真实失败输出。
FAILURE_CLAIM = re.compile(
    r"(报错|崩溃|无法运行|跑不起来|编译失败|启动失败|构建失败|白屏|黑屏|运行失败|重跑失败|对账失败|"
    r"失败后|导致[^。；]{0,12}失败|停在原处|推进不了|无法推进)"
)

INLINE_EXEC = {
    "python", "python3", "python3.11", "python3.12", "python3.13", "python3.14",
    "node", "deno", "bun", "bash", "sh", "zsh", "pwsh", "perl", "ruby", "php",
}
FAKE_EXEC = {"echo", "printf", "true", "false", "yes", ":"}
INLINE_FLAGS = {"-c", "-e", "--eval", "/c", "-Command"}


# ======================================================================= 真实运行记录

def verification_path(task_root: Path) -> Path:
    return task_root / VERIFICATION_REL


def runs(task_root: Path) -> list[dict[str, Any]]:
    data = read_json(verification_path(task_root), {}) or {}
    return [item for item in (data.get("runs") or []) if isinstance(item, dict)]


def _synthetic_reason(argv: list[str]) -> str:
    """只打印常量文本、或内联执行一段字符串，不能当作产物真实运行。"""
    if not argv:
        return "空命令"
    exe = Path(argv[0]).name
    if exe in FAKE_EXEC:
        return f"{exe} 只回显文本，结果不来自产物"
    if exe in INLINE_EXEC and len(argv) > 1 and argv[1] in INLINE_FLAGS:
        return f"{exe} {argv[1]} 是内联字符串，不是运行产物"
    return ""


def record_run(
    task_root: Path,
    workspace: Path,
    argv: list[str],
    *,
    label: str,
    kind: str = "runtime",
    timeout: float = 900.0,
) -> dict[str, Any]:
    """真实执行一条命令并把原始输出固定下来。退出码非 0 同样记录，作为失败证据。"""
    argv = [str(item) for item in argv if str(item) != "--"]
    if not argv:
        raise Solo2611Error(
            "verify 需要真实执行的命令，例如："
            "verify --task-root ROOT --label '重跑批次' -- python3 checks/verify.py"
        )
    synthetic = _synthetic_reason(argv)
    if synthetic:
        raise Solo2611Error(f"这条命令不能作为证据：{synthetic}")
    if not label.strip():
        raise Solo2611Error("verify 必须用 --label 写清这条命令验证什么，例如 --label '重跑同一批看是否新增'")

    started = time.time()
    try:
        proc = subprocess.run(
            argv, cwd=str(workspace), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
        )
        exit_code, timed_out = proc.returncode, False
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired as exc:
        exit_code, timed_out = 124, True
        stdout = (exc.stdout or b"").decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
        stderr = (exc.stderr or b"").decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
    duration = round(time.time() - started, 2)

    existing = runs(task_root)
    index = len(existing) + 1
    log_dir = task_root / LOG_REL
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"run-{index:02d}.log"
    full = f"$ {' '.join(argv)}\nexit={exit_code}\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}\n"
    log_path.write_text(full, encoding="utf-8")

    run = {
        "index": index,
        "label": label.strip(),
        "kind": kind,
        "command": " ".join(argv),
        "argv": argv,
        "cwd": str(workspace),
        "exitCode": exit_code,
        "timedOut": timed_out,
        "durationSeconds": duration,
        "stdout": stdout[:MAX_RUN_OUTPUT],
        "stderr": stderr[:MAX_RUN_OUTPUT],
        "truncated": len(stdout) > MAX_RUN_OUTPUT or len(stderr) > MAX_RUN_OUTPUT,
        "outputSha256": hashlib.sha256(full.encode("utf-8")).hexdigest(),
        "log": str(log_path),
        "recordedAt": utc_now(),
    }
    data = read_json(verification_path(task_root), {}) or {}
    data.setdefault("runs", []).append(run)
    data["updatedAt"] = utc_now()
    write_json(verification_path(task_root), data)
    return run


# ======================================================================= 证据索引

def _content_blocks(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for event in events:
        msg = event.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            blocks.extend(b for b in content if isinstance(b, dict))
    return blocks


def _file_sha(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _trace_facts(task_root: Path) -> dict[str, Any]:
    """主会话轨迹里真实出现过的文件路径、输出文本、步骤数，以及是否出现过失败。"""
    facts: dict[str, Any] = {"outputs": [], "commands": [], "paths": set(), "failed": False, "steps": 0, "hashes": []}
    traces_dir = task_root / "traces"
    if not traces_dir.is_dir():
        return facts
    mains = tr.main_trace_files(traces_dir)
    for path in mains:
        facts["hashes"].append({"path": str(path), "sha256": _file_sha(path)})
        try:
            events = tr.load_events(path)
        except Solo2611Error:
            continue
        for block in _content_blocks(events):
            kind = block.get("type")
            if kind == "tool_use":
                facts["steps"] += 1
                payload = block.get("input") or {}
                if not isinstance(payload, dict):
                    continue
                for key in ("file_path", "notebook_path", "path"):
                    value = payload.get(key)
                    if isinstance(value, str) and value.strip():
                        facts["paths"].add(value.strip())
                command = payload.get("command")
                if isinstance(command, str) and command.strip():
                    facts["commands"].append(command)
            elif kind == "tool_result":
                text = tr._result_text(block).strip()
                if text:
                    facts["outputs"].append(text)
                if block.get("is_error"):
                    facts["failed"] = True
    for path in tr.subagent_files(mains[0]) if mains else []:
        facts["hashes"].append({"path": str(path), "sha256": _file_sha(path)})
        try:
            events = tr.load_events(path)
        except Solo2611Error:
            continue
        for block in _content_blocks(events):
            if block.get("type") == "tool_result":
                text = tr._result_text(block).strip()
                if text:
                    facts["outputs"].append(text)
                if block.get("is_error"):
                    facts["failed"] = True
    return facts


def _artifact_text(workspace: Path, sha: str) -> tuple[str, list[str]]:
    names = git(workspace, "ls-tree", "-r", "--name-only", sha, check=False).splitlines()
    if not names:
        return "", []
    texts: list[str] = []
    used: list[str] = []
    total = 0
    for name in names:
        if len(used) >= MAX_CODE_FILES or total >= MAX_CODE_FILE_CHARS * 4:
            break
        if Path(name).name in SKIP_CODE_NAMES or name.endswith(SKIP_CODE_SUFFIXES):
            continue
        target = workspace / name
        if not target.is_file():
            continue
        try:
            chunk = target.read_text(encoding="utf-8", errors="replace")[:MAX_CODE_FILE_CHARS]
        except OSError:
            continue
        texts.append(chunk)
        used.append(name)
        total += len(chunk)
    return "\n".join(texts), used


def _numbers(text: str) -> set[str]:
    return {tok.rstrip(".") for tok in CLAIM_NUMBER.findall(text or "")}


def _per_run_numbers(runtime: list[dict[str, Any]]) -> list[set[str]]:
    return [_numbers(f"{item.get('stdout', '')}\n{item.get('stderr', '')}") for item in runtime]


def _derived_numbers(per_run: list[set[str]]) -> set[str]:
    """两次真实输出之间的差、和、倍数也算可核验：差值本身就是结论时不用凑原文。"""
    derived: set[str] = set()
    pools = [sorted(values, key=len)[:200] for values in per_run if values]
    for i, left in enumerate(pools):
        for right in pools[i + 1:]:
            for a in left:
                try:
                    av = float(a)
                except ValueError:
                    continue
                for b in right:
                    try:
                        bv = float(b)
                    except ValueError:
                        continue
                    for value in (av - bv, bv - av, av * 2 - bv):
                        if value and value > 0:
                            derived.add(f"{value:g}")
                            if len(derived) > MAX_DERIVED_NUMBERS:
                                return derived
    return derived


def build_index(task_root: Path, state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """返回（可持久化的索引摘要, 核对时使用的完整语料）。全部由脚本自己构建。"""
    workspace = Path(state.get("workspace") or "")
    artifact = state.get("artifact") or {}
    sha = str(artifact.get("sha") or "")
    initial = str(state.get("initialSha") or "")

    trace_facts = _trace_facts(task_root)
    diff_names: list[str] = []
    stat_text = ""
    if workspace.is_dir() and sha and initial:
        diff_names = [n for n in git(workspace, "diff", "--name-only", initial, sha, check=False).splitlines() if n]
        stat_text = git(workspace, "diff", "--shortstat", initial, sha, check=False)
    code_text, code_files = _artifact_text(workspace, sha) if workspace.is_dir() and sha else ("", [])
    prompt_path = Path(state.get("promptPath") or "")
    prompt_text = prompt_path.read_text(encoding="utf-8") if prompt_path.is_file() else ""

    runtime = runs(task_root)
    runtime_outputs = "\n".join(f"{item.get('stdout', '')}\n{item.get('stderr', '')}" for item in runtime)
    trace_outputs = "\n".join(trace_facts["outputs"])
    # 命令文本可以给"函数名、报错原文"作证，但不能给数字作证：
    # 否则 Agent 把编造的数字写进命令参数再执行一次，这个数字就"出现过"了。
    trace_text = "\n".join([trace_outputs, "\n".join(trace_facts["commands"])])

    number_source = "\n".join([runtime_outputs, trace_outputs, stat_text, prompt_text, code_text])
    known_numbers = _numbers(number_source)
    derived = _derived_numbers(_per_run_numbers(runtime))
    known_numbers |= derived

    index = {
        "builtAt": utc_now(),
        "artifactSha": sha,
        "initialSha": initial,
        "traceFiles": trace_facts["hashes"],
        "steps": trace_facts["steps"],
        "paths": sorted({p.strip().lstrip("./") for p in (list(trace_facts["paths"]) + diff_names) if p.strip()}),
        "numbers": sorted(known_numbers),
        "derivedNumbers": sorted(derived),
        "codeFiles": code_files,
        "runtime": {
            "runs": len(runtime),
            "ok": sum(1 for item in runtime if item.get("exitCode") == 0),
            "failed": sum(1 for item in runtime if item.get("exitCode") not in (0, None)),
            "labels": [item.get("label", "") for item in runtime],
        },
        "failureInTrace": bool(trace_facts["failed"]),
    }
    corpus = {
        "runtime": runtime_outputs,
        "trace": trace_text,
        "code": code_text,
        "numbers": known_numbers,
    }
    return index, corpus


# ======================================================================= 声明回查

def _mask(text: str, patterns: list[re.Pattern[str]]) -> str:
    for pattern in patterns:
        text = pattern.sub(lambda m: " " * len(m.group(0)), text)
    return text


def _path_known(claim: str, paths: set[str]) -> bool:
    claim = claim.strip().lstrip("./")
    if not claim:
        return True
    return any(p == claim or p.endswith("/" + claim) or p.endswith(claim) for p in paths)


def _contains(corpus: str, needle: str) -> bool:
    needle = needle.strip()
    return bool(needle) and needle in corpus


def check(task_root: Path, state: dict[str, Any], feedback: str, score: int) -> dict[str, Any]:
    """把 Feedback 里的可核验事实逐条回查；返回 errors/warnings/claims 与索引摘要。"""
    index, corpus = build_index(task_root, state)
    errors: list[str] = []
    warnings: list[str] = []
    claims: list[dict[str, Any]] = []

    known_paths = set(index["paths"])
    known_numbers: set[str] = set(corpus["numbers"])
    # 数字要先把文件名、反引号内容和步骤引用遮掉，否则路径里的版本号会被当成声明；
    # 路径必须从原文里找——把 PATH_CLAIM 也遮掉，这一层就永远查不到东西了。
    masked = _mask(feedback, [BACKTICK, STEP_REF, PATH_CLAIM])

    for token in sorted(set(CLAIM_NUMBER.findall(masked))):
        if token in known_numbers:
            claims.append({"kind": "number", "value": token, "ok": True})
        else:
            claims.append({"kind": "number", "value": token, "ok": False})
            errors.append(
                f"Feedback 里的数值 {token} 在轨迹、产物快照、本机运行输出和需求原文里都没有出现；"
                "换成有真实来源的数字，或改用前后变化和定性结论"
            )

    steps = int(index["steps"] or 0)
    for step in sorted({int(n) for n in STEP_REF.findall(feedback)}):
        if steps and step > steps:
            errors.append(f"Feedback 写到「第 {step} 步」，但轨迹里只有 {steps} 个工具步骤；请对准真实步骤编号")

    for claim in sorted(set(PATH_CLAIM.findall(feedback))):
        if _path_known(claim, known_paths):
            claims.append({"kind": "path", "value": claim, "ok": True})
        else:
            claims.append({"kind": "path", "value": claim, "ok": False})
            errors.append(f"Feedback 引用的文件 {claim} 在产物快照和轨迹里都不存在；只写真实改过或读过的文件")

    for raw in sorted(set(BACKTICK.findall(feedback))):
        token = raw.strip()
        if not token or not re.search(r"[A-Za-z0-9]", token):
            continue  # 纯中文引号一般是页面文案，不做代码回查
        if PATH_CLAIM.fullmatch(token):
            continue  # 已在上面按路径查过
        if IDENTIFIER.fullmatch(token) or DOTTED.fullmatch(token):
            if _contains(corpus["code"], token) or _contains(corpus["runtime"], token) or _contains(corpus["trace"], token):
                claims.append({"kind": "identifier", "value": token, "ok": True})
            else:
                claims.append({"kind": "identifier", "value": token, "ok": False})
                errors.append(
                    f"Feedback 引用的代码标识「{token}」在产物快照和轨迹里都不存在；"
                    "函数名、字段名和命令必须先在真实产物里确认"
                )
            continue
        if ERROR_LIKE.search(token):
            if _contains(corpus["runtime"], token) or _contains(corpus["trace"], token):
                claims.append({"kind": "error", "value": token, "ok": True})
            else:
                claims.append({"kind": "error", "value": token, "ok": False})
                errors.append(
                    f"Feedback 引用了原文「{token}」，但轨迹和本机运行输出里没有这段文字；"
                    "报错和页面文案必须从真实输出复制"
                )
            continue
        if _contains(corpus["runtime"], token) or _contains(corpus["trace"], token) or _contains(corpus["code"], token):
            claims.append({"kind": "text", "value": token, "ok": True})
        else:
            claims.append({"kind": "text", "value": token, "ok": False})
            warnings.append(f"Feedback 的引文「{token}」在轨迹和产物里没有直接命中，请确认来源")

    runtime_runs = runs(task_root)
    ok_runs = [item for item in runtime_runs if item.get("exitCode") == 0]
    if not runtime_runs:
        errors.append(
            "Feedback 没有对应的真实执行记录：先运行 `python3 $S verify --task-root ROOT "
            "--label '验证了什么' -- <真实命令>` 把输出固定下来，再写反馈"
        )
    elif score >= 4 and not ok_runs:
        errors.append(f"打了 {score} 分，但执行记录里没有一次成功退出；高分必须有一次真实跑通的运行证据")

    if FAILURE_CLAIM.search(feedback):
        failed_evidence = bool(index["failureInTrace"]) or any(
            item.get("exitCode") not in (0, None) for item in runtime_runs
        )
        if not failed_evidence:
            errors.append(
                "Feedback 写了报错或失败表现，但轨迹和本机执行记录里没有任何失败输出；"
                "失败结论必须来自真实复现，不能凭设计推测补写"
            )

    verified = sum(1 for item in claims if item["ok"])
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "claims": claims,
        "summary": {"claims": len(claims), "verified": verified, "unverified": len(claims) - verified},
        "index": index,
        "checkedAt": utc_now(),
    }

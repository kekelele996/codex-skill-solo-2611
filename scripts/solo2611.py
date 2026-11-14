#!/usr/bin/env python3
"""solo-2611：26.11 三领域单轮 pointwise 反馈标注的本地交付流水线。"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import string
import subprocess
import sys
import time
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import excel  # noqa: E402
import evidence  # noqa: E402
import feedback as fb  # noqa: E402
import github_repo as gh  # noqa: E402
import doctor  # noqa: E402
import recorder  # noqa: E402
import prompting  # noqa: E402
import prompt_reuse  # noqa: E402
import resume as print_resume  # noqa: E402
import capacity  # noqa: E402
import session as managed  # noqa: E402
import submission  # noqa: E402
import trace as tr  # noqa: E402
from common import (  # noqa: E402
    CONFIG_PATH, CONTAINER_TRACE_DIR, DEFAULT_BASE_URL, SKILL_ROOT, STATE_HOME, Solo2611Error, api_key, base_url,
    config_get, config_set,
    file_lock, git, git_identity_args, is_full_sha, load_config, load_state, local_date, mask_secret, non_space_len,
    out, read_json, run, safe_slug, container_slug, save_config, save_state, sha256_bytes, sha256_file, utc_now, write_json,
)
from spec import (  # noqa: E402
    DIFFICULTIES,
    DOMAINS,
    REPRO_LEVELS,
    SUBMIT_COLUMNS,
    TASK_TYPES,
    scan_banned,
    validate_classification,
)

GITIGNORE_REQUIRED = [
    ".env", ".env.*", "!.env.example", "*.pem", "*.key", "id_rsa*", "*.p12", "credentials*.json", "secrets.*",
    "node_modules/", ".pnpm-store/", "dist/", "build/", "out/", ".next/", ".nuxt/", ".output/", ".cache/",
    ".vite/", ".parcel-cache/", "coverage/", "__pycache__/", "*.pyc", ".venv/", "venv/", "target/", "*.o", "*.obj",
    ".DS_Store",
]
COPY_IGNORE = shutil.ignore_patterns("node_modules", ".git", "dist", "build", ".next", ".nuxt", ".output", ".cache",
                                     "__pycache__", ".venv", "venv", "target", ".DS_Store", "coverage")
SECRET_NAMES = (".env", "id_rsa", "id_ed25519", "credentials.json")
PROMPT_HISTORY = STATE_HOME / "prompt-history.jsonl"
CONTAINER_LOCK = capacity.CONTAINER_LOCK
LAUNCH_LOCK = STATE_HOME / "launch.lock"


def version() -> str:
    data = read_json(SKILL_ROOT / "VERSION.json", {}) or {}
    return str(data.get("version") or "0.0.0")


def emit(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def task_label(task_root: Path) -> str:
    return sha256_bytes(str(task_root.resolve()).encode())[:12]


def ws_of(state: dict[str, Any]) -> Path:
    return Path(state["workspace"])


def account_domain(config: dict[str, Any]) -> str:
    """读取账号唯一大类；缺失或非法时阻断，绝不由单题参数替代。"""
    domain = str(config_get(config, "submission.lockedDomain") or "").strip()
    if not domain:
        raise Solo2611Error(
            "配置缺少 submission.lockedDomain；账号大类必须由账号配置提供，"
            "缺失时停止并联系管理员，不能为单题自行猜测"
        )
    if domain not in DOMAINS:
        raise Solo2611Error(
            f"submission.lockedDomain「{domain}」不是合法大类；可选：{'、'.join(DOMAINS)}"
        )
    return domain


def validate_locked_domain_change(current: str, value: str) -> str:
    """账号大类只允许首次写入；之后只能由管理员在技能外调整。"""
    value = value.strip()
    if value not in DOMAINS:
        raise Solo2611Error(
            f"submission.lockedDomain 必须是账号配置中的合法大类：{'、'.join(DOMAINS)}"
        )
    if current.strip() and value != current.strip():
        raise Solo2611Error(
            "submission.lockedDomain 是账号级只读大类，不能为单题修改；"
            "需要调整账号领域时联系管理员"
        )
    return value


# ======================================================================= config

def cmd_config(args: argparse.Namespace) -> None:
    config = load_config()
    if args.action == "path":
        print(CONFIG_PATH)
    elif args.action == "show":
        emit(mask_secret(config))
    elif args.action == "key":
        print(api_key(config))
    elif args.action == "init":
        if CONFIG_PATH.exists() and not args.force:
            print(f"已存在，未覆盖：{CONFIG_PATH}")
        else:
            save_config(config)
            print(f"已写入：{CONFIG_PATH}（0600），编辑 claude.baseUrl 和 claude.apiKey 后运行 `config test`")
    elif args.action == "test":
        _config_test(config)
    elif args.action == "set":
        for pair in args.pairs:
            key, _, value = pair.partition("=")
            if not _:
                raise Solo2611Error(f"格式应为 KEY=VALUE: {pair}")
            key = key.strip()
            value = value.strip()
            if key == "submission.lockedDomain":
                current = str(config_get(config, key) or "").strip()
                value = validate_locked_domain_change(current, value)
            config_set(config, key, value)
        save_config(config)
        emit(mask_secret(config))


def _config_test(config: dict[str, Any]) -> None:
    """用 1 token 的 /v1/messages 请求确认网关地址和 Key 可用，避免 run 等 3 分钟才拿到 401。"""
    import urllib.error
    import urllib.request

    url = (base_url(config) or DEFAULT_BASE_URL) + "/v1/messages"
    body = json.dumps({"model": "auto_model/urm", "max_tokens": 1,
                       "messages": [{"role": "user", "content": "ping"}]}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "content-type": "application/json", "anthropic-version": "2023-06-01",
        "authorization": f"Bearer {api_key(config)}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            print(f"✅ {url} -> HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        hint = "Key 无效或不属于这个网关" if exc.code in (401, 403) else "网关可达但请求被拒"
        raise Solo2611Error(f"{url} -> HTTP {exc.code}（{hint}）\n{detail}")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise Solo2611Error(f"无法连接 {url}：{exc}")


# ======================================================================= init

def _ensure_gitignore(ws: Path) -> bool:
    path = ws / ".gitignore"
    existing = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    missing = [line for line in GITIGNORE_REQUIRED if line not in existing]
    if not missing:
        return False
    text = "\n".join(existing).rstrip("\n")
    block = "\n# solo-2611：凭据、依赖与构建产物不进仓库\n" + "\n".join(missing) + "\n"
    path.write_text((text + "\n" if text else "") + block, encoding="utf-8")
    return True


def _tracked_secrets(ws: Path) -> list[str]:
    files = git(ws, "ls-files").splitlines()
    hits = []
    for f in files:
        name = Path(f).name
        if name == ".env.example":
            continue
        if name.startswith(".env") or name in SECRET_NAMES or name.endswith((".pem", ".p12")) or name.endswith(".key"):
            hits.append(f)
    return hits


def cmd_init(args: argparse.Namespace) -> None:
    config = load_config()
    domain = account_domain(config)
    if args.domain and args.domain != domain:
        raise Solo2611Error(
            f"当前账号大类固定为「{domain}」，不能用 --domain {args.domain} 跨类建题"
        )
    sub_direction = args.sub_direction or config_get(config, "defaults.subDirection")
    sub_direction, classification_errors = validate_classification(domain, sub_direction)
    if classification_errors:
        raise Solo2611Error(
            classification_errors[0]
            + "；请在该账号大类下选择能真实匹配工程内容的子方向，无法匹配时拒绝建题"
        )
    tasks_dir = Path(args.tasks_dir or config["tasksDir"]).expanduser().resolve()
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    task_root = tasks_dir / f"{local_date()}-{safe_slug(args.task_name)}-{suffix}"
    if "," in str(task_root):
        raise Solo2611Error("任务路径不能包含逗号（会破坏 docker --mount 参数）")
    ws = task_root / "workspace"
    for sub in ("monitor", "delivery", "recording"):
        (task_root / sub).mkdir(parents=True, exist_ok=True)
    source: dict[str, Any] = {}
    if args.git:
        cmd = ["git", "clone", "--quiet", args.git, str(ws)]
        run(cmd, timeout=1800)
        if args.ref:
            git(ws, "checkout", "--quiet", args.ref)
        source = {"kind": "git", "url": args.git, "ref": args.ref or ""}
    elif args.source:
        src = Path(args.source).expanduser().resolve()
        if not src.is_dir():
            raise Solo2611Error(f"源码目录不存在: {src}")
        if (src / ".git").exists():
            if git(src, "status", "--porcelain"):
                print(f"⚠️ {src} 有未提交改动，git clone 只会带走已提交内容", file=sys.stderr)
            run(["git", "clone", "--quiet", "--no-hardlinks", str(src), str(ws)], timeout=1800)
            source = {"kind": "git-dir", "path": str(src), "head": git(src, "rev-parse", "HEAD")}
        else:
            shutil.copytree(src, ws, ignore=COPY_IGNORE)
            run(["git", "init", "--quiet", "-b", "main", str(ws)])
            source = {"kind": "dir", "path": str(src)}
    else:
        raise Solo2611Error("需要 --source DIR 或 --git URL")
    if git(ws, "remote", check=False) == "origin":
        git(ws, "remote", "rename", "origin", "upstream")
    changed = _ensure_gitignore(ws)
    git(ws, "add", "-A")
    if changed or source["kind"] == "dir":
        run(["git", "-C", str(ws), *git_identity_args(config), "commit", "--quiet", "-m", "chore: 初始环境快照"])
    secrets = _tracked_secrets(ws)
    if secrets:
        raise Solo2611Error(f"仓库里跟踪了疑似凭据文件，先删掉并重新 init：{secrets}")
    initial = git(ws, "rev-parse", "HEAD")
    state = {
        "schema": "solo-2611/1",
        "skillVersion": version(),
        "taskName": args.task_name,
        "taskRoot": str(task_root),
        "workspace": str(ws),
        "createdAt": utc_now(),
        "createdDate": local_date(),
        "status": "prepared",
        "source": source,
        "initialSha": initial,
        "fields": {
            "所属领域": domain,
            "子方向": sub_direction,
            "Harness": config_get(config, "defaults.harness"),
            "操作系统": config_get(config, "defaults.os"),
        },
        "attempts": [],
    }
    save_state(task_root, state)
    emit({"taskRoot": str(task_root), "workspace": str(ws), "initialSha": initial, "next": "prompt"})


# ======================================================================= prompt

def _local_history() -> list[dict[str, Any]]:
    if not PROMPT_HISTORY.is_file():
        return []
    rows = []
    for line in PROMPT_HISTORY.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _merge_history(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for rows in groups:
        for row in rows:
            text = str(row.get("text") or "").strip()
            if not text:
                continue
            key = str(row.get("sha256") or prompting.normalize(text))
            merged[key] = {**merged.get(key, {}), **row, "text": text}
    return list(merged.values())


def _load_history(
    task_root: Path,
    *,
    candidate_text: str = "",
    local_only: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    local = [
        {**row, "source": str(row.get("source") or "local")}
        for row in _local_history()
    ]
    meta: dict[str, Any] = {
        "ok": True,
        "source": "local",
        "managed": bool(prompt_reuse.read_task_marker(task_root)),
        "required": False,
        "error": "",
        "status": "history",
        "summary": {},
    }
    if local_only:
        return local, meta
    try:
        response = prompt_reuse.query_prompt_history(
            task_root,
            candidate_text=candidate_text,
            limit=500,
        )
    except prompt_reuse.WorkbenchPromptHistoryError as exc:
        meta.update(
            {
                "ok": False,
                "source": "local",
                "managed": exc.managed,
                "required": exc.managed,
                "error": str(exc),
                "code": exc.code,
            }
        )
        return local, meta
    remote = prompt_reuse.history_records(response)
    meta.update(
        {
            "ok": True,
            "source": "workbench+local",
            "managed": True,
            "required": True,
            "projectId": str(response.get("projectId") or ""),
            "projectName": str(response.get("projectName") or ""),
            "engineeringId": str(response.get("engineeringId") or ""),
            "engineeringName": str(response.get("engineeringName") or ""),
            "status": str(response.get("status") or "history"),
            "blocking": bool(response.get("blocking")),
            "summary": response.get("summary") or {},
        }
    )
    if int(response.get("total") or 0) > len(remote):
        meta.update(
            {
                "ok": False,
                "required": True,
                "error": "同工程历史超过单次查询上限 500 条，拒绝在截断历史上查重",
            }
        )
    return _merge_history(local, remote), meta


def cmd_prompt_history(args: argparse.Namespace) -> None:
    task_root = Path(args.task_root).resolve()
    history, meta = _load_history(
        task_root,
        candidate_text=str(args.text or ""),
        local_only=bool(args.local_only),
    )
    engineering_history = [item for item in history if item.get("source") == "workbench"]
    output_items = engineering_history or history
    emit(
        {
            "ok": bool(meta.get("ok")) or not bool(meta.get("required")),
            "taskRoot": str(task_root),
            "historyMeta": meta,
            "count": len(output_items),
            "localHistoryCount": len(history) - len(engineering_history),
            "items": output_items,
        }
    )
    if meta.get("required") and not meta.get("ok"):
        raise SystemExit(1)


def cmd_prompt(args: argparse.Namespace) -> None:
    task_root = Path(args.task_root).resolve()
    state = load_state(task_root)
    templates = prompting.ensure_templates(task_root, force=bool(args.init and args.force))
    if args.init:
        draft = task_root / "delivery" / "题面草稿.md"
        if args.force or not draft.exists():
            draft.write_text(
                "把真实项目里的业务场景、现有流程、两条互相牵制的约束，以及实现错误时会出现的可见结果，"
                "写成一段自然的需求说明。不要写文件路径、端口、技术栈或逐步修复指令。\n",
                encoding="utf-8",
            )
        history, history_meta = _load_history(task_root)
        engineering_history = [item for item in history if item.get("source") == "workbench"]
        emit({"ok": True, "draft": str(draft), "templates": templates,
              "historyMeta": history_meta,
              "history": engineering_history or history,
              "localHistoryCount": len(history) - len(engineering_history),
              "next": "先排除 history 中同一工程的重复题面，再填写两份审核 JSON 并运行 prompt --file"})
        return
    if state.get("attempts") and not args.force:
        raise Solo2611Error("这道题已经跑过，提示词不能再改；换题请重新 init")
    if not args.file:
        raise Solo2611Error("prompt 需要 --file；第一次使用可先运行 `prompt --init`")
    missing_args = [
        flag for flag, value in (
            ("--task-type", args.task_type),
            ("--difficulty", args.difficulty),
            ("--lang", args.lang),
        )
        if not value
    ]
    if missing_args:
        raise Solo2611Error("prompt 缺少参数: " + " ".join(missing_args))
    text = Path(args.file).read_text(encoding="utf-8")
    if not text.strip():
        raise Solo2611Error("提示词为空")
    fields = state["fields"]
    locked_domain = account_domain(load_config())
    stored_domain = str(fields.get("所属领域") or "").strip()
    domain = locked_domain
    if args.domain and args.domain != locked_domain:
        errors = [
            f"平台账号大类固定为「{locked_domain}」，不能用 --domain {args.domain} 跨类"
        ]
    elif stored_domain != locked_domain:
        errors = [
            f"任务初始化大类「{stored_domain or '空'}」与账号固定大类「{locked_domain}」不一致；"
            "不要直接改标签，应重新归类到该大类下的真实子方向"
        ]
    else:
        errors = []
    sub = args.sub_direction or fields["子方向"]
    sub, classification_errors = validate_classification(domain, sub)
    warnings: list[str] = []
    errors.extend(classification_errors)
    if args.task_type not in TASK_TYPES:
        errors.append(f"任务类型必须是 {TASK_TYPES}")
    if args.difficulty not in DIFFICULTIES:
        errors.append("本期首轮只收「困难」「地狱」")
    if args.repro and args.repro not in REPRO_LEVELS:
        errors.append(f"环境可复现等级必须是 {REPRO_LEVELS}")
    blocks, warns = scan_banned(text, domain)
    if blocks:
        errors.append(f"命中「不被允许出的雷同题」：{blocks}")
    if warns:
        warnings.append(f"含敏感词 {warns}，确认不是雷同题或过简的数据处理演示")
    if non_space_len(text) < 80:
        warnings.append("提示词不足 80 字，容易被判为过于简单（单文件局部修改、无需 repo 上下文）")
    review_path = Path(args.review).expanduser().resolve() if args.review else Path(templates["raRenhuaReview"])
    difficulty_review_path = (
        Path(args.difficulty_review).expanduser().resolve()
        if args.difficulty_review
        else Path(templates["difficultyReview"])
    )
    history, history_meta = _load_history(task_root, candidate_text=text)
    if history_meta.get("required") and not history_meta.get("ok"):
        errors.append(
            f"工作台提示词历史查询失败，不能在去重不完整时安装题面：{history_meta.get('error')}"
        )
    design = prompting.validate_candidate(
        task_root=task_root,
        prompt_path=Path(args.file).resolve(),
        prompt_text=text,
        difficulty=args.difficulty,
        review_path=review_path,
        difficulty_review_path=difficulty_review_path,
        history=history,
        allow_over_170=args.allow_over_170,
        domain=domain,
    )
    errors.extend(design.get("errors") or [])
    warnings.extend(design.get("warnings") or [])
    sha = design["fileSha256"]
    if errors:
        emit({"ok": False, "errors": errors, "warnings": warnings})
        raise SystemExit(2)
    prompt_path = task_root / "delivery" / "提示词.md"
    prompt_path.write_bytes(text.encode("utf-8"))
    fields.update({"所属领域": domain, "子方向": sub, "任务类型": args.task_type, "任务难度": args.difficulty,
                   "语言/框架": args.lang, "环境可复现等级": args.repro or ""})
    state.update({
        "promptPath": str(prompt_path),
        "promptSha256": sha,
        "promptReview": str(review_path),
        "difficultyReview": str(difficulty_review_path),
        "difficultySignalsPassed": (design.get("difficultyReview") or {}).get("signalsPassed"),
        "status": "prompt_ready",
    })
    save_state(task_root, state)
    design["historyMeta"] = history_meta
    prompting.write_validation(task_root, design)
    PROMPT_HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with PROMPT_HISTORY.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"taskRoot": str(task_root), "sha256": sha, "date": local_date(), "text": text},
                            ensure_ascii=False) + "\n")
    emit({
        "ok": True,
        "promptPath": str(prompt_path),
        "sha256": sha,
        "warnings": warnings,
        "difficultySignalsPassed": state["difficultySignalsPassed"],
        "p4Contract": design.get("p4Contract") or {},
        "next": "repo",
    })


# ======================================================================= run

def _ensure_image(image: str) -> None:
    if run(["docker", "image", "inspect", image], check=False).returncode != 0:
        print(f"拉取镜像 {image} …", file=sys.stderr)
        run(["docker", "pull", image], timeout=1800)


def _running_solo_containers() -> list[str]:
    return out(run(["docker", "ps", "--filter", "label=solo2611=true", "--format", "{{.Names}}"], check=False)).split()


def _container_running(name: str) -> bool:
    return name in out(run(["docker", "ps", "--filter", f"name=^{name}$", "--format", "{{.Names}}"], check=False)).split()


def _check_workspace_pristine(state: dict[str, Any]) -> None:
    ws = ws_of(state)
    head = git(ws, "rev-parse", "HEAD")
    if head != state["initialSha"]:
        raise Solo2611Error(f"workspace HEAD {head[:12]} 不是初始快照 {state['initialSha'][:12]}，先 `reset --yes`")
    dirty = git(ws, "status", "--porcelain")
    if dirty:
        raise Solo2611Error(f"workspace 不干净，初始环境与快照不一致：\n{dirty[:800]}\n先 `reset --yes`")


def _require_repo(state: dict[str, Any], skip: bool) -> None:
    if skip or (state.get("remote") or {}).get("initialPushed"):
        return
    raise Solo2611Error("规范要求首轮前把初始快照 push 到远端：先运行 `repo --task-root ROOT`（只想本地试跑加 --skip-repo）")


def _reset_workspace(state: dict[str, Any]) -> None:
    ws = ws_of(state)
    git(ws, "reset", "--hard", "--quiet", state["initialSha"])
    git(ws, "clean", "-fdxq")


def _docker_base(name: str, task_root: Path, ws: Path, image: str, interactive: bool) -> list[str]:
    return [
        "docker", "run", "-it" if interactive else "-i", "--init", "--restart=no",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--name", name, "--label", "solo2611=true", "--label", f"solo2611.task={task_label(task_root)}",
        "--mount", f"type=bind,src={ws},dst=/workspace", "-e", "apikey", "-e", "ANTHROPIC_BASE_URL", image,
    ]


def _wait_launch_spacing(config: dict[str, Any]) -> None:
    """Serialize container starts across agents using a monotonic wall stamp."""
    spacing = max(5, int(config_get(config, "claude.launchSpacingSeconds") or 20))
    with file_lock(LAUNCH_LOCK):
        try:
            previous = read_json(LAUNCH_LOCK, {}) or {}
        except (OSError, json.JSONDecodeError):
            previous = {}
        last_at = 0.0
        try:
            last_at = float(previous.get("lastLaunchedEpoch") or 0)
        except (TypeError, ValueError):
            last_at = 0.0
        remaining = max(0.0, spacing - (time.time() - last_at))
        if remaining:
            print(f"启动间隔保护：等待 {int(remaining + 0.999)} 秒", file=sys.stderr)
            time.sleep(remaining)
        write_json(
            LAUNCH_LOCK,
            {
                "schema": "solo2611/launch-lock/1",
                "lastLaunchedEpoch": time.time(),
                "lastLaunchedAt": utc_now(),
                "spacingSeconds": spacing,
            },
            mode=0o600,
        )


def _opportunity_consumed(state: dict[str, Any]) -> bool:
    if state.get("session"):
        return True
    run_state = state.get("sessionRun") or {}
    if run_state.get("status") in managed.TERMINAL:
        return True
    return any(
        attempt.get("status") in {"running", "rejected", "cancelled", "aborted", "lost"}
        for attempt in state.get("attempts") or []
        if isinstance(attempt, dict)
    )


def _collect_container(task_root: Path, state: dict[str, Any], name: str, attempt_dir: Path,
                       result: dict[str, Any] | None = None) -> dict[str, Any]:
    traces = attempt_dir / "traces"
    shutil.rmtree(traces, ignore_errors=True)
    proc = run(["docker", "cp", f"{name}:{CONTAINER_TRACE_DIR}", str(traces)], check=False, timeout=300)
    if proc.returncode != 0:
        raise Solo2611Error(f"导出轨迹失败，容器 {name} 已保留，不要删除：{proc.stderr.decode(errors='replace')}")
    removed = run(["docker", "rm", name], check=False, timeout=60)
    capacity.release_container(name)
    if removed.returncode != 0:
        inspect = run(["docker", "inspect", name], check=False, timeout=20)
        if inspect.returncode == 0:
            raise Solo2611Error(
                f"轨迹已导出，但容器 {name} 删除失败，禁止标记为接受："
                f"{removed.stderr.decode(errors='replace').strip()}"
            )
    files = tr.main_trace_files(traces)
    prompt_text = Path(state["promptPath"]).read_text(encoding="utf-8")
    if len(files) != 1:
        return {"ok": False, "errors": [f"主会话轨迹文件数量为 {len(files)}，必须恰好 1 个"], "traces": str(traces)}
    info = tr.analyze(files[0], prompt_text)
    info["traces"] = str(traces)
    # 结束标志只说明进程退出了。再看最后在说什么：还在问用户、还在许诺下一步、
    # 工具调用没拿到结果、上下文耗尽或被截断，都不算真的做完这一轮。
    review = tr.completion_review(files[0], info, result or {})
    info["completion"] = review
    info["ok"] = bool(info.get("ok")) and review["ok"]
    info["errors"] = list(info.get("errors") or []) + review["errors"]
    info["warnings"] = list(info.get("warnings") or []) + review["warnings"]
    return info


def _accept_attempt(task_root: Path, state: dict[str, Any], attempt: dict[str, Any], info: dict[str, Any]) -> None:
    final_traces = task_root / "traces"
    shutil.rmtree(final_traces, ignore_errors=True)
    shutil.copytree(info["traces"], final_traces)
    main = tr.main_trace_files(final_traces)[0]
    attempt["status"] = "accepted"
    state["session"] = {
        "sessionId": info["sessionId"], "promptId": info["promptId"], "harnessVersion": info["harnessVersion"],
        "entrypoint": info["entrypoint"], "models": info["models"], "trace": str(main), "tracesDir": str(final_traces),
        "subagentFiles": len(info["subagentFiles"]), "toolUses": info["toolUses"], "warnings": info["warnings"],
        "mode": attempt["mode"],
    }
    state["status"] = "ran"
    save_state(task_root, state)
    _snapshot(task_root, state)
    _digest(task_root, state)
    _auto_push(task_root, load_state(task_root))


def _auto_push(task_root: Path, state: dict[str, Any]) -> None:
    """已建仓的题在产物快照后立即推送；失败不影响本地结果，之后可以 `repo` 补推。"""
    config = load_config()
    if not (state.get("remote") or {}).get("fullName") or not config_get(config, "github.autoPush"):
        return
    try:
        remote = gh.ensure_repo(task_root, state, config)
        print(f"产物快照已推送：{gh.permalink(load_state(task_root), remote['artifactSha'])}")
    except Solo2611Error as exc:
        print(f"⚠️ 产物快照推送失败，稍后运行 `repo --task-root {task_root}` 补推：{exc}", file=sys.stderr)


def cmd_run(args: argparse.Namespace) -> None:
    task_root = Path(args.task_root).resolve()
    state = load_state(task_root)
    config = load_config()
    if not state.get("promptPath"):
        raise Solo2611Error("先运行 prompt")
    if state.get("session") or (state.get("sessionRun") or {}).get("status") == "accepted":
        raise Solo2611Error("这道题已有被接受的会话；一题只有一次会话机会")
    if (state.get("sessionRun") or {}).get("status") == "aborted" or state.get("status") == "session_aborted":
        raise Solo2611Error("这道题已终止；终止是终态，只能换题")
    if _opportunity_consumed(state) and (state.get("sessionRun") or {}).get("status") != "start_failed":
        raise Solo2611Error("这道题已经用过会话机会；v4 不支持整题重跑")
    if sha256_file(Path(state["promptPath"])) != state["promptSha256"]:
        raise Solo2611Error("delivery/提示词.md 在 prompt 之后被改动过")
    _require_repo(state, args.skip_repo)
    _check_workspace_pristine(state)
    image = str(config_get(config, "claude.image"))
    _ensure_image(image)
    mode = str(args.mode or config_get(config, "claude.runMode") or "supervised")
    if mode not in {"supervised", "print"}:
        raise Solo2611Error("--mode 只能是 supervised 或 print")
    if mode == "supervised":
        managed.assert_interactive_prereqs()
        ws = ws_of(state)
        os.chmod(ws, 0o777)
        # 先占容器名额再开始计时：等名额的时间不算进会话总时限，也不消耗会话机会。
        holder = capacity.holder_key(task_root)
        capacity.acquire(
            holder,
            limit=int(config_get(config, "claude.maxContainers") or 4),
            task_root=task_root,
            wait_seconds=float(config_get(config, "claude.slotWaitSeconds") or capacity.DEFAULT_WAIT_SECONDS),
        )
        attempt = managed.prepare_attempt(task_root, state, image)
        save_state(task_root, state)
        _wait_launch_spacing(config)
        try:
            result = managed.start_supervised(task_root, load_state(task_root), int(attempt["index"]))
        except Exception as exc:
            failed_state = load_state(task_root)
            failed_run = failed_state.setdefault("sessionRun", {})
            failed_container = str(failed_run.get("container") or attempt.get("container") or "")
            failed_tmux = str(failed_run.get("tmuxSession") or "")
            if failed_container or failed_tmux:
                with suppress(Exception):
                    managed._remove_tmux_and_container(failed_container, failed_tmux)
            # A launched container may already be running even if bookkeeping
            # failed. `release` refuses to give its slot away in that case.
            capacity.release(holder)
            failed_run["status"] = "start_failed"
            failed_run["error"] = f"{type(exc).__name__}: {exc}"
            failed_state["status"] = "prompt_ready"
            current = next(
                (
                    item
                    for item in failed_state.get("attempts") or []
                    if int(item.get("index") or 0) == int(attempt["index"])
                ),
                None,
            )
            if current is not None:
                current["status"] = "start_failed"
                current["startFailed"] = True
            save_state(task_root, failed_state)
            raise Solo2611Error(f"受管会话启动失败，不消耗会话机会：{exc}") from exc
        emit(result)
        if not args.follow:
            return
        while True:
            status = managed.status_view(task_root)
            emit(status)
            if status["status"] in managed.TERMINAL or status["status"] == "session_lost":
                if status["status"] != "accepted":
                    raise SystemExit(2)
                return
            time.sleep(5)

    # print is the conservative fallback: one container, one prompt, no continues.
    key = api_key(config)
    timeout = float(args.timeout or config_get(config, "claude.timeoutSeconds") or 10800)
    silent = float(config_get(config, "claude.silentSeconds") or 1200)
    limit = int(config_get(config, "claude.maxContainers") or 4)
    env = os.environ.copy()
    env["apikey"] = key
    env["ANTHROPIC_BASE_URL"] = base_url(config) or DEFAULT_BASE_URL
    ws = ws_of(state)
    os.chmod(ws, 0o777)
    idx = len(state["attempts"]) + 1
    attempt_dir = task_root / "monitor" / "runs" / f"attempt-{idx:02d}"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    name = f"solo2611-{container_slug(state['taskName'])}-{idx:02d}-{random.randrange(16**4):04x}".lower()
    attempt = {
        "index": idx,
        "container": name,
        "mode": "print",
        "image": image,
        "startedAt": utc_now(),
        "status": "running",
        "dir": str(attempt_dir),
    }
    state["attempts"].append(attempt)
    state["status"] = "running"
    state["sessionRun"] = {
        "schema": "solo2611/session-run/1",
        "mode": "print",
        "attemptIndex": idx,
        "container": name,
        "status": "running",
        "startedAt": utc_now(),
        "continues": {"used": 0, "max": 0, "history": []},
        "rounds": [],
        "interrupts": [],
        "abort": None,
        "outcome": None,
    }
    save_state(task_root, state)
    cmd = _docker_base(name, task_root, ws, image, interactive=False) + ["print"]
    capacity.acquire(
        capacity.holder_key(task_root),
        limit=limit,
        container=name,
        task_root=task_root,
        wait_seconds=float(config_get(config, "claude.slotWaitSeconds") or capacity.DEFAULT_WAIT_SECONDS),
    )
    _wait_launch_spacing(config)
    stdin = open(state["promptPath"], "rb")
    stdout = open(attempt_dir / "stdout.jsonl", "wb")
    stderr = open(attempt_dir / "stderr.log", "wb")
    proc = subprocess.Popen(cmd, stdin=stdin, stdout=stdout, stderr=stderr, env=env, start_new_session=True)
    attempt["pid"] = proc.pid
    save_state(task_root, state)
    for _ in range(60):
        if _container_running(name) or proc.poll() is not None:
            break
        time.sleep(0.5)
    capacity.settle()
    print(f"[attempt {idx}] 容器 {name} 运行中（总时限 {int(timeout)} 秒，静默时限 {int(silent)} 秒）", file=sys.stderr)
    started = time.monotonic()
    last_size = 0
    last_progress = started
    detached = False
    try:
        while proc.poll() is None:
            current_size = (attempt_dir / "stdout.jsonl").stat().st_size
            if current_size != last_size:
                last_size = current_size
                last_progress = time.monotonic()
            if time.monotonic() - last_progress >= silent:
                attempt["timeout"] = True
                attempt["timeoutKind"] = "silent"
                run(["docker", "stop", "-t", "10", name], check=False, timeout=60)
                break
            if time.monotonic() - started >= timeout:
                attempt["timeout"] = True
                attempt["timeoutKind"] = "total"
                run(["docker", "stop", "-t", "10", name], check=False, timeout=60)
                break
            time.sleep(5)
        proc.wait(timeout=120)
    except KeyboardInterrupt:
        if not args.hard_stop:
            detached = True
            attempt["detachedAt"] = utc_now()
            state["status"] = "running"
            save_state(task_root, state)
    finally:
        stdin.close()
        stdout.close()
        stderr.close()
    if detached:
        raise Solo2611Error(
            "已脱离，容器和运行未终止；稍后执行 "
            f"`python3 {Path(__file__).resolve()} resume --task-root {task_root}` 接回"
        )
    if args.hard_stop and proc.poll() is None:
        run(["docker", "stop", "-t", "10", name], check=False, timeout=60)
        _reset_workspace(state)
        state["status"] = "prompt_ready"
        save_state(task_root, state)
        raise Solo2611Error("--hard-stop 已立即终止容器并恢复工作区")
    attempt["exitCode"] = proc.returncode
    attempt["finishedAt"] = utc_now()
    result = tr.stdout_result(attempt_dir / "stdout.jsonl")
    attempt["result"] = result
    info = _collect_container(task_root, state, name, attempt_dir, result)
    attempt["completion"] = info.get("completion") or {}
    if result.get("is_error"):
        info["ok"] = False
        info.setdefault("errors", []).append(f"print 模式返回错误: {result}")
    if attempt.get("timeout"):
        info["ok"] = False
        info.setdefault("errors", []).append(
            "超过静默时限" if attempt.get("timeoutKind") == "silent" else "超过总时长上限"
        )
    attempt["errors"] = info.get("errors", [])
    if info.get("ok"):
        _accept_attempt(task_root, state, attempt, info)
        state = load_state(task_root)
        state["sessionRun"]["status"] = "accepted"
        state["sessionRun"]["outcome"] = {"acceptedAt": utc_now(), "remark": ""}
        save_state(task_root, state)
        emit({"ok": True, "attempt": idx, "sessionId": info["sessionId"], "promptId": info["promptId"],
              "artifactSha": state["artifact"]["sha"], "diffStat": state["artifact"]["shortStat"],
              "timeline": str(task_root / "delivery" / "轨迹时间线.md"), "next": "record"})
        return
    attempt["status"] = "rejected"
    state = load_state(task_root)
    state["status"] = "session_aborted"
    state["sessionRun"]["status"] = "aborted"
    state["sessionRun"]["abort"] = {
        "reason": "print_validation_failed",
        "detail": "；".join(info.get("errors") or []),
        "at": utc_now(),
        "by": "auto",
    }
    save_state(task_root, state)
    raise Solo2611Error(
        "print 会话不可用；v4 不重跑。错误：" + "；".join(info.get("errors") or ["结构校验失败"])
    )


def cmd_interactive(args: argparse.Namespace) -> None:
    task_root = Path(args.task_root).resolve()
    state = load_state(task_root)
    config = load_config()
    if not state.get("promptPath"):
        raise Solo2611Error("先运行 prompt")
    if state.get("session"):
        raise Solo2611Error("这道题已有被接受的会话")
    _require_repo(state, args.skip_repo)
    _check_workspace_pristine(state)
    image = str(config_get(config, "claude.image"))
    _ensure_image(image)
    ws = ws_of(state)
    os.chmod(ws, 0o777)
    idx = len(state["attempts"]) + 1
    name = f"solo2611-{container_slug(state['taskName'])}-{idx:02d}-{random.randrange(16**4):04x}".lower()
    attempt = {"index": idx, "container": name, "mode": "interactive", "image": image, "startedAt": utc_now(),
               "status": "pending_collect", "dir": str(task_root / "monitor" / "runs" / f"attempt-{idx:02d}")}
    state["attempts"].append(attempt)
    state["pendingContainer"] = name
    state["status"] = "running"
    save_state(task_root, state)
    cmd = _docker_base(name, task_root, ws, image, interactive=True)
    script = Path(__file__).resolve()
    url = base_url(config) or DEFAULT_BASE_URL
    line = f'apikey="$(python3 {script} config key)" ANTHROPIC_BASE_URL="{url}" ' + " ".join(f"'{c}'" if (" " in c or "," in c) else c for c in cmd)
    if shutil.which("pbcopy"):
        subprocess.run(["pbcopy"], input=Path(state["promptPath"]).read_bytes(), check=False)
    print("\n".join([
        "在 Mac 终端执行下面整行（Key 由配置读取，不会写进命令历史）：", "", line, "",
        f"提示词已复制到剪贴板（原文：{state['promptPath']}），出现输入框后粘贴发送；手工模式不支持自动继续。",
        "模型回答结束后按两次 Ctrl+D 回到终端，然后执行：", "",
        f"python3 {script} collect --task-root {task_root}",
    ]))


def cmd_collect(args: argparse.Namespace) -> None:
    task_root = Path(args.task_root).resolve()
    state = load_state(task_root)
    name = args.container or state.get("pendingContainer")
    if not name:
        raise Solo2611Error("没有待收集的容器")
    if _container_running(name):
        raise Solo2611Error(f"容器 {name} 仍在运行，先在对话里按两次 Ctrl+D 退出")
    attempt = next(a for a in state["attempts"] if a["container"] == name)
    attempt_dir = Path(attempt["dir"])
    attempt_dir.mkdir(parents=True, exist_ok=True)
    info = _collect_container(task_root, state, name, attempt_dir)
    attempt["finishedAt"] = utc_now()
    attempt["completion"] = info.get("completion") or {}
    attempt["errors"] = info.get("errors", [])
    state.pop("pendingContainer", None)
    if info.get("ok"):
        _accept_attempt(task_root, state, attempt, info)
        emit({"ok": True, "sessionId": info["sessionId"], "promptId": info["promptId"],
              "artifactSha": state["artifact"]["sha"], "next": "record"})
        return
    attempt["status"] = "rejected"
    state["status"] = "prompt_ready"
    save_state(task_root, state)
    emit({"ok": False, "errors": info.get("errors"), "hint": "该会话不可用；v4 不支持整题重跑，请换题"})
    raise SystemExit(2)


def cmd_recover_round(args: argparse.Namespace) -> None:
    """Audit a clean end_turn boundary and discard later false-continue data."""
    task_root = Path(args.task_root).resolve()
    emit(
        managed.recover_round_boundary(
            task_root,
            round_no=int(args.round),
            reason=args.reason,
            execute=bool(args.execute),
        )
    )


def cmd_supervise(args: argparse.Namespace) -> None:
    task_root = Path(args.task_root).resolve()
    managed.supervisor_loop(task_root, int(args.attempt), attach=bool(args.attach))


def cmd_run_status(args: argparse.Namespace) -> None:
    task_root = Path(args.task_root).resolve()
    status = managed.status_view(task_root, log_lines=max(1, int(args.lines or 20)))
    if args.json:
        emit(status)
        return
    next_action = status.get("nextActionAt") or "无"
    active = "在线" if status.get("supervisorAlive") else "离线"
    print(
        f"状态：{status['status']}  第 {status['round']} 轮  "
        f"继续 {status['continues']['used']}/{status['continues']['max']}\n"
        f"最近活动：{status.get('lastActivityAt') or '未知'}  下次动作：{next_action}\n"
        f"剩余时间：{status.get('remainingSeconds', 0)} 秒  监督器：{active}\n"
        f"容器：{status.get('container') or '无'}  SessionID：{status.get('sessionId') or '无'}"
    )
    if status.get("blocked"):
        print(f"等待人工处理：{status['blocked'].get('kind')}，截止 {status['blocked'].get('until')}")
    for event in status.get("logTail") or []:
        print(f"{event.get('at', '')} {event.get('event', '')}")


def cmd_resume(args: argparse.Namespace) -> None:
    task_root = Path(args.task_root).resolve()
    result = managed.resume_session(task_root)
    emit(result)
    if not args.follow:
        if not result.get("ok"):
            raise SystemExit(2)
        return
    while True:
        status = managed.status_view(task_root)
        emit(status)
        if status.get("status") in managed.TERMINAL or status.get("status") == "session_lost":
            if status.get("status") != "accepted":
                raise SystemExit(2)
            return
        time.sleep(5)


def cmd_continue(args: argparse.Namespace) -> None:
    result = managed.human_continue(Path(args.task_root).resolve(), note=args.note or "")
    emit(result)
    if not result.get("ok"):
        raise SystemExit(2)


def cmd_abort(args: argparse.Namespace) -> None:
    result = managed.abort(
        Path(args.task_root).resolve(),
        args.reason,
        by=args.by,
        detail="人工执行 abort",
    )
    emit(result)
    if not result.get("ok"):
        raise SystemExit(2)


def cmd_resume_all(args: argparse.Namespace) -> None:
    config = load_config()
    tasks_dir = Path(args.tasks_dir or config["tasksDir"]).expanduser().resolve()
    result = managed.supervise_all(tasks_dir, dry_run=bool(args.dry_run))
    emit(result)
    if not result.get("ok"):
        raise SystemExit(2)


def cmd_doctor(args: argparse.Namespace) -> None:
    config = load_config()
    tasks_dir = Path(args.tasks_dir or config["tasksDir"]).expanduser().resolve()
    if args.action == "baseline":
        output = (
            Path(args.out).expanduser().resolve()
            if args.out
            else tasks_dir / "monitor" / "baseline.json"
        )
        emit(doctor.write_baseline(tasks_dir, output))
        return
    if args.action == "fix-stale" or args.fix_stale:
        emit(doctor.fix_stale(tasks_dir))
        return
    raise Solo2611Error("doctor 只支持 baseline 或 --fix-stale")


def cmd_reset(args: argparse.Namespace) -> None:
    task_root = Path(args.task_root).resolve()
    state = load_state(task_root)
    if state.get("session") or (state.get("sessionRun") or {}).get("status") in {"accepted", "aborted"}:
        raise Solo2611Error("已接受或已终止的会话是终态，reset 不能改变它；请换题")
    if not args.yes:
        raise Solo2611Error("reset 会丢弃 workspace 里初始快照之后的全部改动，确认后加 --yes")
    _reset_workspace(state)
    state.pop("session", None)
    state.pop("artifact", None)
    state["status"] = "prompt_ready" if state.get("promptPath") else "prepared"
    save_state(task_root, state)
    emit({"ok": True, "workspace": state["workspace"], "head": state["initialSha"]})


# ======================================================================= snapshot / digest

def _snapshot(task_root: Path, state: dict[str, Any]) -> None:
    config = load_config()
    ws = ws_of(state)
    initial = state["initialSha"]
    if run(["git", "-C", str(ws), "merge-base", "--is-ancestor", initial, "HEAD"], check=False).returncode != 0:
        raise Solo2611Error("初始快照不是当前 HEAD 的祖先（模型改写了历史），这条数据无法追溯")
    model_commits = git(ws, "rev-list", "--reverse", f"{initial}..HEAD").split()
    if git(ws, "status", "--porcelain"):
        git(ws, "add", "-A")
        sid = (state.get("session") or {}).get("sessionId", "")
        run(["git", "-C", str(ws), *git_identity_args(config), "commit", "--quiet", "-m", f"chore: 产物快照 session {sid}"])
    sha = git(ws, "rev-parse", "HEAD")
    big = []
    for line in git(ws, "diff", "--numstat", initial, sha).splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0] == "-" and (ws / parts[2]).is_file() and (ws / parts[2]).stat().st_size > 5 << 20:
            big.append(parts[2])
    state["artifact"] = {
        "sha": sha, "modelCommits": model_commits, "shortStat": git(ws, "diff", "--shortstat", initial, sha),
        "stat": git(ws, "diff", "--stat=160", initial, sha), "largeBinaries": big, "createdAt": utc_now(),
    }
    if sha == initial:
        state["artifact"]["warning"] = "模型没有留下任何文件改动，产物快照等于初始快照"
    state["status"] = "snapshotted"
    save_state(task_root, state)


def _digest(task_root: Path, state: dict[str, Any]) -> Path:
    session = state.get("session") or {}
    main = Path(session["trace"])
    info = tr.analyze(main, Path(state["promptPath"]).read_text(encoding="utf-8"))
    md = tr.timeline_markdown(main, info, (state.get("artifact") or {}).get("stat", ""))
    path = task_root / "delivery" / "轨迹时间线.md"
    path.write_text(md, encoding="utf-8")
    return path


def cmd_snapshot(args: argparse.Namespace) -> None:
    task_root = Path(args.task_root).resolve()
    state = load_state(task_root)
    if not state.get("session"):
        raise Solo2611Error("还没有被接受的会话")
    _snapshot(task_root, state)
    emit(state["artifact"])


BUNDLE_SKIP_DIFF = ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", ".min.js", ".map")


def cmd_bundle(args: argparse.Namespace) -> None:
    """把题面、会话信息、快照、轨迹时间线和完整 diff 汇成一份原始材料，不做任何评价。"""
    task_root = Path(args.task_root).resolve()
    state = load_state(task_root)
    session = state.get("session") or {}
    artifact = state.get("artifact") or {}
    if not session or not artifact.get("sha"):
        raise Solo2611Error("还没有被接受的会话和产物快照")
    ws, initial, sha = ws_of(state), state["initialSha"], artifact["sha"]
    fields = state.get("fields") or {}
    timeline = task_root / "delivery" / "轨迹时间线.md"
    if not timeline.is_file():
        _digest(task_root, state)
    names = git(ws, "diff", "--name-only", initial, sha).splitlines()
    skipped = [n for n in names if n.endswith(BUNDLE_SKIP_DIFF)]
    pathspec = [n for n in names if n not in skipped]
    diff = git(ws, "diff", "--no-color", "--find-renames", initial, sha, "--", *pathspec) if pathspec else ""
    lines = [
        "# 产物材料", "",
        "> 由 `solo2611.py bundle` 从任务目录原样汇总：题面、会话、快照、轨迹时间线、完整 diff。不含任何评价。", "",
        "## 基本信息", "",
        f"- 任务目录：`{task_root}`",
        *[f"- {k}：{v}" for k, v in fields.items() if v],
        f"- SessionID：`{session.get('sessionId', '')}`",
        f"- TurnID/PromptID：`{session.get('promptId', '')}`",
        f"- Harness 版本：`{session.get('harnessVersion', '')}`（entrypoint `{session.get('entrypoint', '')}`）",
        f"- 初始环境快照：{gh.permalink(state, initial) or initial}",
        f"- 产物快照：{gh.permalink(state, sha) or sha}",
        f"- 模型自己的提交：{len(artifact.get('modelCommits') or [])} 个",
        f"- 轨迹文件：`{session.get('trace', '')}`",
        f"- 运行录屏：`{(state.get('recording') or {}).get('video', '未录制')}`", "",
        "## User Prompt", "", Path(state["promptPath"]).read_text(encoding="utf-8").rstrip(), "",
        "## 变更统计", "", "```text", artifact.get("stat", "").rstrip(), "```", "",
    ]
    if skipped:
        lines += [f"未展开 diff 的生成文件：{', '.join(skipped)}", ""]
    body = timeline.read_text(encoding="utf-8").split("\n", 1)[1].lstrip()
    lines += ["## 轨迹时间线", "", body.replace("\n## ", "\n### "), "",
              "## 完整 diff", "", f"`git diff {initial[:12]} {sha[:12]}`", "", "```diff", diff.rstrip(), "```", ""]
    src = Path(str((state.get("source") or {}).get("path") or ""))
    default_dir = src if str(src) not in ("", ".") and src.is_dir() else task_root / "delivery"
    out_path = Path(args.out).expanduser().resolve() if args.out else default_dir / "产物材料.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    emit({"path": str(out_path), "sizeKB": round(out_path.stat().st_size / 1024, 1), "files": len(names),
          "diffSkipped": skipped})


def cmd_digest(args: argparse.Namespace) -> None:
    task_root = Path(args.task_root).resolve()
    state = load_state(task_root)
    if not state.get("session"):
        raise Solo2611Error("还没有被接受的会话")
    print(_digest(task_root, state))


# ======================================================================= record

def cmd_record(args: argparse.Namespace) -> None:
    if args.action == "list":
        for w in recorder.list_windows():
            print(f"{w['windowId']:>7}  {w['app'][:22]:<22}  {w['width']}x{w['height']:<6} {w['title'][:60]}")
        return
    task_root = Path(args.task_root).resolve()
    state = load_state(task_root)
    if args.action == "plan":
        if not args.mode:
            raise Solo2611Error("record plan 需要 --mode terminal|web")
        path = recorder.write_plan(task_root, args.mode, force=args.force)
        emit({"ok": True, "plan": str(path), "next": "填写计划后运行 `record check-plan --file`"})
        return
    if args.action == "check-plan":
        if not args.file:
            raise Solo2611Error("record check-plan 需要 --file")
        emit(recorder.check_plan(Path(args.file).expanduser().resolve()))
        return
    if args.action == "terminal":
        emit(recorder.open_clean_terminal(task_root, Path(state["workspace"])))
        return
    if args.action == "terminal-stop":
        emit(recorder.close_terminal_session(task_root))
        return
    if args.action == "chrome-start":
        if not args.url:
            raise Solo2611Error("record chrome-start 需要 --url http://127.0.0.1:...")
        emit(recorder.start_chrome(task_root, args.url))
        return
    if args.action == "chrome-stop":
        emit(recorder.stop_chrome(task_root))
        return
    if args.action == "import":
        plan_path = Path(args.plan).expanduser().resolve() if args.plan else task_root / "monitor" / "record-plan.json"
        plan_check: dict[str, Any] | None = None
        if plan_path.is_file():
            plan_check = recorder.check_plan(plan_path)
        meta = recorder.finalize(task_root, Path(args.file).expanduser().resolve(),
                                 {"source": "import", "importedFrom": str(Path(args.file).expanduser())})
        if plan_check:
            meta["recordPlan"] = plan_check["path"]
            meta["recordPlanMode"] = plan_check["mode"]
            state["recordPlan"] = plan_check
    elif args.action == "stop":
        meta = recorder.stop(task_root)
    elif args.action == "auto":
        plan_path = Path(args.plan).expanduser().resolve() if args.plan else task_root / "monitor" / "record-plan.json"
        meta = recorder.run_plan(task_root, Path(state["workspace"]), plan_path, max_seconds=float(args.max))
        state["recordPlan"] = recorder.check_plan(plan_path)
    else:
        if not args.window_id:
            raise Solo2611Error("录制必须显式传 `--window-id`；禁止按应用名、标题或整屏猜测")
        window = recorder.pick_window(window_id=int(args.window_id))
        if recorder._window_app(window) == "Chrome":
            chrome_session = read_json(task_root / "monitor" / "chrome-session.json", {}) or {}
            if (
                chrome_session.get("status") != "ok"
                or int(chrome_session.get("windowId") or 0) != int(window["windowId"])
                or int(chrome_session.get("windowOwnerPid") or 0) != int(window.get("pid") or 0)
            ):
                raise Solo2611Error("Chrome 窗口不是本任务新开的独立实例；先运行 `record chrome-start --url ...`")
        plan_path = Path(args.plan).expanduser().resolve() if args.plan else task_root / "monitor" / "record-plan.json"
        plan_check: dict[str, Any] | None = None
        if plan_path.is_file():
            plan_check = recorder.check_plan(plan_path)
            target_apps = {str(item) for item in (read_json(plan_path, {}) or {}).get("targetApps", [])}
            app_name = recorder._window_app(window)
            if app_name not in target_apps:
                raise Solo2611Error(f"窗口 {args.window_id} 属于 {app_name}，不在录屏计划 {sorted(target_apps)} 中")
        meta = recorder.start(task_root, window, max_seconds=float(args.max))
        if plan_check:
            meta["recordPlan"] = plan_check["path"]
            meta["recordPlanMode"] = plan_check["mode"]
            recorder.write_json(recorder._meta_path(task_root), meta)
            state["recordPlan"] = plan_check
            save_state(task_root, state)
        if args.action == "start":
            emit({"recording": True, "window": window, "hint": "现在操作产物，结束后运行 record stop（最长自动停在 "
                  f"{int(args.max)} 秒）"})
            return
        time.sleep(float(args.seconds))
        meta = recorder.stop(task_root)
    state["recording"] = {k: meta.get(k) for k in (
        "video", "durationSeconds", "width", "height", "sizeMB", "needsObjectStorage",
        "source", "finishedAt", "captureKind", "captureBackend", "windowCaptureOk",
        "pointerStrategy", "activationPerformed", "recordPlan", "recordPlanMode",
    )}
    if state.get("status") in {"snapshotted", "ran"}:
        state["status"] = "recorded"
    save_state(task_root, state)
    emit(state["recording"])


# ======================================================================= verify

def cmd_verify(args: argparse.Namespace) -> None:
    """把一条真实命令的执行结果固定成证据；feedback 里的事实只能来自这里和轨迹。"""
    task_root = Path(args.task_root).resolve()
    state = load_state(task_root)
    workspace = Path(state.get("workspace") or "")
    if not workspace.is_dir():
        raise Solo2611Error(f"任务工作区不存在：{workspace}")
    argv = [str(item) for item in (args.command or [])]
    run = evidence.record_run(
        task_root, workspace, argv, label=args.label, kind="runtime", timeout=float(args.timeout or 900),
    )
    existing = evidence.runs(task_root)
    emit({
        "ok": run["exitCode"] == 0,
        "index": run["index"],
        "label": run["label"],
        "command": run["command"],
        "exitCode": run["exitCode"],
        "timedOut": run["timedOut"],
        "durationSeconds": run["durationSeconds"],
        "stdoutTail": " ".join(run["stdout"].split())[-400:],
        "stderrTail": " ".join(run["stderr"].split())[-400:],
        "log": run["log"],
        "totalRuns": len(existing),
        "okRuns": sum(1 for item in existing if item.get("exitCode") == 0),
    })


# ======================================================================= feedback

def cmd_feedback(args: argparse.Namespace) -> None:
    task_root = Path(args.task_root).resolve()
    state = load_state(task_root)
    path = task_root / "delivery" / "feedback.md"
    if args.init:
        if path.exists():
            print(f"已存在，不覆盖：{path}")
        else:
            path.write_text(fb.TEMPLATE, encoding="utf-8")
            print(path)
        print(fb.ensure_full_score_review_template(task_root))
        return
    result = fb.check(path, task_root=task_root, state=state)
    write_json(task_root / "monitor" / "feedback-check.json", {k: v for k, v in result.items() if k != "feedback"})
    trace_alignment = result.get("traceAlignment") or {}
    write_json(task_root / "monitor" / "feedback-trace-audit.json", trace_alignment)
    verification = {
        "sha256": sha256_file(evidence.verification_path(task_root)) if evidence.verification_path(task_root).is_file() else "",
        "runs": len(evidence.runs(task_root)),
        "feedbackSha256": sha256_file(path),
        "traceAlignment": {
            "status": trace_alignment.get("status"),
            "feedbackRuntimeCeiling": trace_alignment.get("feedbackRuntimeCeiling"),
            "traceCapabilityLevel": trace_alignment.get("traceCapabilityLevel"),
            "unsupportedRuntimeClaims": trace_alignment.get("unsupportedRuntimeClaims") or [],
            "traceOpinion": trace_alignment.get("traceOpinion") or {},
        },
        "localReview": result.get("localReview") or {},
        "checkedAt": utc_now(),
    }
    write_json(task_root / "monitor" / "feedback-evidence.json", verification)
    ev = result.get("evidence") or {}
    state["feedbackCheck"] = {
        "ok": result["ok"], "score": result["score"], "warnings": result["warnings"],
        "sha256": sha256_file(path), "checkedAt": utc_now(),
        "evidence": ev.get("summary") or {},
        "traceAlignment": {
            "status": trace_alignment.get("status"),
            "feedbackRuntimeCeiling": trace_alignment.get("feedbackRuntimeCeiling"),
            "traceCapabilityLevel": trace_alignment.get("traceCapabilityLevel"),
            "unsupportedRuntimeClaims": trace_alignment.get("unsupportedRuntimeClaims") or [],
        },
        "fullScoreReview": result.get("fullScoreReview") or {},
        "localReview": result.get("localReview") or {},
        "verificationSha256": verification["sha256"],
    }
    save_state(task_root, state)
    emit({
        "ok": result["ok"], "score": result["score"], "errors": result["errors"], "warnings": result["warnings"],
        "evidence": ev.get("summary") or {},
        "traceAlignment": trace_alignment,
        "fullScoreReview": result.get("fullScoreReview") or {},
        "localReviewRequired": bool(result.get("localReviewRequired")),
        "localReviewStatus": result.get("localReviewStatus") or "not_required",
        "localReview": result.get("localReview") or {},
    })
    if result.get("localReviewStatus") in {"pending", "rejected"}:
        raise SystemExit(4)
    if not result["ok"]:
        raise SystemExit(2)


# ======================================================================= submission

def cmd_submit(args: argparse.Namespace) -> None:
    task_root = Path(args.task_root).resolve()
    config = load_config()
    auto_submit = bool(config_get(config, "submission.autoSubmit"))
    execute = bool(args.execute or (auto_submit and not args.dry_run))
    watch = bool(args.watch or (auto_submit and execute))
    if execute and args.dry_run:
        raise Solo2611Error("--execute 与 --dry-run 不能同时使用")
    result = submission.submit_task(
        task_root,
        execute=execute,
        watch=watch,
        server=str(config_get(config, "submission.server") or submission.DEFAULT_SERVER),
        poll_interval=float(args.poll_interval or config_get(config, "submission.pollIntervalSeconds") or 300),
        poll_timeout=float(args.poll_timeout or config_get(config, "submission.pollTimeoutSeconds") or 86400),
        locked_domain=str(config_get(config, "submission.lockedDomain") or ""),
    )
    emit(result)
    if result.get("repairRequired"):
        raise SystemExit(3)
    if not result.get("ok"):
        raise SystemExit(2)


def cmd_submission_status(args: argparse.Namespace) -> None:
    task_root = Path(args.task_root).resolve()
    config = load_config()
    result = submission.poll_submission(
        task_root,
        args.submission_id,
        server=str(config_get(config, "submission.server") or submission.DEFAULT_SERVER),
        interval=float(args.poll_interval or config_get(config, "submission.pollIntervalSeconds") or 300),
        timeout=float(args.poll_timeout or config_get(config, "submission.pollTimeoutSeconds") or 86400),
    )
    submission._save_result(task_root, result)
    emit(result)
    if result.get("repairRequired"):
        raise SystemExit(3)
    if not result.get("ok"):
        raise SystemExit(2)


def cmd_repair(args: argparse.Namespace) -> None:
    task_root = Path(args.task_root).resolve()
    config = load_config()
    execute = bool(args.execute)
    result = submission.repair_task(
        task_root,
        args.submission_id,
        comment=args.comment,
        execute=execute,
        watch=bool(args.watch or execute),
        refresh_video=bool(args.refresh_video),
        server=str(config_get(config, "submission.server") or submission.DEFAULT_SERVER),
        poll_interval=float(args.poll_interval or config_get(config, "submission.pollIntervalSeconds") or 300),
        poll_timeout=float(args.poll_timeout or config_get(config, "submission.pollTimeoutSeconds") or 86400),
        locked_domain=str(config_get(config, "submission.lockedDomain") or ""),
        review_score_delta=int(
            config_get(config, "submission.localReviewScoreDelta") or submission.LOCAL_REVIEW_SCORE_DELTA
        ),
        review_min_similarity=float(
            config_get(config, "submission.localReviewMinSimilarity") or submission.LOCAL_REVIEW_MIN_SIMILARITY
        ),
    )
    emit(result)
    if result.get("localReviewRequired"):
        raise SystemExit(4)
    if result.get("repairRequired"):
        raise SystemExit(3)
    if not result.get("ok"):
        raise SystemExit(2)


def cmd_local_review(args: argparse.Namespace) -> None:
    decision = "approved" if args.approve else "rejected" if args.reject else ""
    result = submission.local_review(
        Path(args.task_root).resolve(),
        decision=decision,
        reviewer=str(args.reviewer or ""),
        note=str(args.note or ""),
        kind=str(args.kind or "auto"),
    )
    emit(result)
    if result.get("status") in {"pending", "rejected"}:
        raise SystemExit(4)


# ======================================================================= export

def build_row(task_root: Path) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    state = load_state(task_root)
    fields = state.get("fields", {})
    session = state.get("session") or {}
    artifact = state.get("artifact") or {}
    rec = state.get("recording") or {}
    missing: list[str] = []
    fb_path = task_root / "delivery" / "feedback.md"
    fbr: dict[str, Any] = {"score": "", "feedback": "", "remark": "", "warnings": []}
    if fb_path.is_file():
        checked = fb.check(fb_path, task_root=task_root, state=state)
        if checked["ok"]:
            fbr = checked
        else:
            missing.append("Feedback 检查未通过：" + "；".join(checked["errors"]))
    else:
        missing.append("delivery/feedback.md（证据化起草并完成专家复核）")
    trace_api_key_risk = (
        tr.detect_plaintext_api_key_risk(Path(session["trace"]))
        if session.get("trace") and Path(session["trace"]).is_file()
        else {"risk": False, "signals": [], "remark": ""}
    )
    submission_remark = tr.submission_remark(state, api_key_remark=trace_api_key_risk["remark"])
    run_state = state.get("sessionRun") or {}
    if run_state.get("status") == "aborted":
        missing.append("会话已终止，终止的题不允许导出")
    elif run_state and run_state.get("status") not in {None, "", "accepted"}:
        missing.append("会话尚未进入可导出的终态")
    prompt = Path(state["promptPath"]).read_text(encoding="utf-8") if state.get("promptPath") else ""
    if state.get("promptPath"):
        prompt_path = Path(state["promptPath"])
        validation = read_json(task_root / "monitor" / "prompt" / "validation.json", {}) or {}
        if not prompt_path.is_file() or sha256_file(prompt_path) != state.get("promptSha256"):
            missing.append("提示词在安装后被修改或丢失")
        if validation.get("ok") is not True:
            missing.append("提示词难度/ra-人话验证记录未通过")
        for key, label in (("promptReview", "ra-人话审核记录"), ("difficultyReview", "难度论证")):
            if not str(state.get(key) or "") or not Path(state[key]).is_file():
                missing.append(label)
    row = {
        "User Prompt": prompt,
        "所属领域": fields.get("所属领域", ""),
        "子方向": fields.get("子方向", ""),
        "任务类型": fields.get("任务类型", ""),
        "任务难度": fields.get("任务难度", ""),
        "语言/框架": fields.get("语言/框架", ""),
        "Harness": fields.get("Harness", ""),
        "Harness 版本": session.get("harnessVersion", ""),
        "操作系统": fields.get("操作系统", ""),
        "环境可复现等级": fields.get("环境可复现等级", ""),
        "初始环境快照": gh.permalink(state, state.get("initialSha", "")),
        "SessionID": session.get("sessionId", ""),
        "TurnID/PromptID": session.get("promptId", ""),
        "轨迹文件": session.get("trace", ""),
        "产物快照": gh.permalink(state, artifact.get("sha", "")),
        "运行录屏": rec.get("video", ""),
        "交付完整性": fbr["score"],
        "Feedback": fbr["feedback"],
        "备注": submission_remark,
    }
    optional = {"环境可复现等级", "备注"}
    if missing:  # feedback 已经报过缺失，不再重复列这两列
        optional |= {"交付完整性", "Feedback"}
    missing += [k for k in SUBMIT_COLUMNS if k not in optional and row.get(k) in ("", None)]
    if rec.get("video") and not Path(rec["video"]).is_file():
        missing.append("录屏文件已不存在")
    if session.get("trace") and not Path(session["trace"]).is_file():
        missing.append("轨迹文件已不存在")
    internal = {
        "任务目录": str(task_root), "运行模式": session.get("mode", ""), "实际尝试次数": len(state.get("attempts", [])),
        "初始快照 SHA": state.get("initialSha", ""), "产物快照 SHA": artifact.get("sha", ""),
        "远端": (state.get("remote") or {}).get("web", "未推送"),
        "轨迹目录": session.get("tracesDir", ""), "子代理轨迹数": session.get("subagentFiles", 0),
        "录屏时长(秒)": rec.get("durationSeconds", ""), "录屏大小(MB)": rec.get("sizeMB", ""),
        "录屏采集方式": rec.get("captureKind", ""), "录屏窗口证据": rec.get("windowCaptureOk", ""),
        "Feedback 检查警告": "；".join(fbr.get("warnings") or []), "导出时间": utc_now(),
        "轨迹明文API Key风险": trace_api_key_risk["risk"],
    }
    return row, internal, missing


def _field_guide(row: dict[str, Any], state: dict[str, Any]) -> str:
    src = (SKILL_ROOT / "references" / "field-guide.md").read_text(encoding="utf-8")
    lines = [src.rstrip(), "", "## 本题取值", ""]
    for k in SUBMIT_COLUMNS:
        v = str(row.get(k, ""))
        v = v if len(v) <= 80 else v[:80] + "…"
        lines.append(f"- {k}：{v.replace(chr(10), ' ') or '（空）'}")
    return "\n".join(lines) + "\n"


def cmd_export(args: argparse.Namespace) -> None:
    task_root = Path(args.task_root).resolve()
    state = load_state(task_root)
    config = load_config()
    row, internal, missing = build_row(task_root)
    if missing and not args.allow_incomplete:
        emit({"ok": False, "missing": missing})
        raise SystemExit(2)
    xlsx = task_root / "delivery" / "交付表.xlsx"
    excel.write_workbook(xlsx, [row], [internal])
    excel.read_back(xlsx)
    guide = task_root / "delivery" / "字段说明.md"
    guide.write_text(_field_guide(row, state), encoding="utf-8")
    if not missing:
        state["status"] = "exported"
    state["export"] = {"xlsx": str(xlsx), "guide": str(guide), "missing": missing, "exportedAt": utc_now(),
                       "exportedDate": local_date()}
    save_state(task_root, state)
    emit({"ok": not missing, "xlsx": str(xlsx), "guide": str(guide), "missing": missing})
    if not missing and bool(config_get(config, "submission.autoSubmit")) and not args.no_submit:
        submitted = submission.submit_task(
            task_root,
            execute=True,
            watch=True,
            server=str(config_get(config, "submission.server") or submission.DEFAULT_SERVER),
            poll_interval=float(config_get(config, "submission.pollIntervalSeconds") or 300),
            poll_timeout=float(config_get(config, "submission.pollTimeoutSeconds") or 86400),
        )
        emit(submitted)
        if submitted.get("repairRequired"):
            raise SystemExit(3)
        if not submitted.get("ok"):
            raise SystemExit(2)


def cmd_export_batch(args: argparse.Namespace) -> None:
    config = load_config()
    tasks_dir = Path(args.tasks_dir or config["tasksDir"]).expanduser().resolve()
    rows, internals, skipped = [], [], []
    seen_sessions: dict[str, str] = {}
    seen_prompts: dict[str, str] = {}
    for st in sorted(tasks_dir.glob("*/monitor/state.json")):
        task_root = st.parent.parent
        state = read_json(st, {})
        if args.date and (state.get("export") or {}).get("exportedDate", state.get("createdDate")) != args.date:
            continue
        row, internal, missing = build_row(task_root)
        if missing:
            skipped.append({"task": str(task_root), "missing": missing})
            continue
        sid, psha = row["SessionID"], state.get("promptSha256", "")
        if sid in seen_sessions or psha in seen_prompts:
            skipped.append({"task": str(task_root), "missing": [f"与 {seen_sessions.get(sid) or seen_prompts.get(psha)} 重复"]})
            continue
        seen_sessions[sid], seen_prompts[psha] = str(task_root), str(task_root)
        rows.append(row)
        internals.append(internal)
    target = Path(args.out).expanduser().resolve() if args.out else tasks_dir / f"交付表-{args.date or local_date()}.xlsx"
    if rows:
        excel.write_workbook(target, rows, internals)
        excel.read_back(target)
    emit({"ok": bool(rows), "xlsx": str(target) if rows else "", "rows": len(rows), "skipped": skipped})


# ======================================================================= status / push / cleanup

def cmd_status(args: argparse.Namespace) -> None:
    task_root = Path(args.task_root).resolve()
    state = load_state(task_root)
    session = state.get("session") or {}
    rec = state.get("recording") or {}
    capture_report = read_json(task_root / "recording" / "运行录屏-window-capture.json", {}) or {}
    cursor_report = read_json(task_root / "recording" / "运行录屏-cursor-guard.json", {}) or {}
    chrome_instance = read_json(task_root / "recording" / "chrome-instance.json", {}) or {}
    chrome_cleanup = read_json(task_root / "recording" / "chrome-profile-cleanup.json", {}) or {}
    terminal_cleanup = read_json(task_root / "monitor" / "terminal-cleanup.json", {}) or {}
    prompt_validation = read_json(task_root / "monitor" / "prompt" / "validation.json", {}) or {}
    chrome_ok = True
    if rec.get("recordPlanMode") == "web" or capture_report.get("ownerApp") == "Chrome":
        chrome_ok = bool(
            chrome_instance.get("status") == "ok"
            and chrome_instance.get("dedicatedInstance") is True
            and chrome_instance.get("reusedRunningChrome") is False
            and chrome_instance.get("userChromeTouched") is False
            and chrome_instance.get("debugPortOwnerVerified") is True
            and chrome_cleanup.get("status") == "removed"
            and chrome_cleanup.get("existsAfter") is False
        )
    terminal_ok = True
    if rec.get("recordPlanMode") == "web":
        terminal_ok = bool(
            terminal_cleanup.get("status") == "removed"
            and terminal_cleanup.get("windowExistsAfter") is False
            and terminal_cleanup.get("sessionExistsAfter") is False
        )
    rec_ok = bool(
        rec.get("video")
        and rec.get("windowCaptureOk") is True
        and (
            (capture_report.get("status") == "ok" and cursor_report.get("status") == "ok")
            or capture_report.get("status") == cursor_report.get("status") == "manual-import"
        )
        and cursor_report.get("pointerStrategy") == "none"
        and chrome_ok
        and terminal_ok
    )
    fbc = state.get("feedbackCheck") or {}
    checks = [
        ("初始快照", bool(is_full_sha(state.get("initialSha", ""))), state.get("initialSha", "")),
        ("GitHub 初始快照", bool(gh.permalink(state, state.get("initialSha", ""))),
         gh.permalink(state, state.get("initialSha", ""))),
        ("提示词（难度+人话）", bool(state.get("promptPath") and prompt_validation.get("ok") is True),
         (state.get("fields") or {}).get("任务类型", "")),
        ("单轮会话", bool(session), session.get("sessionId", "")),
        ("TurnID/PromptID", bool(session.get("promptId")), session.get("promptId", "")),
        ("产物快照", bool((state.get("artifact") or {}).get("sha")), (state.get("artifact") or {}).get("shortStat", "")),
        ("GitHub 产物快照", bool(gh.permalink(state, (state.get("artifact") or {}).get("sha", ""))),
         gh.permalink(state, (state.get("artifact") or {}).get("sha", ""))),
        ("轨迹时间线", (task_root / "delivery" / "轨迹时间线.md").is_file(), ""),
        ("运行录屏", rec_ok, f"{rec['durationSeconds']}s {rec.get('sizeMB')}MB {rec.get('captureKind', '')}" if rec.get("video") else ""),
        ("Feedback（证据化并复核）", bool(fbc.get("ok")), f"score={fbc['score']}" if fbc.get("score") is not None else ""),
        ("Excel", (state.get("export") or {}).get("missing") == [], (state.get("export") or {}).get("xlsx", "")),
    ]
    print(f"任务：{task_root}\n状态：{state.get('status')}  尝试次数：{len(state.get('attempts', []))}\n")
    for name, ok, detail in checks:
        print(f"  {'✅' if ok else '⬜'} {name:<20} {detail}")
    submission_state = state.get("submission") or {}
    if submission_state:
        print(
            "\n  平台提交："
            f"{submission_state.get('status', '')}  "
            f"id={submission_state.get('submissionId', '')}  "
            f"{submission_state.get('qcSummary', '')}"
        )
    if state.get("pendingContainer"):
        print(f"\n  ⚠️ 交互容器 {state['pendingContainer']} 尚未 collect")


def cmd_repo(args: argparse.Namespace) -> None:
    task_root = Path(args.task_root).resolve()
    state = load_state(task_root)
    if (state.get("artifact") or {}).get("sha") is None and state.get("session"):
        raise Solo2611Error("会话已接受但还没有产物快照，先运行 snapshot")
    remote = gh.ensure_repo(task_root, state, load_config(), name=args.name or "", private=args.private)
    state = load_state(task_root)
    emit({"repo": remote["web"], "private": remote.get("private"),
          "初始环境快照": gh.permalink(state, state["initialSha"]),
          "产物快照": gh.permalink(state, (state.get("artifact") or {}).get("sha", "")) or "（运行后自动推送）",
          "verified": remote.get("verified")})


def cmd_cleanup(args: argparse.Namespace) -> None:
    task_root = Path(args.task_root).resolve()
    state = load_state(task_root)
    if state.get("pendingContainer") and not args.force:
        raise Solo2611Error(f"交互容器 {state['pendingContainer']} 的轨迹还没 collect，删除会丢轨迹；确认丢弃加 --force")
    names = out(run(["docker", "ps", "-a", "--filter", f"label=solo2611.task={task_label(task_root)}",
                     "--format", "{{.Names}}"], check=False)).split()
    removed = [n for n in names if run(["docker", "rm", "-f", n], check=False).returncode == 0]
    for n in removed:
        capacity.release_container(n)
    state.pop("pendingContainer", None)
    save_state(task_root, state)
    emit({"removed": removed, "terminals": managed.sweep_session_terminals()})


def cmd_capacity(args: argparse.Namespace) -> None:
    """查看容器名额：上限、运行中、启动中和剩余。"""
    emit(capacity.snapshot(int(config_get(load_config(), "claude.maxContainers") or 4)))


def cmd_terminal_sweep(args: argparse.Namespace) -> None:
    """关闭所有已结束会话残留的可见终端窗口；仍在运行的会话不受影响。"""
    emit(managed.sweep_session_terminals())


# ======================================================================= main

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="solo2611", description=__doc__)
    p.add_argument("--version", action="version", version=version())
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("config", help="设备配置")
    s.add_argument("action", choices=["init", "show", "set", "path", "key", "test"])
    s.add_argument("pairs", nargs="*")
    s.add_argument("--force", action="store_true", help="init 时覆盖已有配置")
    s.set_defaults(fn=cmd_config)

    s = sub.add_parser("init", help="建任务目录并提交初始环境快照")
    s.add_argument("--task-name", required=True)
    s.add_argument("--source")
    s.add_argument("--git")
    s.add_argument("--ref")
    s.add_argument("--tasks-dir")
    s.add_argument("--domain", choices=list(DOMAINS), help="必须与配置中的账号唯一大类一致")
    s.add_argument("--sub-direction", help="必须是该大类下且与真实工程匹配的子方向")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("prompt-history", help="查询当前工程在工作台里用过的提示词")
    s.add_argument("--task-root", required=True)
    s.add_argument("--text", help="可选：同时检查一段候选题面")
    s.add_argument("--local-only", action="store_true", help="仅读技能本地历史，不访问工作台")
    s.set_defaults(fn=cmd_prompt_history)

    s = sub.add_parser("prompt", help="初始化题面证据 / 安装提示词并做规范门禁")
    s.add_argument("--task-root", required=True)
    s.add_argument("--init", action="store_true", help="生成题面草稿、难度论证和 ra-人话审核模板")
    s.add_argument("--file")
    s.add_argument("--task-type", choices=TASK_TYPES)
    s.add_argument("--difficulty", choices=DIFFICULTIES)
    s.add_argument("--lang", help="语言/框架，逗号分隔，如 Python, PostgreSQL")
    s.add_argument("--domain", choices=list(DOMAINS), help="只能与配置中的账号唯一大类一致")
    s.add_argument("--sub-direction", help="必须是该大类下且与真实工程匹配的子方向")
    s.add_argument("--repro", choices=REPRO_LEVELS)
    s.add_argument("--review", help="ra-人话审核记录，默认 monitor/prompt/ra-renhua-review.json")
    s.add_argument("--difficulty-review", help="难度论证，默认 monitor/prompt/difficulty-review.json")
    s.add_argument("--allow-over-170", action="store_true", help="题面确有需要时允许超过 170 字，仍不得超过 240 字")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_prompt)

    s = sub.add_parser("run", help="受管交互单轮执行（默认脱离；print 为回退）")
    s.add_argument("--task-root", required=True)
    s.add_argument("--mode", choices=["supervised", "print"])
    s.add_argument("--attempts", type=int, help="兼容旧参数；v4 不再整题重跑")
    s.add_argument("--timeout", type=float)
    s.add_argument("--force", action="store_true")
    s.add_argument("--skip-repo", action="store_true", help="不要求初始快照已推送（本地试跑）")
    s.add_argument("--follow", action="store_true", help="等待终态并持续输出进度")
    s.add_argument("--hard-stop", action="store_true", help="Ctrl-C 时立即停止容器并恢复工作区")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("run-status", help="查询受管会话进度")
    s.add_argument("--task-root", required=True)
    s.add_argument("--json", action="store_true")
    s.add_argument("--lines", type=int, default=20)
    s.set_defaults(fn=cmd_run_status)

    s = sub.add_parser("resume", help="幂等接回监督器或 print 容器")
    s.add_argument("--task-root", required=True)
    s.add_argument("--follow", action="store_true")
    s.set_defaults(fn=cmd_resume)

    s = sub.add_parser("continue", help="人工发送一次字面“继续”（计数）")
    s.add_argument("--task-root", required=True)
    s.add_argument("--note", help="仅写入事件日志，不发送给模型")
    s.set_defaults(fn=cmd_continue)

    s = sub.add_parser("abort", help="终止会话并保留现场")
    s.add_argument("--task-root", required=True)
    s.add_argument("--reason", required=True)
    s.add_argument("--by", choices=["human", "auto"], default="human")
    s.set_defaults(fn=cmd_abort)

    s = sub.add_parser("resume-all", help="批量接回未结束会话")
    s.add_argument("--tasks-dir")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_resume_all)

    s = sub.add_parser("doctor", help="生成基线或修复历史悬空状态")
    s.add_argument("action", nargs="?", choices=["baseline", "fix-stale"])
    s.add_argument("--tasks-dir")
    s.add_argument("--out")
    s.add_argument("--fix-stale", action="store_true")
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("supervise", help="内部入口：常驻监督一个会话")
    s.add_argument("--task-root", required=True)
    s.add_argument("--attempt", required=True, type=int)
    s.add_argument("--attach", action="store_true")
    s.set_defaults(fn=cmd_supervise)

    s = sub.add_parser("interactive", help="生成官方交互式启动命令（专家手动粘贴提示词）")
    s.add_argument("--task-root", required=True)
    s.add_argument("--skip-repo", action="store_true")
    s.set_defaults(fn=cmd_interactive)

    s = sub.add_parser("collect", help="交互模式结束后导出轨迹、校验并打产物快照")
    s.add_argument("--task-root", required=True)
    s.add_argument("--container")
    s.set_defaults(fn=cmd_collect)

    s = sub.add_parser(
        "recover-round",
        help="误触发继续后，按正常完成轮次回切正式轨迹（保留原始审计副本）",
    )
    s.add_argument("--task-root", required=True)
    s.add_argument("--round", type=int, default=1, help="作为正式边界的正常完成轮次")
    s.add_argument("--reason", required=True, help="误触发继续的客观原因")
    s.add_argument("--execute", action="store_true", help="确认预览后写入边界并接受会话")
    s.set_defaults(fn=cmd_recover_round)

    s = sub.add_parser("reset", help="把 workspace 恢复到初始快照")
    s.add_argument("--task-root", required=True)
    s.add_argument("--yes", action="store_true")
    s.set_defaults(fn=cmd_reset)

    for name, fn, help_text in (("snapshot", cmd_snapshot, "重打产物快照"), ("digest", cmd_digest, "重新生成轨迹时间线"),
                                ("status", cmd_status, "检查交付项")):
        s = sub.add_parser(name, help=help_text)
        s.add_argument("--task-root", required=True)
        s.set_defaults(fn=fn)

    s = sub.add_parser("record", help="录屏计划与窗口级采集")
    s.add_argument("action", choices=[
        "list", "plan", "check-plan", "terminal", "terminal-stop", "auto", "start", "stop", "for", "import",
        "chrome-start", "chrome-stop",
    ])
    s.add_argument("--task-root")
    s.add_argument("--window-id", type=int)
    s.add_argument("--max", type=float, default=90)
    s.add_argument("--seconds", type=float, default=60)
    s.add_argument("--file")
    s.add_argument("--plan")
    s.add_argument("--mode", choices=["terminal", "web"])
    s.add_argument("--url")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_record)

    s = sub.add_parser("verify", help="在产物上真实执行一条命令，把输出固定成 Feedback 的证据")
    s.add_argument("--task-root", required=True)
    s.add_argument("--label", required=True, help="这条命令验证什么，例如「重跑同一批看是否新增」")
    s.add_argument("--timeout", type=float, default=900)
    s.add_argument("command", nargs=argparse.REMAINDER, help="-- 之后是要执行的命令及其参数")
    s.set_defaults(fn=cmd_verify)

    s = sub.add_parser("feedback", help="生成模板 / 检查证据化并复核的 feedback.md")
    s.add_argument("--task-root", required=True)
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--init", action="store_true")
    g.add_argument("--check", action="store_true")
    s.set_defaults(fn=cmd_feedback)

    s = sub.add_parser("submit", help="提交 SOLO2；默认遵守 submission.autoSubmit 开关")
    s.add_argument("--task-root", required=True)
    mode = s.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    s.add_argument("--watch", action="store_true")
    s.add_argument("--poll-interval", type=float)
    s.add_argument("--poll-timeout", type=float)
    s.set_defaults(fn=cmd_submit)

    s = sub.add_parser("submission-status", help="刷新 SOLO2 提交或质检状态")
    s.add_argument("--task-root", required=True)
    s.add_argument("--submission-id", required=True)
    s.add_argument("--poll-interval", type=float)
    s.add_argument("--poll-timeout", type=float)
    s.set_defaults(fn=cmd_submission_status)

    s = sub.add_parser("repair", help="提交 PENDING_FIX 返修")
    s.add_argument("--task-root", required=True)
    s.add_argument("--submission-id", required=True)
    s.add_argument("--comment", required=True)
    s.add_argument("--execute", action="store_true")
    s.add_argument("--watch", action="store_true")
    s.add_argument("--refresh-video", action="store_true")
    s.add_argument("--poll-interval", type=float)
    s.add_argument("--poll-timeout", type=float)
    s.set_defaults(fn=cmd_repair)

    s = sub.add_parser("local-review", help="查看或登记返修前的本地核验结论")
    s.add_argument("--task-root", required=True)
    decision = s.add_mutually_exclusive_group()
    decision.add_argument("--approve", action="store_true")
    decision.add_argument("--reject", action="store_true")
    s.add_argument("--reviewer")
    s.add_argument("--note")
    s.add_argument("--kind", choices=["auto", "feedback", "repair"], default="auto")
    s.set_defaults(fn=cmd_local_review)

    s = sub.add_parser("export", help="导出单题交付表")
    s.add_argument("--task-root", required=True)
    s.add_argument("--allow-incomplete", action="store_true")
    s.add_argument("--no-submit", action="store_true", help="返修重导出时禁止自动重新提交")
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("export-batch", help="合并多题为一张交付表")
    s.add_argument("--tasks-dir")
    s.add_argument("--date", help="按导出日期筛选，YYYY-MM-DD")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_export_batch)

    s = sub.add_parser("bundle", help="把题面、会话、快照、轨迹时间线和完整 diff 汇成一份 Markdown")
    s.add_argument("--task-root", required=True)
    s.add_argument("--out", help="默认写到 init 时的源码目录（--source）；git 来源写到 TASK_ROOT/delivery/")
    s.set_defaults(fn=cmd_bundle)

    s = sub.add_parser("repo", help="新建 GitHub 公开仓库并推送初始/产物快照（可重复执行，只补推缺的）")
    s.add_argument("--task-root", required=True)
    s.add_argument("--name", help="仓库名提示，默认任务名；会自动转成可读 ASCII 并加 4 位随机后缀")
    s.add_argument("--private", action="store_true", help="规范要求评测团队可访问，默认公开")
    s.set_defaults(fn=cmd_repo)

    s = sub.add_parser("cleanup", help="删除本题遗留容器")
    s.add_argument("--task-root", required=True)
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_cleanup)

    s = sub.add_parser("capacity", help="查看容器名额占用")
    s.set_defaults(fn=cmd_capacity)

    s = sub.add_parser("terminal-sweep", help="关闭已结束会话残留的可见终端窗口")
    s.set_defaults(fn=cmd_terminal_sweep)

    sub.add_parser("version", help="打印版本").set_defaults(fn=lambda a: print(version()))
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "record" and args.action != "list" and not args.task_root:
        raise SystemExit("record 需要 --task-root")
    if args.cmd == "record" and args.action == "import" and not args.file:
        raise SystemExit("record import 需要 --file")
    try:
        args.fn(args)
    except (Solo2611Error, OSError, RuntimeError) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import excel  # noqa: E402
import evidence  # noqa: E402
import feedback as fb  # noqa: E402
import github_repo as gh  # noqa: E402
import recorder  # noqa: E402
import prompting  # noqa: E402
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
CONTAINER_LOCK = STATE_HOME / "containers.lock"


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

def _load_history() -> list[dict[str, Any]]:
    if not PROMPT_HISTORY.is_file():
        return []
    rows = []
    for line in PROMPT_HISTORY.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


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
        emit({"ok": True, "draft": str(draft), "templates": templates,
              "next": "修改题面后填写 difficulty-review.json 与 ra-renhua-review.json，再运行 prompt --file"})
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
    design = prompting.validate_candidate(
        task_root=task_root,
        prompt_path=Path(args.file).resolve(),
        prompt_text=text,
        difficulty=args.difficulty,
        review_path=review_path,
        difficulty_review_path=difficulty_review_path,
        history=_load_history(),
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
    prompting.write_validation(task_root, design)
    PROMPT_HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with PROMPT_HISTORY.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"taskRoot": str(task_root), "sha256": sha, "date": local_date(), "text": text},
                            ensure_ascii=False) + "\n")
    emit({"ok": True, "promptPath": str(prompt_path), "sha256": sha, "warnings": warnings,
          "difficultySignalsPassed": state["difficultySignalsPassed"], "next": "repo"})


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


def _collect_container(task_root: Path, state: dict[str, Any], name: str, attempt_dir: Path,
                       result: dict[str, Any] | None = None) -> dict[str, Any]:
    traces = attempt_dir / "traces"
    shutil.rmtree(traces, ignore_errors=True)
    proc = run(["docker", "cp", f"{name}:{CONTAINER_TRACE_DIR}", str(traces)], check=False, timeout=300)
    if proc.returncode != 0:
        raise Solo2611Error(f"导出轨迹失败，容器 {name} 已保留，不要删除：{proc.stderr.decode(errors='replace')}")
    run(["docker", "rm", name], check=False, timeout=60)
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
    if state.get("session") and not args.force:
        raise Solo2611Error("这道题已有被接受的会话；一个 session 只算一条数据。确需重跑请加 --force（会丢弃现有产物）")
    if sha256_file(Path(state["promptPath"])) != state["promptSha256"]:
        raise Solo2611Error("delivery/提示词.md 在 prompt 之后被改动过")
    _require_repo(state, args.skip_repo)
    if args.force and state.get("session"):
        state.pop("session", None)
        state.pop("artifact", None)
        _reset_workspace(state)
    _check_workspace_pristine(state)
    image = str(config_get(config, "claude.image"))
    key = api_key(config)
    _ensure_image(image)
    attempts = int(args.attempts or config_get(config, "claude.attempts") or 3)
    timeout = float(args.timeout or config_get(config, "claude.timeoutSeconds") or 7200)
    limit = int(config_get(config, "claude.maxContainers") or 4)
    env = os.environ.copy()
    env["apikey"] = key
    env["ANTHROPIC_BASE_URL"] = base_url(config) or DEFAULT_BASE_URL
    ws = ws_of(state)
    os.chmod(ws, 0o777)
    for n in range(1, attempts + 1):
        idx = len(state["attempts"]) + 1
        attempt_dir = task_root / "monitor" / "runs" / f"attempt-{idx:02d}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        name = f"solo2611-{container_slug(state['taskName'])}-{idx:02d}-{random.randrange(16**4):04x}".lower()
        attempt = {"index": idx, "container": name, "mode": "print", "image": image, "startedAt": utc_now(),
                   "status": "running", "dir": str(attempt_dir)}
        state["attempts"].append(attempt)
        state["status"] = "running"
        save_state(task_root, state)
        cmd = _docker_base(name, task_root, ws, image, interactive=False) + ["print"]
        with file_lock(CONTAINER_LOCK):
            while len(_running_solo_containers()) >= limit:
                print(f"容器已达上限 {limit}，等待名额…", file=sys.stderr)
                time.sleep(10)
            stdin = open(state["promptPath"], "rb")
            stdout = open(attempt_dir / "stdout.jsonl", "wb")
            stderr = open(attempt_dir / "stderr.log", "wb")
            proc = subprocess.Popen(cmd, stdin=stdin, stdout=stdout, stderr=stderr, env=env, start_new_session=True)
            for _ in range(60):
                if _container_running(name) or proc.poll() is not None:
                    break
                time.sleep(0.5)
        print(f"[attempt {idx}] 容器 {name} 运行中（超时 {int(timeout)} 秒）", file=sys.stderr)
        started = time.monotonic()
        cancelled = False
        try:
            while proc.poll() is None:
                if time.monotonic() - started > timeout:
                    attempt["timeout"] = True
                    run(["docker", "stop", "-t", "10", name], check=False, timeout=60)
                    break
                time.sleep(5)
            proc.wait(timeout=120)
        except KeyboardInterrupt:
            cancelled = True
            run(["docker", "stop", "-t", "10", name], check=False, timeout=60)
        finally:
            stdin.close()
            stdout.close()
            stderr.close()
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
            info.setdefault("errors", []).append("超过单次超时上限")
        attempt["errors"] = info.get("errors", [])
        if cancelled:
            attempt["status"] = "cancelled"
            _reset_workspace(state)
            state["status"] = "prompt_ready"
            save_state(task_root, state)
            raise Solo2611Error("已中断；workspace 已恢复到初始快照，轨迹留在 " + str(attempt_dir))
        if info.get("ok"):
            _accept_attempt(task_root, state, attempt, info)
            emit({"ok": True, "attempt": idx, "sessionId": info["sessionId"], "promptId": info["promptId"],
                  "artifactSha": state["artifact"]["sha"], "diffStat": state["artifact"]["shortStat"],
                  "timeline": str(task_root / "delivery" / "轨迹时间线.md"), "next": "record"})
            return
        attempt["status"] = "rejected"
        save_state(task_root, state)
        print(f"[attempt {idx}] 不可用：{info.get('errors')}；按规范重新 roll（恢复初始快照 + 新容器 + 新会话）", file=sys.stderr)
        if any("401" in e or "authenticat" in e.lower() for e in info.get("errors", [])):
            state["status"] = "run_failed"
            save_state(task_root, state)
            raise Solo2611Error("网关鉴权失败（401），检查 claude.apiKey 是否为本期团队 Key")
        _reset_workspace(state)
        if n < attempts:
            time.sleep(min(300, 30 * 2 ** (n - 1)))
    state["status"] = "run_failed"
    save_state(task_root, state)
    raise Solo2611Error(f"{attempts} 次尝试都没有得到完整单轮（持续中断/报错时规范要求换题）")


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
        f"提示词已复制到剪贴板（原文：{state['promptPath']}），出现输入框后粘贴发送，只发这一次，不追问、不纠错。",
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
    emit({"ok": False, "errors": info.get("errors"), "hint": "按规范重新 roll：先 `reset --yes` 再 `interactive`"})
    raise SystemExit(2)


def cmd_reset(args: argparse.Namespace) -> None:
    task_root = Path(args.task_root).resolve()
    state = load_state(task_root)
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
    verification = {
        "sha256": sha256_file(evidence.verification_path(task_root)) if evidence.verification_path(task_root).is_file() else "",
        "runs": len(evidence.runs(task_root)),
        "feedbackSha256": sha256_file(path),
        "checkedAt": utc_now(),
    }
    write_json(task_root / "monitor" / "feedback-evidence.json", verification)
    ev = result.get("evidence") or {}
    state["feedbackCheck"] = {
        "ok": result["ok"], "score": result["score"], "warnings": result["warnings"],
        "sha256": sha256_file(path), "checkedAt": utc_now(),
        "evidence": ev.get("summary") or {},
        "fullScoreReview": result.get("fullScoreReview") or {},
        "verificationSha256": verification["sha256"],
    }
    save_state(task_root, state)
    emit({
        "ok": result["ok"], "score": result["score"], "errors": result["errors"], "warnings": result["warnings"],
        "evidence": ev.get("summary") or {},
        "fullScoreReview": result.get("fullScoreReview") or {},
    })
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
    )
    emit(result)
    if result.get("repairRequired"):
        raise SystemExit(3)
    if not result.get("ok"):
        raise SystemExit(2)


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
        "备注": "",
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
    state.pop("pendingContainer", None)
    save_state(task_root, state)
    emit({"removed": removed})


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

    s = sub.add_parser("run", help="官方镜像 print 模式单轮执行（失败自动重新 roll）")
    s.add_argument("--task-root", required=True)
    s.add_argument("--attempts", type=int)
    s.add_argument("--timeout", type=float)
    s.add_argument("--force", action="store_true")
    s.add_argument("--skip-repo", action="store_true", help="不要求初始快照已推送（本地试跑）")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("interactive", help="生成官方交互式启动命令（专家手动粘贴提示词）")
    s.add_argument("--task-root", required=True)
    s.add_argument("--skip-repo", action="store_true")
    s.set_defaults(fn=cmd_interactive)

    s = sub.add_parser("collect", help="交互模式结束后导出轨迹、校验并打产物快照")
    s.add_argument("--task-root", required=True)
    s.add_argument("--container")
    s.set_defaults(fn=cmd_collect)

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
    except Solo2611Error as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""solo-2611 公共工具：配置、状态、Git、JSON 原子写入。"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SKILL_ROOT = Path(__file__).resolve().parent.parent
CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
CONFIG_PATH = Path(os.environ.get("SOLO2611_CONFIG", CODEX_HOME / "solo-2611" / "config.json"))
STATE_HOME = CODEX_HOME / "solo-2611"
CACHE_DIR = CODEX_HOME / "cache" / "solo-2611"

# 官方镜像与固定口径（CLAUDE_CODE_DOCKER_MAC2.md / 26.11 标注说明）
DEFAULT_IMAGE = "adminfather/benzhi-claude-code2:20261110"
CONTAINER_TRACE_DIR = "/home/node/.claude/projects"
DEFAULT_DOMAIN = "数据工程"
# 未显式指定分类时进入数据工程的主要子方向；其他合法领域/子方向按工程实际选择。
DEFAULT_SUB_DIRECTION = "数仓与数据湖"
DEFAULT_TASKS_DIR = Path.home() / "claude-runs" / "solo-2611"

DEFAULT_BASE_URL = "https://llm2.jzxhnh.com"  # 镜像内置的 ANTHROPIC_BASE_URL

DEFAULT_CONFIG: dict[str, Any] = {
    "configVersion": 4,
    "claude": {
        "apiKey": "",
        "baseUrl": DEFAULT_BASE_URL,
        "image": DEFAULT_IMAGE,
        "maxContainers": 4,
        "timeoutSeconds": 10800,
        "silentSeconds": 1200,
        "noProgressSeconds": 1200,
        "launchSpacingSeconds": 20,
        "runMode": "supervised",
        "continueMax": 2,
        "startAttempts": 3,
        "replTimeoutSeconds": 90,
        "slotWaitSeconds": 1800,
        "fatalHoldMinutes": 30,
        "openTerminalOnRun": False,
        "terminalReadOnly": True,
        "terminalActivate": False,
    },
    "tasksDir": str(DEFAULT_TASKS_DIR),
    "git": {"userName": "solo-2611", "userEmail": "solo-2611@local"},
    "defaults": {
        "domain": DEFAULT_DOMAIN,
        "subDirection": DEFAULT_SUB_DIRECTION,
        "harness": "Claude Code",
        "os": "MacOS/Linux",
    },
    "github": {"proxyHttp": "", "accessTokens": "", "autoPush": True},
    "submission": {
        "autoSubmit": True,
        "server": "https://solo2.jzxhnh.com",
        "lockedDomain": "",
        "pollIntervalSeconds": 300,
        "pollTimeoutSeconds": 86400,
        "localReviewScoreDelta": 2,
        "localReviewMinSimilarity": 0.5,
    },
}


class Solo2611Error(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def local_date() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config() -> dict[str, Any]:
    config = _deep_merge(DEFAULT_CONFIG, read_json(CONFIG_PATH, {}) or {})
    # v2 只升级配置结构。安全、游戏图形与多媒体、数据工程的合法子方向均可继续使用，
    # 不能把旧的图形默认值当成错误分类强制改成数据工程。
    if int(config.get("configVersion") or 0) < 2:
        config["configVersion"] = 2
    # 26.11 最新分类取消了「数据工程 / 其他」，旧的默认值迁移到本期主要方向。
    if int(config.get("configVersion") or 0) < 3:
        defaults = config.setdefault("defaults", {})
        if defaults.get("domain") == DEFAULT_DOMAIN and defaults.get("subDirection") == "其他":
            defaults["subDirection"] = DEFAULT_SUB_DIRECTION
        config["configVersion"] = 3
    if int(config.get("configVersion") or 0) < 4:
        claude = config.setdefault("claude", {})
        # v4 只限制新会话能力，不修改磁盘上已有的任务或正在运行的容器。
        claude.setdefault("runMode", "supervised")
        claude.setdefault("continueMax", 2)
        claude.setdefault("startAttempts", 2)
        claude.setdefault("launchSpacingSeconds", 20)
        claude.setdefault("silentSeconds", 1200)
        claude.setdefault("noProgressSeconds", 1200)
        claude.setdefault("fatalHoldMinutes", 30)
        claude.setdefault("openTerminalOnRun", False)
        claude["timeoutSeconds"] = max(10800, int(claude.get("timeoutSeconds") or 10800))
        config["configVersion"] = 4
    claude = config.setdefault("claude", {})
    claude["continueMax"] = min(2, max(0, int(claude.get("continueMax") or 0)))
    claude["startAttempts"] = min(3, max(1, int(claude.get("startAttempts") or 3)))
    claude["replTimeoutSeconds"] = min(600, max(30, int(claude.get("replTimeoutSeconds") or 90)))
    claude["launchSpacingSeconds"] = max(5, int(claude.get("launchSpacingSeconds") or 20))
    claude["silentSeconds"] = max(60, int(claude.get("silentSeconds") or 1200))
    claude["noProgressSeconds"] = max(60, int(claude.get("noProgressSeconds") or 1200))
    claude["fatalHoldMinutes"] = max(1, int(claude.get("fatalHoldMinutes") or 30))
    raw_open_terminal = claude.get("openTerminalOnRun", False)
    claude["openTerminalOnRun"] = (
        raw_open_terminal
        if isinstance(raw_open_terminal, bool)
        else str(raw_open_terminal).strip().lower() in {"1", "true", "yes", "on"}
    )
    if claude.get("runMode") not in {"supervised", "print"}:
        claude["runMode"] = "supervised"
    return config


def save_config(config: dict[str, Any]) -> None:
    write_json(CONFIG_PATH, config, mode=0o600)


def config_get(config: dict[str, Any], dotted: str) -> Any:
    node: Any = config
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def config_set(config: dict[str, Any], dotted: str, value: str) -> None:
    parts = dotted.split(".")
    node = config
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    old = node.get(parts[-1])
    if isinstance(old, bool):
        node[parts[-1]] = value.lower() in {"1", "true", "yes"}
    elif isinstance(old, int):
        node[parts[-1]] = int(value)
    else:
        node[parts[-1]] = value


def mask_secret(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: (_mask_any(v) if _is_secret_key(k) else mask_secret(v)) for k, v in value.items()}
    return value


def _is_secret_key(key: str) -> bool:
    k = key.lower()
    return any(word in k for word in ("key", "token", "password", "secret", "cookie"))


def _mask_any(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: _mask_any(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_mask_any(x) for x in v]
    return _mask(v)


def _mask(v: Any) -> str:
    s = str(v or "")
    return "" if not s else (s[:4] + "***" + s[-2:] if len(s) > 8 else "***")


def api_key(config: dict[str, Any]) -> str:
    key = os.environ.get("SOLO2611_API_KEY", "").strip() or str(config_get(config, "claude.apiKey") or "").strip()
    if not key:
        raise Solo2611Error(
            "缺少本期团队 API Key：运行 `python3 scripts/solo2611.py config set claude.apiKey=<KEY>`"
            "（或设置环境变量 SOLO2611_API_KEY）。旧 sologsb 的 Key 在新网关会返回 401。"
        )
    return key


def base_url(config: dict[str, Any]) -> str:
    return (os.environ.get("SOLO2611_BASE_URL", "").strip() or str(config_get(config, "claude.baseUrl") or "").strip()).rstrip("/")


def run(cmd: list[str], *, cwd: Path | None = None, check: bool = True, env: dict[str, str] | None = None,
        timeout: float | None = None, input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env, input=input_bytes,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
    if check and proc.returncode != 0:
        err = (proc.stderr or proc.stdout).decode("utf-8", errors="replace").strip()
        raise Solo2611Error(f"命令失败 ({proc.returncode}): {' '.join(cmd)}\n{err}")
    return proc


def out(proc: subprocess.CompletedProcess) -> str:
    return proc.stdout.decode("utf-8", errors="replace").strip()


def git(repo: Path, *args: str, check: bool = True) -> str:
    return out(run(["git", "-C", str(repo), *args], check=check))


def git_identity_args(config: dict[str, Any]) -> list[str]:
    name = str(config_get(config, "git.userName") or "solo-2611")
    email = str(config_get(config, "git.userEmail") or "solo-2611@local")
    return ["-c", f"user.name={name}", "-c", f"user.email={email}", "-c", "commit.gpgsign=false"]


def is_full_sha(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{40}", value or ""))


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^0-9A-Za-z一-鿿_-]+", "-", value).strip("-")
    return slug[:48] or "task"


def container_slug(value: str) -> str:
    """Docker 容器名只允许 ASCII；中文任务名使用稳定短哈希兜底。"""
    slug = re.sub(r"[^0-9A-Za-z_-]+", "-", value or "").strip("-_").lower()
    if not slug:
        slug = "task-" + hashlib.sha1((value or "").encode("utf-8")).hexdigest()[:8]
    return slug[:24]


# ---------------------------------------------------------------- 任务状态

def state_path(task_root: Path) -> Path:
    return task_root / "monitor" / "state.json"


def load_state(task_root: Path) -> dict[str, Any]:
    path = state_path(task_root)
    lock_path = task_root / "monitor" / "state.lock"
    state: Any = None
    for attempt in range(3):
        with file_lock(lock_path):
            try:
                state = read_json(path)
            except (OSError, json.JSONDecodeError):
                state = None
        if isinstance(state, dict):
            break
        if attempt < 2:
            time.sleep(0.05)
    if not isinstance(state, dict):
        raise Solo2611Error(f"不是 solo-2611 任务目录（缺少 monitor/state.json）: {task_root}")
    return state


def save_state(task_root: Path, state: dict[str, Any]) -> None:
    state["updatedAt"] = utc_now()
    with file_lock(task_root / "monitor" / "state.lock"):
        write_json(state_path(task_root), state)


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def non_space_len(text: str) -> int:
    return len(re.sub(r"\s+", "", text or ""))

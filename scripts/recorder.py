"""运行录屏：ScreenCaptureKit 按窗口 ID 采集，转码为 720p mp4 并留下隔离证据。"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from common import CACHE_DIR, STATE_HOME, Solo2611Error, file_lock, out, read_json, run, utc_now, write_json

SWIFT_SOURCE = Path(__file__).resolve().parent / "screencapturekit_window_recorder.swift"
MAX_SECONDS = 90.0
VIDEO_NAME = "运行录屏.mp4"
TERMINAL_BUNDLE_ID = "com.apple.Terminal"
CHROME_BUNDLE_ID = "com.google.Chrome"
ALLOWED_WINDOW_BUNDLES = {TERMINAL_BUNDLE_ID, CHROME_BUNDLE_ID}
RECORDING_LOCK = STATE_HOME / "run" / "recording.lock"
RECORDING_LOCK_GUARD = STATE_HOME / "run" / "recording.lock.guard"
CHROME_BINARY = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

PLAN_TEMPLATE: dict[str, Any] = {
    "schemaVersion": 1,
    "mode": "terminal",
    "targetApps": ["Terminal"],
    "captureKind": "window-id",
    "pointerStrategy": "none",
    "buildCommands": [],
    "preflightCommands": [],
    "startCommand": "",
    "commands": [],
    "pageSteps": [],
    "chromeUrl": "",
    "pageCommand": "",
    "cleanupCommands": [],
    "expectedAppFailure": False,
    "notes": "只写本地测试数据；启动服务、重置数据放 preflightCommands；收尾必须停止本次服务。",
}


def ensure_binary() -> Path:
    if platform.system() != "Darwin":
        raise Solo2611Error("窗口录屏只支持 macOS；其它系统请自行录制后用 `record import` 导入")
    digest = hashlib.sha256(SWIFT_SOURCE.read_bytes()).hexdigest()[:16]
    machine = platform.machine() or "arm64"
    binary = CACHE_DIR / "bin" / f"sck-window-recorder-{machine}-{digest}"
    if binary.is_file() and os.access(binary, os.X_OK):
        return binary
    swiftc = shutil.which("swiftc") or out(run(["xcrun", "--find", "swiftc"], check=False))
    if not swiftc:
        raise Solo2611Error("缺少 swiftc（安装 Xcode Command Line Tools）")
    binary.parent.mkdir(parents=True, exist_ok=True)
    tmp = binary.with_suffix(".tmp")
    run([swiftc, "-O", "-parse-as-library", "-swift-version", "5", "-target", f"{machine}-apple-macosx15.0",
         "-framework", "ScreenCaptureKit", "-framework", "AVFoundation", "-framework", "AppKit",
         "-framework", "CoreGraphics", "-framework", "CoreMedia", str(SWIFT_SOURCE), "-o", str(tmp)], timeout=300)
    os.chmod(tmp, 0o755)
    os.replace(tmp, binary)
    return binary


def plan_template(mode: str = "terminal") -> dict[str, Any]:
    plan = json.loads(json.dumps(PLAN_TEMPLATE, ensure_ascii=False))
    plan["mode"] = mode
    plan["targetApps"] = ["Terminal", "Chrome"] if mode == "web" else ["Terminal"]
    return plan


def write_plan(task_root: Path, mode: str, *, force: bool = False) -> Path:
    if mode not in {"terminal", "web"}:
        raise Solo2611Error("录屏计划 mode 只允许 terminal 或 web")
    path = task_root / "monitor" / "record-plan.json"
    if path.exists() and not force:
        raise Solo2611Error(f"{path} 已存在；确认覆盖时加 --force")
    write_json(path, plan_template(mode))
    return path


def check_plan(path: Path) -> dict[str, Any]:
    plan = read_json(path)
    if not isinstance(plan, dict):
        raise Solo2611Error(f"录屏计划不存在或不是 JSON 对象: {path}")
    errors: list[str] = []
    warnings: list[str] = []
    mode = str(plan.get("mode") or "")
    if mode not in {"terminal", "web"}:
        errors.append("mode 必须是 terminal 或 web")
    expected_apps = {"Terminal", "Chrome"} if mode == "web" else {"Terminal"}
    apps = {str(item) for item in (plan.get("targetApps") or [])}
    if apps != expected_apps:
        errors.append(f"{mode} 模式的 targetApps 必须恰好是 {sorted(expected_apps)}")
    if plan.get("captureKind") != "window-id":
        errors.append("captureKind 必须是 window-id")
    if plan.get("pointerStrategy") != "none":
        errors.append("pointerStrategy 必须是 none，录制器不操作宿主鼠标")
    for key in ("buildCommands", "preflightCommands", "commands", "pageSteps", "cleanupCommands"):
        if not isinstance(plan.get(key), list):
            errors.append(f"{key} 必须是数组")
    if mode == "terminal" and not str(plan.get("startCommand") or "").strip():
        raise Solo2611Error("terminal 计划必须填写 startCommand，并在录制窗口展示真实启动过程")
    if mode == "web":
        warnings.append("Web 题必须分别录制 Terminal.app 和本次新开的独立 Chrome，剪成一段不超过90秒的视频")
        if not str(plan.get("startCommand") or "").strip():
            errors.append("web 计划必须填写 startCommand，并在 Terminal 段展示真实应用启动")
        if not str(plan.get("chromeUrl") or "").strip().startswith("http://127.0.0.1:"):
            errors.append("web 计划必须填写本地回环 chromeUrl")
        if not str(plan.get("pageCommand") or "").strip():
            errors.append("web 计划必须填写 pageCommand，用于录制期间执行页面操作")
        page_steps = [str(item).strip() for item in plan.get("pageSteps") or [] if str(item).strip()]
        unique_page_steps = list(dict.fromkeys(page_steps))
        if len(unique_page_steps) < 2:
            errors.append("web 计划必须填写至少两个不同 pageSteps，并覆盖页面操作与结果")
        else:
            if not any(
                marker in " ".join(unique_page_steps)
                for marker in ("发起", "提交", "保存", "查询", "选择", "点击", "输入", "核对", "看到", "显示")
            ):
                warnings.append("pageSteps 需要写清页面上的业务操作，而不只是进入页面")
            if any(term in " ".join(page_steps).lower() for term in ("http 200", "打开页面", "健康检查")):
                warnings.append("pageSteps 需要包含业务操作和可观察结果，不能只写打开页面或 HTTP 200")
    text = json.dumps(plan, ensure_ascii=False)
    secret_terms = ("apiKey", "Bearer ", "password", "secret", "token=", ".env")
    hits = [term for term in secret_terms if term.lower() in text.lower()]
    if hits:
        errors.append(f"录屏计划疑似包含凭据或密钥标记: {hits}")
    commands = plan.get("commands") if isinstance(plan.get("commands"), list) else []
    leaks = find_sensitive("\n".join([str(plan.get("startCommand") or ""), *map(str, commands)]))
    if leaks:
        errors.append("startCommand/commands 会原样出现在录屏里，不能带本机"
                      + "、".join(dict.fromkeys(hit["kinds"] for hit in leaks))
                      + "；录屏终端已在 workspace 下，改用相对路径: " + leaks[0]["line"])
    if not plan.get("cleanupCommands"):
        warnings.append("cleanupCommands 为空，确认录制结束后手工停掉本次启动的服务")
    result = {"ok": not errors, "errors": errors, "warnings": warnings, "mode": mode, "path": str(path)}
    if errors:
        raise Solo2611Error("录屏计划未通过:\n- " + "\n- ".join(errors))
    return result


def _window_app(window: dict[str, Any]) -> str:
    bundle = str(window.get("bundleId") or "")
    if bundle == TERMINAL_BUNDLE_ID:
        return "Terminal"
    if bundle == CHROME_BUNDLE_ID:
        return "Chrome"
    return str(window.get("app") or "")


# ---------------------------------------------------------------- 隐私门禁
#
# 录屏会交付给外部，画面里不能出现宿主机的登录账号、家目录路径或主机名。
# 泄露点有三处：Terminal 窗口标题、shell 提示符、命令输出里的绝对路径。
# 预防靠 `record terminal` 开的干净终端；这里的检查负责兜底，查到就拒绝，不提供跳过参数。

CLEAN_TERMINAL_TITLE = "solo-2611 录屏"
CLEAN_PROMPT = "%1~ $ "


def _host_names() -> set[str]:
    names = {socket.gethostname().split(".")[0]}
    for key in ("ComputerName", "LocalHostName"):
        try:
            proc = subprocess.run(["scutil", "--get", key], capture_output=True, text=True, timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0:
            names.add(proc.stdout.strip())
    return {name for name in names if name and name.lower() != "localhost"}


def sensitive_terms() -> dict[str, list[str]]:
    """本机不该出现在录屏里的标识，按类别返回。"""
    home = Path.home()
    accounts = {getpass.getuser(), os.environ.get("USER", ""), os.environ.get("LOGNAME", ""), home.name}
    return {
        "登录账号": sorted(name for name in accounts if len(name) >= 2),
        "家目录路径": [str(home)],
        "主机名": sorted(_host_names()),
    }


def _term_pattern(kind: str, term: str) -> re.Pattern[str]:
    escaped = re.escape(term)
    if kind == "家目录路径":
        return re.compile(escaped)
    if kind == "主机名" and len(term) < 4:
        # 两三个字母的主机名（如 Pi）会撞上普通单词，只认 user@host 这种写法
        return re.compile(rf"@{escaped}(?![A-Za-z0-9_-])", re.IGNORECASE)
    return re.compile(rf"(?<![A-Za-z0-9_-]){escaped}(?![A-Za-z0-9_-])", re.IGNORECASE)


def find_sensitive(text: str, terms: dict[str, list[str]] | None = None) -> list[dict[str, str]]:
    """返回命中的行。行内的敏感内容已替换成 ***，可以直接展示。"""
    terms = sensitive_terms() if terms is None else terms
    patterns = [(kind, _term_pattern(kind, term)) for kind, items in terms.items() for term in items]
    hits: list[dict[str, str]] = []
    for line in text.splitlines():
        kinds = [kind for kind, pattern in patterns if pattern.search(line)]
        if not kinds:
            continue
        masked = line
        for _, pattern in patterns:
            masked = pattern.sub("***", masked)
        hits.append({"kinds": "、".join(dict.fromkeys(kinds)), "line": masked.strip()[:160]})
    return hits


def _privacy_error(where: str, hits: list[dict[str, str]], advice: str) -> Solo2611Error:
    kinds = "、".join(dict.fromkeys(k for hit in hits for k in hit["kinds"].split("、")))
    sample = "\n".join(f"  {hit['line']}" for hit in hits[:3])
    more = f"\n  …共 {len(hits)} 行" if len(hits) > 3 else ""
    return Solo2611Error(f"{where}出现本机{kinds}，录屏不能带这些信息：\n{sample}{more}\n{advice}")


def terminal_text(window_id: int) -> str:
    """读 Terminal 窗口当前标签页的全部内容（含 scrollback）。不激活、不置前窗口。"""
    script = f'tell application "Terminal" to get history of selected tab of (first window whose id is {int(window_id)})'
    try:
        proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Solo2611Error(f"读取 Terminal 窗口内容失败，无法确认录屏不含本机账号信息：{exc}") from exc
    if proc.returncode != 0:
        raise Solo2611Error(
            "读取 Terminal 窗口内容失败，无法确认录屏不含本机账号信息"
            "（需在 系统设置 → 隐私与安全性 → 自动化 中允许当前程序控制「终端」）：\n"
            + (proc.stderr or proc.stdout).strip()[-400:]
        )
    return proc.stdout


def check_window_privacy(window: dict[str, Any], *, phase: str) -> dict[str, Any]:
    """phase=start：开录前标题和已有内容都必须干净；phase=stop：录下来的内容必须干净。"""
    app = _window_app(window)
    advice = "用 `record terminal --task-root ROOT` 打开干净的录屏终端后重录。"
    title_hits = find_sensitive(str(window.get("title") or ""))
    if title_hits and phase == "start":
        raise _privacy_error("窗口标题", title_hits, advice)
    content_checked = False
    if app == "Terminal":
        hits = find_sensitive(terminal_text(int(window["windowId"])))
        content_checked = True
        if hits and phase == "start":
            raise _privacy_error(
                "Terminal 窗口现有内容", hits,
                advice + "若坚持用当前窗口，先改掉提示符并执行 `clear && printf '\\e[3J'` 清空 scrollback。",
            )
        if hits:
            raise _privacy_error(
                "录制期间 Terminal 窗口", hits,
                "这段录屏已作废。改用相对路径或不打印绝对路径的命令（如 pytest -q）后重录。",
            )
    return {"status": "ok", "phase": phase, "titleChecked": True, "contentChecked": content_checked,
            "checkedAt": utc_now()}


def open_clean_terminal(task_root: Path, workspace: Path) -> dict[str, Any]:
    """新开一个不带账号、主机名和个人 shell 配置的 Terminal 窗口，专供录屏。"""
    if not workspace.is_dir():
        raise Solo2611Error(f"workspace 不存在: {workspace}")
    session = Path(tempfile.mkdtemp(prefix="solo2611-rec-", dir="/tmp"))
    # 不读 /etc/zshrc：它会把提示符设成 user@host，并向标题栏上报工作目录
    (session / ".zshenv").write_text("unsetopt GLOBAL_RCS\n", encoding="utf-8")
    (session / ".zshrc").write_text(
        f"PROMPT={shlex.quote(CLEAN_PROMPT)}\nRPROMPT=''\nunset HISTFILE\n"
        "precmd_functions=()\nchpwd_functions=()\npreexec_functions=()\n",
        encoding="utf-8",
    )
    enter = session / "enter.sh"
    # 先在用户自己的登录 shell 里起步，继承完整 PATH；随后换成干净 shell 并清掉 scrollback
    enter.write_text(
        f"cd {shlex.quote(str(workspace))} || exit 1\n"
        f"export ZDOTDIR={shlex.quote(str(session))}\n"
        # 用户原 shell 已把账号、主机名和家目录写进标题栏：清空工作目录(OSC 7)，重写窗口/标签标题(OSC 2/1)
        f"printf '\\033]7;\\007\\033]2;%s\\007\\033]1;%s\\007' {shlex.quote(CLEAN_TERMINAL_TITLE)} {shlex.quote(workspace.name)}\n"
        "printf '\\033[2J\\033[3J\\033[H'\n"
        "exec /bin/zsh -i\n",
        encoding="utf-8",
    )
    script = f"""
tell application "Terminal"
    set t to do script "exec /bin/zsh {enter}"
    set w to first window whose selected tab is t
    set bounds of w to {{80, 80, 1360, 800}}
    return id of w
end tell
"""
    proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=30, check=False)
    if proc.returncode != 0 or not proc.stdout.strip().isdigit():
        shutil.rmtree(session, ignore_errors=True)
        raise Solo2611Error("打开录屏终端失败（需允许当前程序控制「终端」）：\n" + (proc.stderr or proc.stdout).strip()[-400:])
    window_id = int(proc.stdout.strip())
    window: dict[str, Any] = {}
    privacy: dict[str, Any] = {}
    last_error: Solo2611Error | None = None
    deadline = time.time() + 8
    while time.time() < deadline:  # 等登录 shell 换成干净 shell、清屏完成
        time.sleep(0.6)
        window = next((w for w in list_windows() if int(w.get("windowId") or 0) == window_id), {})
        if not window:
            continue
        try:
            privacy = check_window_privacy(window, phase="start")
            last_error = None
            break
        except Solo2611Error as exc:
            last_error = exc
    if not window:
        raise Solo2611Error(f"录屏终端已打开（窗口 {window_id}），但窗口列表里找不到它，用 `record list` 确认")
    if last_error:
        raise last_error
    info = {"ok": True, "windowId": window_id, "title": window.get("title", ""), "sessionDir": str(session),
            "workspace": str(workspace), "prompt": CLEAN_PROMPT, "privacy": privacy, "openedAt": utc_now(),
            "next": f"在这个窗口里执行计划中的命令：record start --window-id {window_id}"}
    write_json(task_root / "monitor" / "record-terminal.json", info)
    return info


def _osascript(script: str, *, timeout: float = 15) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout, check=False)


def _terminal_type(window_id: int, command: str) -> None:
    """在录屏终端里敲一条命令。不激活、不置前窗口。"""
    escaped = command.replace("\\", "\\\\").replace('"', '\\"')
    proc = _osascript(
        f'tell application "Terminal" to do script "{escaped}" in selected tab of (first window whose id is {int(window_id)})'
    )
    if proc.returncode != 0:
        raise Solo2611Error("向录屏终端发送命令失败：" + (proc.stderr or proc.stdout).strip()[-300:])


def _terminal_idle(window_id: int, prompt: str) -> bool:
    lines = [line for line in terminal_text(window_id).splitlines() if line.strip()]
    return bool(lines) and lines[-1].strip() == prompt.strip()


def _close_terminal(window_id: int, session_dir: str) -> None:
    tty = _osascript(f'tell application "Terminal" to get tty of selected tab of (first window whose id is {int(window_id)})')
    if tty.returncode == 0 and tty.stdout.strip().startswith("/dev/"):
        # 先结束这个终端里的进程，关窗时才不会弹「是否终止进程」的确认框
        subprocess.run(["pkill", "-t", tty.stdout.strip().removeprefix("/dev/")], capture_output=True, check=False)
        time.sleep(0.5)
    _osascript(f'tell application "Terminal" to close (first window whose id is {int(window_id)}) saving no')
    if session_dir.startswith("/tmp/solo2611-rec-"):
        shutil.rmtree(session_dir, ignore_errors=True)


def _terminal_window_exists(window_id: int) -> bool:
    """用 Terminal 自身计数窗口，避免 ScreenCaptureKit 枚举偶发超时中断清理。"""
    proc = _osascript(
        f'tell application "Terminal" to count (every window whose id is {int(window_id)})'
    )
    if proc.returncode != 0:
        return True
    try:
        return int(proc.stdout.strip()) > 0
    except ValueError:
        return True


def close_terminal_session(task_root: Path) -> dict[str, Any]:
    """关闭本题录屏终端并留下清理证据；Web 录屏必须在 Chrome 段前调用。"""
    info = read_json(task_root / "monitor" / "record-terminal.json", {}) or {}
    if not info:
        raise Solo2611Error("没有本题的录屏终端记录")
    window_id = int(info.get("windowId") or 0)
    session_dir = str(info.get("sessionDir") or "")
    window_exists = _terminal_window_exists(window_id)
    if window_exists:
        _close_terminal(window_id, session_dir)
    elif session_dir.startswith("/tmp/solo2611-rec-"):
        shutil.rmtree(session_dir, ignore_errors=True)
    # Terminal 关闭窗口是异步的；立即枚举容易把刚发出的 close 误判为失败。
    window_exists_after = window_exists
    deadline = time.time() + 5
    while time.time() < deadline:
        window_exists_after = _terminal_window_exists(window_id)
        if not window_exists_after:
            break
        time.sleep(0.25)
    if session_dir.startswith("/tmp/solo2611-rec-"):
        shutil.rmtree(session_dir, ignore_errors=True)
    session_exists_after = bool(session_dir) and Path(session_dir).exists()
    report = {
        "status": "removed" if not window_exists_after and not session_exists_after else "failed",
        "windowId": window_id,
        "sessionDir": session_dir,
        "windowExistedBefore": window_exists,
        "windowExistsAfter": window_exists_after,
        "sessionExistsAfter": session_exists_after,
        "closedAt": utc_now(),
    }
    write_json(task_root / "monitor" / "terminal-cleanup.json", report)
    return report


def _run_offscreen(commands: list[Any], workspace: Path, label: str, *, must_pass: bool) -> None:
    for command in commands:
        proc = subprocess.run(str(command), shell=True, cwd=workspace, capture_output=True, text=True,
                              timeout=900, check=False)
        if proc.returncode != 0 and must_pass:
            raise Solo2611Error(f"{label} 失败（exit {proc.returncode}）: {command}\n{(proc.stderr or proc.stdout)[-800:]}")


def run_plan(task_root: Path, workspace: Path, plan_path: Path, *, max_seconds: float = MAX_SECONDS) -> dict[str, Any]:
    """无人值守录制 Terminal 段：开干净终端 → 开录 → 逐条敲计划命令 → 停止并过隐私门禁 → 收尾。

    计划里的每条命令都要能自己结束；常驻服务放 preflightCommands，或在命令末尾加 & 放后台。
    """
    plan_check = check_plan(plan_path)
    if plan_check["mode"] != "terminal":
        raise Solo2611Error("record auto 只录 Terminal 段；Web 题按 references/recording.md 分段录制后 import")
    plan = read_json(plan_path, {}) or {}
    shown = [str(plan.get("startCommand") or "").strip(), *(str(item).strip() for item in plan.get("commands") or [])]
    shown = [command for command in shown if command]
    _run_offscreen(plan.get("buildCommands") or [], workspace, "buildCommands", must_pass=True)
    _run_offscreen(plan.get("preflightCommands") or [], workspace, "preflightCommands", must_pass=True)
    terminal = open_clean_terminal(task_root, workspace)
    window_id = int(terminal["windowId"])
    prompt = f"{workspace.name} $"
    recording = False
    try:
        window = next(w for w in list_windows() if int(w.get("windowId") or 0) == window_id)
        start(task_root, window, max_seconds=max_seconds)
        recording = True
        deadline = time.time() + max_seconds - 6  # 留出停止和落盘的时间
        time.sleep(1.0)
        typed = 0
        for command in shown:
            if time.time() >= deadline:
                break
            _terminal_type(window_id, command)
            typed += 1
            time.sleep(0.8)
            while time.time() < deadline and not _terminal_idle(window_id, prompt):
                time.sleep(0.5)
        time.sleep(1.5)  # 让最后一屏输出停留在画面里
        recording = False
        meta = stop(task_root)
        meta.update({"recordPlan": plan_check["path"], "recordPlanMode": plan_check["mode"], "automation": {
            "mode": "record-auto", "commandsPlanned": len(shown), "commandsTyped": typed,
            "expectedAppFailure": bool(plan.get("expectedAppFailure"))}})
        write_json(_meta_path(task_root), meta)
        if typed < len(shown):
            raise Solo2611Error(f"90 秒内只执行了 {typed}/{len(shown)} 条命令，录屏不完整。精简 commands 或把耗时步骤挪到 preflightCommands 后重录")
        return meta
    finally:
        if recording:
            try:
                stop(task_root)
            except Solo2611Error:
                pass
        _run_offscreen(plan.get("cleanupCommands") or [], workspace, "cleanupCommands", must_pass=False)
        close_terminal_session(task_root)


def validate_window(window: dict[str, Any]) -> dict[str, Any]:
    window_id = int(window.get("windowId") or 0)
    if window_id <= 0:
        raise Solo2611Error("录制窗口必须带正整数 CGWindowID")
    bundle = str(window.get("bundleId") or "")
    if bundle not in ALLOWED_WINDOW_BUNDLES:
        raise Solo2611Error(f"只允许录制 Terminal.app 或 Google Chrome 窗口，当前 bundleId={bundle!r}")
    if int(window.get("width") or 0) < 200 or int(window.get("height") or 0) < 150:
        raise Solo2611Error("录制窗口过小，无法展示完整运行结果")
    return window


def _process_rows() -> list[dict[str, Any]]:
    proc = run(["ps", "-axo", "pid=,ppid=,command="], check=False, timeout=20)
    rows: list[dict[str, Any]] = []
    for line in out(proc).splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        rows.append({"pid": pid, "ppid": ppid, "command": parts[2]})
    return rows


def _chrome_rows() -> list[dict[str, Any]]:
    return [
        row for row in _process_rows()
        if "/Google Chrome.app/Contents/MacOS/Google Chrome" in str(row["command"])
        or "/Google Chrome Helper" in str(row["command"])
    ]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _listener_pids(port: int) -> set[int]:
    proc = run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp"], check=False, timeout=10)
    return {
        int(line[1:])
        for line in out(proc).splitlines()
        if line.startswith("p") and line[1:].isdigit()
    }


def _wait_cdp(port: int, timeout: float = 30) -> dict[str, Any]:
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            time.sleep(0.4)
    raise Solo2611Error(f"独立 Chrome 的调试端口 {port} 未就绪: {last_error}")


def start_chrome(task_root: Path, url: str) -> dict[str, Any]:
    """启动只属于本任务的临时 Chrome，不连接或关闭用户已有实例。"""
    if platform.system() != "Darwin":
        raise Solo2611Error("独立 Chrome 启动只支持 macOS")
    if not CHROME_BINARY.is_file():
        raise Solo2611Error(f"找不到 Google Chrome: {CHROME_BINARY}")
    if not url.startswith("http://127.0.0.1:") and not url.startswith("http://localhost:"):
        raise Solo2611Error("录屏 Chrome 只允许访问本机回环地址上的被测应用")
    baseline_rows = _chrome_rows()
    baseline_pids = sorted({int(row["pid"]) for row in baseline_rows})
    baseline_windows = {
        int(window["windowId"])
        for window in list_windows()
        if str(window.get("bundleId") or "") == CHROME_BUNDLE_ID
    }
    profile_root = CACHE_DIR / "chrome-profiles"
    profile_root.mkdir(parents=True, exist_ok=True)
    profile = Path(tempfile.mkdtemp(prefix="solo-2611-", dir=profile_root))
    default_profile = profile / "Default"
    default_profile.mkdir(parents=True, exist_ok=True)
    write_json(
        default_profile / "Preferences",
        {
            "credentials_enable_service": False,
            "credentials_enable_autosignin": False,
            "profile": {"password_manager_enabled": False},
        },
    )
    port = _free_port()
    record_dir = task_root / "recording"
    record_dir.mkdir(parents=True, exist_ok=True)
    log = (record_dir / "chrome.log").open("wb")
    args = [
        f"--user-data-dir={profile}",
        f"--remote-debugging-port={port}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--disable-notifications",
        "--disable-popup-blocking",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-features=Translate,MediaRouter,OptimizationHints,CalculateNativeWinOcclusion",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-background-timer-throttling",
        "--password-store=basic",
        "--use-mock-keychain",
        "--disable-save-password-bubble",
        "--window-size=1440,810",
        url,
    ]
    command = ["open", "-g", "-n", "-a", "Google Chrome", "--args", *args]
    proc = subprocess.run(command, stdout=log, stderr=log, check=False)
    if proc.returncode != 0:
        shutil.rmtree(profile, ignore_errors=True)
        raise Solo2611Error("后台启动独立 Chrome 失败，见 recording/chrome.log")
    try:
        _wait_cdp(port)
        deadline = time.time() + 25
        owned_rows: list[dict[str, Any]] = []
        window: dict[str, Any] | None = None
        while time.time() < deadline:
            owned_rows = [
                row for row in _chrome_rows()
                if str(profile) in str(row["command"]) and int(row["pid"]) not in baseline_pids
            ]
            owned_pids = {int(row["pid"]) for row in owned_rows}
            candidates = [
                item for item in list_windows()
                if str(item.get("bundleId") or "") == CHROME_BUNDLE_ID
                and int(item.get("pid") or 0) in owned_pids
                and int(item.get("windowId") or 0) not in baseline_windows
            ]
            if owned_rows and candidates:
                window = validate_window(sorted(candidates, key=lambda item: int(item["windowId"]))[-1])
                break
            time.sleep(0.4)
        if not window:
            raise Solo2611Error("独立 Chrome 已启动，但没找到属于本次临时 profile 的窗口")
        owned_pids = {int(row["pid"]) for row in owned_rows}
        listener_pids = _listener_pids(port)
        debug_port_owner_verified = bool(listener_pids & owned_pids)
        if not debug_port_owner_verified:
            raise Solo2611Error("无法确认调试端口监听进程属于本次独立 Chrome")
        root_rows = [
            row for row in owned_rows
            if f"--remote-debugging-port={port}" in str(row["command"])
            and str(profile) in str(row["command"])
        ]
        recording_pid = int(root_rows[0]["pid"]) if root_rows else min(owned_pids)
        instance_path = task_root / "recording" / "chrome-instance.json"
        session = {
            "schemaVersion": 1,
            "status": "ok",
            "dedicatedInstance": True,
            "reusedRunningChrome": False,
            "userChromeTouched": False,
            "profileCommandLineVerified": True,
            "debugPortOwnerVerified": debug_port_owner_verified,
            "preExistingChromePids": baseline_pids,
            "preExistingChromeWindowIds": sorted(baseline_windows),
            "recordingChromeProcesses": owned_rows,
            "recordingChromePid": recording_pid,
            "debugPortOwnerPids": sorted(listener_pids),
            "debugPort": port,
            "userDataDir": str(profile),
            "url": url,
            "windowId": int(window["windowId"]),
            "windowOwnerPid": int(window.get("pid") or 0),
            "window": window,
            "createdAt": utc_now(),
            "path": str(instance_path),
        }
        write_json(instance_path, session)
        write_json(task_root / "monitor" / "chrome-session.json", session)
        return session
    except BaseException:
        for row in _chrome_rows():
            if str(profile) in str(row["command"]):
                try:
                    os.kill(int(row["pid"]), signal.SIGTERM)
                except OSError:
                    pass
        shutil.rmtree(profile, ignore_errors=True)
        raise
    finally:
        log.close()


def stop_chrome(task_root: Path) -> dict[str, Any]:
    path = task_root / "monitor" / "chrome-session.json"
    session = read_json(path, {}) or {}
    if not session:
        raise Solo2611Error("没有本次任务的独立 Chrome 会话")
    profile = Path(str(session.get("userDataDir") or ""))
    if not profile.resolve().is_relative_to((CACHE_DIR / "chrome-profiles").resolve()):
        raise Solo2611Error("Chrome 临时 profile 路径不在允许目录内，拒绝清理")
    terminated: list[int] = []
    for row in _chrome_rows():
        if str(profile) not in str(row["command"]):
            continue
        try:
            os.kill(int(row["pid"]), signal.SIGTERM)
            terminated.append(int(row["pid"]))
        except OSError:
            pass
    deadline = time.time() + 5
    while time.time() < deadline and any(
        str(profile) in str(row["command"]) for row in _chrome_rows()
    ):
        time.sleep(0.2)
    for row in _chrome_rows():
        if str(profile) in str(row["command"]):
            try:
                os.kill(int(row["pid"]), signal.SIGKILL)
            except OSError:
                pass
    shutil.rmtree(profile, ignore_errors=True)
    report = {
        "status": "removed" if not profile.exists() else "failed",
        "path": str(profile),
        "removed": not profile.exists(),
        "existsAfter": profile.exists(),
        "terminatedProcesses": terminated,
        "finishedAt": utc_now(),
    }
    write_json(task_root / "recording" / "chrome-profile-cleanup.json", report)
    return report


def list_windows() -> list[dict[str, Any]]:
    proc = run([str(ensure_binary()), "--list-windows"], check=False, timeout=30)
    if proc.returncode != 0:
        raise Solo2611Error("读取窗口失败（首次使用需在 系统设置 → 隐私与安全性 → 屏幕录制 中授权当前终端）：\n"
                            + proc.stderr.decode("utf-8", errors="replace"))
    return json.loads(out(proc) or "[]")


def pick_window(app: str = "", title: str = "", window_id: int = 0) -> dict[str, Any]:
    windows = list_windows()
    if window_id:
        for w in windows:
            if int(w["windowId"]) == window_id:
                return validate_window(w)
        raise Solo2611Error(f"找不到窗口 ID {window_id}，用 `record list` 查看当前窗口")
    app_l, title_l = app.lower(), title.lower()
    hits = [w for w in windows
            if (not app_l or app_l in str(w.get("app", "")).lower() or app_l in str(w.get("bundleId", "")).lower())
            and (not title_l or title_l in str(w.get("title", "")).lower())]
    if not hits:
        raise Solo2611Error(f"没有匹配 app={app!r} title={title!r} 的窗口，用 `record list` 查看")
    if len(hits) > 1 and not title:
        names = "; ".join(f"{w['windowId']}:{w['app']}:{w['title'][:30]}" for w in hits[:6])
        raise Solo2611Error(f"匹配到多个窗口，请加 --title 或 --window-id：{names}")
    return validate_window(hits[0])


def _meta_path(task_root: Path) -> Path:
    return task_root / "monitor" / "recording.json"


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        waited, _status = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _acquire_recording_slot(task_root: Path) -> None:
    RECORDING_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(RECORDING_LOCK_GUARD):
        current = read_json(RECORDING_LOCK, {}) or {}
        owner_pid = int(current.get("ownerPid") or 0)
        if owner_pid and _alive(owner_pid) and current.get("taskRoot") != str(task_root):
            raise Solo2611Error(
                f"另一个任务正在录屏：{current.get('taskRoot')}（pid {owner_pid}）。"
                "所有任务共用主机级录屏锁，等它结束后再开始。"
            )
        write_json(RECORDING_LOCK, {
            "status": "starting",
            "ownerPid": os.getpid(),
            "taskRoot": str(task_root),
            "startedAt": utc_now(),
        })


def _promote_recording_slot(task_root: Path, recorder_pid: int) -> None:
    with file_lock(RECORDING_LOCK_GUARD):
        current = read_json(RECORDING_LOCK, {}) or {}
        if current.get("taskRoot") != str(task_root):
            raise Solo2611Error("录屏锁已被其他任务接管")
        write_json(RECORDING_LOCK, current | {
            "status": "recording",
            "ownerPid": recorder_pid,
            "recorderPid": recorder_pid,
            "readyAt": utc_now(),
        })


def _release_recording_slot(task_root: Path, recorder_pid: int = 0) -> None:
    with file_lock(RECORDING_LOCK_GUARD):
        current = read_json(RECORDING_LOCK, {}) or {}
        if current.get("taskRoot") != str(task_root):
            return
        owner = int(current.get("recorderPid") or current.get("ownerPid") or 0)
        if recorder_pid and owner and owner != recorder_pid:
            return
        RECORDING_LOCK.unlink(missing_ok=True)


def start(task_root: Path, window: dict[str, Any], *, max_seconds: float = MAX_SECONDS) -> dict[str, Any]:
    window = validate_window(window)
    privacy = check_window_privacy(window, phase="start")
    if max_seconds <= 0 or max_seconds > MAX_SECONDS:
        raise Solo2611Error(f"录制最长 {MAX_SECONDS:g} 秒；规范不允许剪掉失败片段")
    meta = read_json(_meta_path(task_root), {}) or {}
    pid = int(meta.get("pid") or 0)
    if meta.get("status") == "recording" and pid and _alive(pid):
        raise Solo2611Error(f"已有录屏在进行（pid {pid}），先 `record stop`")
    _acquire_recording_slot(task_root)
    rec_dir = task_root / "recording"
    rec_dir.mkdir(parents=True, exist_ok=True)
    raw = rec_dir / "raw.mov"
    ready = rec_dir / ".ready.json"
    ready.unlink(missing_ok=True)
    log = (rec_dir / "recorder.log").open("wb")
    cmd = [str(ensure_binary()), "--window-id", str(window["windowId"]), "--output", str(raw),
           "--ready-file", str(ready), "--max-seconds", str(max_seconds)]
    try:
        proc = subprocess.Popen(cmd, stdout=log, stderr=log, start_new_session=True)
    except BaseException:
        log.close()
        _release_recording_slot(task_root)
        raise
    log.close()
    deadline = time.time() + 20
    while time.time() < deadline and not ready.is_file():
        if proc.poll() is not None:
            _release_recording_slot(task_root)
            raise Solo2611Error("录制器启动失败，见 " + str(rec_dir / "recorder.log"))
        time.sleep(0.2)
    if not ready.is_file():
        proc.send_signal(signal.SIGINT)
        _release_recording_slot(task_root)
        raise Solo2611Error("录制器 20 秒内未就绪（检查屏幕录制授权）")
    ready_info = read_json(ready, {}) or {}
    try:
        _promote_recording_slot(task_root, proc.pid)
    except BaseException:
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
        _release_recording_slot(task_root)
        raise
    meta = {
        "status": "recording",
        "pid": proc.pid,
        "window": window,
        "raw": str(raw),
        "startedAt": utc_now(),
        "startedEpoch": time.time(),
        "maxSeconds": max_seconds,
        "showsCursor": False,
        "cursorCaptured": False,
        "captureKind": "window-id",
        "captureBackend": "screen-capture-kit",
        "pointerStrategy": "none",
        "pointerPolicy": "host-input-untouched",
        "activationPerformed": False,
        "ready": ready_info,
        "privacy": privacy,
    }
    write_json(_meta_path(task_root), meta)
    return meta


def stop(task_root: Path) -> dict[str, Any]:
    meta = read_json(_meta_path(task_root), {}) or {}
    if meta.get("status") != "recording":
        raise Solo2611Error("当前没有进行中的录屏")
    pid = int(meta.get("pid") or 0)
    if pid and _alive(pid):
        os.kill(pid, signal.SIGINT)
        deadline = time.time() + 40
        while time.time() < deadline and _alive(pid):
            time.sleep(0.3)
        if _alive(pid):
            os.kill(pid, signal.SIGTERM)
            deadline = time.time() + 5
            while time.time() < deadline and _alive(pid):
                time.sleep(0.2)
        if _alive(pid):
            os.kill(pid, signal.SIGKILL)
            _release_recording_slot(task_root, pid)
            raise Solo2611Error("录制器没有在 45 秒内落盘，已强制结束；保留失败日志并重新录制")
    raw = Path(meta["raw"])
    if not raw.is_file() or raw.stat().st_size == 0:
        meta["status"] = "failed"
        write_json(_meta_path(task_root), meta)
        _release_recording_slot(task_root, pid)
        raise Solo2611Error("没有拿到录制文件，见 recording/recorder.log")
    try:
        window = meta.get("window") if isinstance(meta.get("window"), dict) else {}
        try:
            meta["privacy"] = check_window_privacy(window, phase="stop")
        except Solo2611Error:
            # 带账号信息的片段不能留在任务目录里，更不能转码成交付视频
            raw.unlink(missing_ok=True)
            meta["status"] = "failed"
            meta["privacy"] = {"status": "failed", "phase": "stop", "checkedAt": utc_now()}
            write_json(_meta_path(task_root), meta)
            raise
        return finalize(task_root, raw, meta | {"source": "screen-capture-kit"})
    finally:
        _release_recording_slot(task_root, pid)


def probe(video: Path) -> dict[str, Any]:
    proc = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height:format=duration",
                "-of", "json", str(video)])
    data = json.loads(out(proc))
    stream = (data.get("streams") or [{}])[0]
    return {"width": int(stream.get("width") or 0), "height": int(stream.get("height") or 0),
            "duration": round(float((data.get("format") or {}).get("duration") or 0), 2)}


def finalize(task_root: Path, source: Path, meta: dict[str, Any]) -> dict[str, Any]:
    if not shutil.which("ffmpeg"):
        raise Solo2611Error("缺少 ffmpeg（brew install ffmpeg）")
    src_info = probe(source)
    if src_info["duration"] > MAX_SECONDS + 0.5:
        raise Solo2611Error(f"录屏 {src_info['duration']} 秒超过 90 秒上限。规范禁止剪掉失败片段，请重新录制一段更紧凑的完整运行过程")
    target = task_root / "recording" / VIDEO_NAME
    vf = "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
    run(["ffmpeg", "-y", "-v", "error", "-i", str(source), "-vf", vf, "-r", "30", "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "26", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", str(target)], timeout=600)
    info = probe(target)
    if (info["width"], info["height"]) != (1280, 720):
        raise Solo2611Error(f"转码后分辨率异常: {info}")
    size_mb = round(target.stat().st_size / 1024 / 1024, 2)
    window = meta.get("window") if isinstance(meta.get("window"), dict) else {}
    is_import = meta.get("source") == "import"
    capture_kind = "manual-import" if is_import else "window-id"
    capture_backend = "manual-import" if is_import else "screen-capture-kit"
    window_id = int(window.get("windowId") or 0)
    capture_ok = bool(is_import or (capture_kind == "window-id" and window_id > 0))
    capture = {
        "status": "manual-import" if is_import else ("ok" if capture_ok else "failed"),
        "segment": "final",
        "captureKind": capture_kind,
        "captureBackend": capture_backend,
        "showsCursor": None if is_import else False,
        "cursorCaptured": None if is_import else False,
        "windowId": window_id,
        "ownerPid": int(window.get("pid") or 0),
        "ownerApp": _window_app(window),
        "ownerName": str(window.get("app") or ""),
        "ownerBundleId": str(window.get("bundleId") or ""),
        "bounds": {
            "x": window.get("x"),
            "y": window.get("y"),
            "width": int(window.get("width") or 0),
            "height": int(window.get("height") or 0),
        },
        "source": str(source),
        "output": str(target),
        "exitCode": 0,
        "checkedAt": utc_now(),
    }
    cursor_guard = {
        "status": "manual-import" if is_import else "ok",
        "segment": "final",
        "windowId": window_id,
        "captureKind": capture_kind,
        "pointerStrategy": "none",
        "pointerPolicy": "host-input-untouched",
        "hostInputRespected": None if is_import else True,
        "pointerMoved": None if is_import else False,
        "mouseButtonsQueried": None if is_import else False,
        "parkApplied": None if is_import else False,
        "positionReadOnly": None if is_import else True,
        "finalPointerInsideWindow": None,
    }
    write_json(task_root / "recording" / f"{target.stem}-window-capture.json", capture)
    write_json(task_root / "recording" / f"{target.stem}-cursor-guard.json", cursor_guard)
    meta.update({
        "status": "done",
        "video": str(target),
        "durationSeconds": info["duration"],
        "width": 1280,
        "height": 720,
        "sizeMB": size_mb,
        "finishedAt": utc_now(),
        "needsObjectStorage": size_mb > 10,
        "captureKind": capture_kind,
        "captureBackend": capture_backend,
        "windowCaptureOk": capture_ok,
        "pointerStrategy": "none",
        "activationPerformed": False,
    })
    write_json(_meta_path(task_root), meta)
    return meta

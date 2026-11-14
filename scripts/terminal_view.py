"""Visible macOS Terminal windows attached to managed tmux sessions.

The window is only a view.  It is opened without stealing keyboard focus,
attached read-only by default, tagged with a unique title, and closed again as
soon as its tmux session is gone.  Every helper is best effort: a failure here
must never fail or delay the managed session itself.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

TITLE_PREFIX = "solo2611-view:"
FIELD = "\x1f"
RECORD = "\x1e"
ATTACH_RE = re.compile(
    r"(?:tmux attach(?:-session)?(?: -r)? -t|terminal_view\.py attach-session(?: -r)? --tmux) "
    r"(solo2611-[0-9a-z-]+)"
)
HISTORY_LIMIT = 4000


def supported() -> bool:
    return sys.platform == "darwin"


def window_title(tmux_name: str) -> str:
    return f"{TITLE_PREFIX}{tmux_name}"


def _osascript(script: str, *, timeout: float = 15) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def terminal_running() -> bool:
    """True when Terminal.app is already running; sweeping must never launch it."""
    try:
        result = subprocess.run(
            ["pgrep", "-x", "Terminal"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def attach_command(tmux_name: str, *, read_only: bool = True) -> str:
    """Run a small helper so the visible window closes even if the supervisor dies."""
    script = Path(__file__).resolve()
    python = shutil.which("python3") or sys.executable
    args = [
        python,
        str(script),
        "attach-session",
        "--tmux",
        tmux_name,
    ]
    if read_only:
        args.append("--read-only")
    return "exec " + " ".join(shlex.quote(part) for part in args)


def open_script(tmux_name: str, *, read_only: bool = True, activate: bool = False) -> str:
    command = attach_command(tmux_name, read_only=read_only)
    lines = ['tell application "Terminal"']
    if activate:
        lines.append("  activate")
    lines += [
        f'  set newTab to do script "{_escape(command)}"',
        "  try",
        f'    set custom title of newTab to "{_escape(window_title(tmux_name))}"',
        "  end try",
        '  set tabTty to ""',
        "  try",
        "    set tabTty to tty of newTab",
        "  end try",
        "  set winId to 0",
        "  repeat with w in windows",
        "    try",
        "      if (tty of selected tab of w) is tabTty then set winId to id of w",
        "    end try",
        "  end repeat",
        f'  return (winId as text) & "{FIELD}" & tabTty',
        "end tell",
    ]
    return "\n".join(lines)


def open_window(
    tmux_name: str,
    *,
    read_only: bool = True,
    activate: bool = False,
    attempts: int = 2,
) -> dict[str, Any]:
    """Open one window for the session; retry once because Terminal can be slow to answer."""
    if not supported():
        return {"requested": True, "opened": False, "error": "自动打开可见终端目前只支持 macOS"}
    error = ""
    for number in range(max(1, attempts)):
        try:
            result = _osascript(open_script(tmux_name, read_only=read_only, activate=activate))
        except (OSError, subprocess.SubprocessError) as exc:
            error = f"打开 Terminal.app 失败: {exc}"
        else:
            if result.returncode == 0:
                window_id, _, tty = result.stdout.strip().partition(FIELD)
                return {
                    "requested": True,
                    "opened": True,
                    "app": "Terminal",
                    "tmux": tmux_name,
                    "windowId": int(window_id) if window_id.isdigit() else 0,
                    "tty": tty.strip(),
                    "title": window_title(tmux_name),
                    "readOnly": read_only,
                    "openAttempts": number + 1,
                }
            error = result.stderr.strip()[-500:] or "打开 Terminal.app 失败"
        if number + 1 < max(1, attempts):
            time.sleep(1.5)
    return {"requested": True, "opened": False, "error": error, "openAttempts": max(1, attempts)}


LIST_SCRIPT = f"""
tell application "Terminal"
  set fieldSep to (ASCII character 31)
  set recordSep to (ASCII character 30)
  set output to ""
  repeat with w in windows
    try
      set winId to id of w
      repeat with t in tabs of w
        set tabTitle to ""
        try
          set tabTitle to custom title of t
        end try
        set tabTty to ""
        try
          set tabTty to tty of t
        end try
        set tabBusy to false
        try
          set tabBusy to busy of t
        end try
        set tabHistory to ""
        try
          set tabHistory to history of t
        end try
        if (length of tabHistory) > {HISTORY_LIMIT} then set tabHistory to text 1 thru {HISTORY_LIMIT} of tabHistory
        set output to output & winId & fieldSep & tabTty & fieldSep & tabTitle & fieldSep & tabBusy & fieldSep & tabHistory & recordSep
      end repeat
    end try
  end repeat
  return output
end tell
"""


def list_tabs() -> list[dict[str, Any]]:
    """Every Terminal tab with the tmux session it was opened for, if any."""
    if not supported() or not terminal_running():
        return []
    try:
        result = _osascript(LIST_SCRIPT, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    rows: list[dict[str, Any]] = []
    for record in result.stdout.split(RECORD):
        parts = record.strip("\n").split(FIELD, 4)
        if len(parts) < 5 or not parts[0].strip().isdigit():
            continue
        title = parts[2].strip()
        history = parts[4]
        tmux_name = ""
        source = ""
        if title.startswith(TITLE_PREFIX):
            tmux_name, source = title[len(TITLE_PREFIX):], "title"
        else:
            # Terminal 不一定接受自定义标题，旧版本打开的窗口也没有标记：
            # 这时从标题或启动命令里的 attach 命令认领。
            for text, origin in ((title, "title-command"), (history, "history")):
                match = ATTACH_RE.search(text)
                if match:
                    tmux_name, source = match.group(1), origin
                    break
        rows.append(
            {
                "windowId": int(parts[0].strip()),
                "tty": parts[1].strip(),
                "title": title,
                "busy": parts[3].strip().lower() == "true",
                "tmux": tmux_name,
                "matchedBy": source,
            }
        )
    return rows


def close_script(window_id: int, tty: str) -> str:
    """Close one window, but only while it still owns the tty it was listed with."""
    return "\n".join(
        [
            'tell application "Terminal"',
            "  set closedCount to 0",
            "  repeat with w in windows",
            "    try",
            f"      if (id of w) is {int(window_id)} then",
            "        set owned to false",
            "        repeat with t in tabs of w",
            f'          if (tty of t) is "{_escape(tty)}" then set owned to true',
            "        end repeat",
            "        if owned then",
            "          close w saving no",
            "          set closedCount to closedCount + 1",
            "        end if",
            "      end if",
            "    end try",
            "  end repeat",
            "  return closedCount",
            "end tell",
        ]
    )


def close_tty_script(tty: str) -> str:
    """Close the Terminal window that owns this exact tty."""
    return "\n".join(
        [
            'tell application "Terminal"',
            "  set closedCount to 0",
            "  repeat with w in windows",
            "    try",
            "      set owned to false",
            "      repeat with t in tabs of w",
            f'        if (tty of t) is "{_escape(tty)}" then set owned to true',
            "      end repeat",
            "      if owned then",
            "        close w saving no",
            "        set closedCount to closedCount + 1",
            "      end if",
            "    end try",
            "  end repeat",
            "  return closedCount",
            "end tell",
        ]
    )


def _close_row(row: dict[str, Any]) -> bool:
    try:
        result = _osascript(close_script(int(row["windowId"]), str(row["tty"])))
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() not in {"", "0"}


def _close_current_tty() -> bool:
    """Close the Terminal view after tmux exits; never affect another tty."""
    if not supported():
        return False
    try:
        tty = tty_name()
    except OSError:
        return False
    if not tty:
        return False
    try:
        result = _osascript(close_tty_script(tty), timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() not in {"", "0"}


def _close_session_view(tmux_name: str) -> int:
    """Fallback: close the exact managed view even when stdin lost its tty path."""
    closed = 0
    for row in list_tabs():
        if row.get("tmux") == tmux_name and _close_row(row):
            closed += 1
    return closed


def tty_name(fd: int = 0) -> str:
    """Return the controlling terminal path without shelling out."""
    try:
        return os.ttyname(fd)
    except OSError:
        return ""


def attach_session(tmux_name: str, *, read_only: bool = True) -> int:
    """Attach in the foreground and close this Terminal view when the session ends."""
    command = ["tmux", "attach-session"]
    if read_only:
        command.append("-r")
    command.extend(["-t", tmux_name])
    code = 1
    try:
        result = subprocess.run(command, check=False)
        code = int(result.returncode)
    finally:
        # tmux can disappear because Claude exited, abort cleaned the session,
        # or the supervisor crashed. In every case this disposable view should go.
        if not _close_current_tty():
            _close_session_view(tmux_name)
    return code


def tmux_alive(tmux_name: str) -> bool:
    """True while the tmux session still exists (or tmux cannot be asked)."""
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", f"={tmux_name}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except FileNotFoundError:
        return False
    except (OSError, subprocess.SubprocessError):
        return True
    return result.returncode == 0


def close_window(
    tmux_name: str,
    *,
    alive: Callable[[str], bool] | None = None,
    wait_seconds: float = 4.0,
) -> dict[str, Any]:
    """Close the view of one finished session.

    Terminal's ``busy`` flag is unreliable for an ``exec``-ed attach command, so
    the tmux session itself is the source of truth: while it is alive the
    window is left alone.
    """
    if not supported() or not tmux_name or not terminal_running():
        return {"closed": 0, "skipped": 0}
    check = alive or tmux_alive
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while check(tmux_name):
        if time.monotonic() >= deadline:
            rows = [row for row in list_tabs() if row["tmux"] == tmux_name]
            return {"closed": 0, "skipped": len(rows), "tmux": tmux_name, "reason": "session_alive"}
        time.sleep(0.5)
    closed = 0
    # attach 退出后窗口可能还要一瞬间才能被列出/关闭，补一次重试。
    for _ in range(2):
        rows = [row for row in list_tabs() if row["tmux"] == tmux_name]
        if not rows:
            break
        # A tab created for one of our tmux views is ours even if Terminal keeps
        # its stale ``busy`` flag after ``exec tmux attach`` returns. History-only
        # matches are more conservative because the tab may already be reused.
        pending = [
            row
            for row in rows
            if not row["busy"] or row.get("matchedBy") in {"title", "title-command"}
        ]
        if not pending:
            return {"closed": closed, "skipped": len(rows), "tmux": tmux_name, "reason": "window_busy"}
        for row in pending:
            if _close_row(row):
                closed += 1
        time.sleep(0.5)
    return {"closed": closed, "skipped": 0, "tmux": tmux_name}


def sweep(alive: Callable[[str], bool]) -> dict[str, Any]:
    """Close every leftover view whose tmux session no longer exists."""
    if not supported() or not terminal_running():
        return {"closed": 0, "kept": 0, "sessions": []}
    closed = kept = 0
    sessions: list[str] = []
    for row in list_tabs():
        name = str(row["tmux"])
        if not name:
            continue
        # Our titled/command-claimed view is disposable after its tmux session
        # ends, even when Terminal still reports the unreliable busy flag.
        # History-only matches stay protected because that tab may be reused.
        disposable = row.get("matchedBy") in {"title", "title-command"}
        if alive(name) or (row["busy"] and not disposable):
            kept += 1
            continue
        if _close_row(row):
            closed += 1
            sessions.append(name)
    return {"closed": closed, "kept": kept, "sessions": sessions}


if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] != "attach-session":
        raise SystemExit("usage: terminal_view.py attach-session --tmux NAME [--read-only]")
    session_name = ""
    index = 2
    while index < len(sys.argv):
        value = sys.argv[index]
        if value == "--tmux" and index + 1 < len(sys.argv):
            session_name = sys.argv[index + 1]
            index += 2
            continue
        if value == "--read-only":
            index += 1
            continue
        raise SystemExit(f"unknown argument: {value}")
    if not session_name:
        raise SystemExit("missing --tmux")
    raise SystemExit(attach_session(session_name, read_only="--read-only" in sys.argv))

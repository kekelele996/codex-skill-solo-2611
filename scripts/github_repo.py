"""GitHub 快照仓库：每题新建一个公开仓库，初始快照推到 main，产物快照随后快进推送。

Token 读 github.accessTokens（或环境变量 GITHUB_TOKEN），只经 HTTP 头和 GIT_CONFIG_* 环境变量传递，
不写进远端 URL、不写进 workspace 的 .git/config；workspace 也不加 origin，避免模型在容器里看到远端。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import re
import string
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from common import Solo2611Error, config_get, is_full_sha, run, save_state, utc_now

API = "https://api.github.com"

_REPO_TERMS = {
    "数据脱敏": "data-masking",
    "k-匿名": "k-anonymity",
    "合规": "compliance",
    "特征平台": "feature-platform",
    "点时间": "point-in-time",
    "正确性": "correctness",
    "列级": "column-level",
    "影响分析": "impact-analysis",
    "日志模板": "log-template",
    "结构化": "structured",
    "指标": "metric",
    "管道": "pipeline",
    "数据质量": "data-quality",
    "剖析": "profiling",
    "规则": "rules",
    "治理": "governance",
    "数据湖": "data-lake",
    "小文件": "small-file",
    "分区": "partition",
    "整理": "organize",
    "工坊": "workshop",
    "实体解析": "entity-resolution",
    "主数据": "master-data",
    "数据目录": "data-catalog",
    "列语义": "column-semantics",
    "重复资产": "duplicate-assets",
    "推荐": "recommendation",
    "变更流": "change-stream",
    "增量": "incremental",
    "演进": "evolution",
    "工业": "industrial",
    "时序": "time-series",
    "遥测": "telemetry",
    "对齐": "alignment",
    "缺口": "gap",
    "修复": "repair",
    "一致性": "consistency",
    "注册": "registry",
    "口径": "caliber",
    "检查": "check",
    "中心": "center",
    "数据": "data",
    "质量": "quality",
    "血缘": "lineage",
    "合并": "merge",
    "分析": "analysis",
    "应用": "app",
    "管理": "management",
    "同步": "sync",
    "对账": "reconciliation",
    "清洗": "cleaning",
    "查询": "query",
    "推断": "inference",
    "识别": "recognition",
    "生成": "generation",
    "安全": "security",
    "认证": "auth",
    "权限": "permission",
    "加密": "encryption",
    "漏洞": "vulnerability",
    "攻防": "offense-defense",
    "网络": "network",
    "通信": "communication",
    "系统": "system",
    "图像": "image",
    "视频": "video",
    "音频": "audio",
    "渲染": "rendering",
    "引擎": "engine",
    "游戏": "game",
    "平台": "platform",
    "工作台": "workbench",
    "台": "platform",
    "服务": "service",
    "接口": "api",
    "导入": "import",
    "导出": "export",
    "回填": "backfill",
    "挖掘": "mining",
    "与": " ",
    "及": " ",
    "和": " ",
    "的": " ",
}


def _transliterate(value: str) -> str:
    if not re.search(r"[^\x00-\x7f]", value):
        return value
    script = """
import Foundation
let input = String(data: FileHandle.standardInput.readDataToEndOfFile(), encoding: .utf8) ?? ""
let latin = input.applyingTransform(.toLatin, reverse: false) ?? input
let plain = latin.applyingTransform(.stripDiacritics, reverse: false) ?? latin
print(plain)
"""
    try:
        proc = subprocess.run(
            ["swift", "-e", script],
            input=value.encode("utf-8"),
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.decode("utf-8", errors="replace").strip()


def repo_slug(value: str) -> str:
    """Return a readable GitHub-safe slug; never fall back to an empty prefix."""
    text = str(value or "").strip()
    for source, target in sorted(_REPO_TERMS.items(), key=lambda item: len(item[0]), reverse=True):
        text = text.replace(source, f" {target} ")
    text = _transliterate(text)
    slug = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    slug = re.sub(r"-{2,}", "-", slug)
    compact = re.sub(r"[^a-z0-9]", "", slug)
    if len(compact) < 3:
        slug = "solo-2611-" + hashlib.sha1(str(value or "").encode("utf-8")).hexdigest()[:10]
    return slug[:75].rstrip("-")


def token(config: dict[str, Any]) -> str:
    raw = os.environ.get("GITHUB_TOKEN", "").strip() or config_get(config, "github.accessTokens") or ""
    value = (raw[0] if isinstance(raw, list) and raw else str(raw)).strip()
    if not value:
        raise Solo2611Error("缺少 GitHub Token：`config set github.accessTokens=<token>`（需要 repo 权限）")
    return value


def _proxy(config: dict[str, Any]) -> str:
    proxy = str(config_get(config, "github.proxyHttp") or "").strip()
    if proxy and "://" not in proxy:
        proxy = "http://" + proxy
    return proxy


def api(config: dict[str, Any], method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token(config)}", "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"})
    proxy = _proxy(config)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": proxy, "http": proxy} if proxy else {}))
    try:
        with opener.open(req, timeout=60) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"message": raw.decode("utf-8", errors="replace")[:300]}
    except (urllib.error.URLError, TimeoutError) as exc:
        raise Solo2611Error(f"无法访问 GitHub API：{exc}（需要代理时 `config set github.proxyHttp=127.0.0.1:端口`）")


def git_env(config: dict[str, Any]) -> dict[str, str]:
    basic = base64.b64encode(f"x-access-token:{token(config)}".encode()).decode()
    env = os.environ.copy()
    env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}", "GIT_TERMINAL_PROMPT": "0"})
    proxy = _proxy(config)
    if proxy:
        env.update({"HTTPS_PROXY": proxy, "HTTP_PROXY": proxy})
    return env


def push_sha(ws: Path, url: str, sha: str, branch: str, env: dict[str, str] | None) -> None:
    """只做快进推送：不 force、不 rebase，已推送的快照 SHA 永远可达。"""
    proc = run(["git", "-C", str(ws), "push", "--quiet", url, f"{sha}:refs/heads/{branch}"],
               check=False, env=env, timeout=900)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise Solo2611Error(f"推送 {sha[:12]} 到 {url} 失败（不会 force）：{err}")


def _create(config: dict[str, Any], name: str, private: bool, description: str) -> dict[str, Any]:
    status, owner = api(config, "GET", "/user")
    if status != 200:
        raise Solo2611Error(f"GitHub Token 无效（/user 返回 {status}）：{owner.get('message', '')}")
    for _ in range(5):
        repo = f"{name}-{''.join(random.choices(string.ascii_lowercase + string.digits, k=4))}"
        status, body = api(config, "POST", "/user/repos", {"name": repo, "private": private, "description": description,
                                                           "auto_init": False, "has_issues": False, "has_wiki": False})
        if status == 201:
            return body
        if status != 422:  # 422 = 重名，换后缀再试
            raise Solo2611Error(f"创建仓库失败（{status}）：{body.get('message', '')}")
    raise Solo2611Error("连续 5 次生成的仓库名都已存在")


def verify(config: dict[str, Any], full_name: str, sha: str) -> bool:
    status, _ = api(config, "GET", f"/repos/{full_name}/commits/{sha}")
    return status == 200


def ensure_repo(task_root: Path, state: dict[str, Any], config: dict[str, Any], *, name: str = "",
                private: bool = False) -> dict[str, Any]:
    """首次调用建仓并推初始快照；之后只补推尚未推送的产物快照。"""
    ws = Path(state["workspace"])
    remote = state.get("remote") or {}
    env = git_env(config)
    created_now = False
    if not remote.get("fullName"):
        base = repo_slug(name or state["taskName"])
        body = _create(config, base, private, f"solo-2611 snapshot: {state['taskName']}")
        remote = {"fullName": body["full_name"], "web": body["html_url"], "url": body["clone_url"], "branch": "main",
                  "private": bool(body.get("private")), "createdAt": utc_now()}
        created_now = True
    try:
        if not remote.get("initialPushed"):
            push_sha(ws, remote["url"], state["initialSha"], remote["branch"], env)
            remote["initialPushed"] = utc_now()
    except Solo2611Error as exc:
        if created_now:  # 刚建的空仓库推不上去就删掉，免得留下没有快照的公开仓库
            status, _ = api(config, "DELETE", f"/repos/{remote['fullName']}")
            raise Solo2611Error(f"{exc}\n{'已删除刚创建的空仓库' if status == 204 else '空仓库删除失败，请手动删除 ' + remote['web']}")
        raise
    state["remote"] = remote
    save_state(task_root, state)
    sha = (state.get("artifact") or {}).get("sha")
    if sha and remote.get("artifactSha") != sha:
        push_sha(ws, remote["url"], sha, remote["branch"], env)
        remote.update({"artifactSha": sha, "artifactPushed": utc_now()})
    checks = {"initial": verify(config, remote["fullName"], state["initialSha"])}
    if sha:
        checks["artifact"] = verify(config, remote["fullName"], sha)
    remote["verified"] = checks
    state["remote"] = remote
    save_state(task_root, state)
    if not all(checks.values()):
        raise Solo2611Error(f"推送后 GitHub 上查不到提交：{checks}")
    return remote


def permalink(state: dict[str, Any], sha: str) -> str:
    remote = state.get("remote") or {}
    pushed = sha == state.get("initialSha") and remote.get("initialPushed") or sha == remote.get("artifactSha")
    return f"{remote['web'].rstrip('/')}/commit/{sha}" if remote.get("web") and pushed and is_full_sha(sha) else ""

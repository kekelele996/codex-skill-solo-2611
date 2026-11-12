#!/usr/bin/env python3
"""每轮完成后把产物源码提交并推送到项目 GitHub 仓库 main 分支（solo-annotation-loop）。

借鉴 pg-code 的提交设计：
- commit author/committer 严格使用 ~/.codex/pg-code/github-context.json 的 gitAuthor
- 只提交源码，排除依赖/构建产物/密钥/评审材料
- 推送后调用宿主 API 校验 commit 可达
- 提交信息固定为 `round <N>: <sessionId>`

用法：
  python3 commit-round.py <workspace> --round 1 --session-id <sessionId> [--output locator-补丁.json]
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

CONTEXT = Path.home() / '.codex' / 'pg-code' / 'github-context.json'

EXCLUDE_PATHSPECS = [
    ':(glob,exclude)**/node_modules/**', ':(glob,exclude)**/.pnpm-store/**',
    ':(glob,exclude)**/vendor/**', ':(glob,exclude)**/.venv/**', ':(glob,exclude)**/venv/**',
    ':(glob,exclude)**/__pycache__/**', ':(glob,exclude)**/*.pyc', ':(glob,exclude)**/.gradle/**',
    ':(glob,exclude)**/target/**', ':(glob,exclude)**/dist/**', ':(glob,exclude)**/build/**',
    ':(glob,exclude)**/out/**', ':(glob,exclude)**/.next/**', ':(glob,exclude)**/.nuxt/**',
    ':(glob,exclude)**/.output/**', ':(glob,exclude)**/.svelte-kit/**', ':(glob,exclude)**/.turbo/**',
    ':(glob,exclude)**/.parcel-cache/**', ':(glob,exclude)**/coverage/**', ':(glob,exclude)**/*.tsbuildinfo',
    ':(glob,exclude)**/*.class', ':(glob,exclude)**/*.o', ':(glob,exclude)**/*.obj', ':(glob,exclude)**/*.a',
    ':(glob,exclude)**/*.so', ':(glob,exclude)**/*.dylib', ':(glob,exclude)**/*.dll', ':(glob,exclude)**/*.exe',
    ':(glob,exclude)**/*.jar', ':(glob,exclude)**/*.war', ':(glob,exclude)**/*.zip', ':(glob,exclude)**/*.tar',
    ':(glob,exclude)**/*.tar.gz', ':(glob,exclude)**/*.tgz', ':(glob,exclude)**/*.rar', ':(glob,exclude)**/*.7z',
    ':(glob,exclude)**/.pytest_cache/**', ':(glob,exclude)**/.mypy_cache/**', ':(glob,exclude)**/.ruff_cache/**',
    ':(glob,exclude)**/*.egg-info/**', ':(glob,exclude)**/.idea/**', ':(glob,exclude)**/.vscode/**',
    ':(glob,exclude)**/.trae/**', ':(glob,exclude)**/.cursor/**', ':(glob,exclude)**/.DS_Store',
    ':(glob,exclude)**/Thumbs.db', ':(glob,exclude)**/*.log', ':(glob,exclude)**/*.tmp',
    ':(glob,exclude)**/*.db-shm', ':(glob,exclude)**/*.db-wal', ':(glob,exclude)**/*.db-journal',
    ':(glob,exclude)**/.env', ':(glob,exclude)**/.env.local', ':(glob,exclude)**/.env.*.local',
    ':(glob,exclude)**/*.bak', ':(glob,exclude)**/*.bak-*', ':(glob,exclude)**/*.orig', ':(glob,exclude)**/*.rej',
]


def run_git(repo: Path, *args: str, env=None, check=True) -> subprocess.CompletedProcess[str]:
    r = subprocess.run(['git', *args], cwd=repo, env=env, text=True, capture_output=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {(r.stderr or r.stdout).strip()}")
    return r


def load_context() -> dict:
    if not CONTEXT.exists():
        raise RuntimeError(f'未找到 GitHub 上下文: {CONTEXT}')
    data = json.loads(CONTEXT.read_text(encoding='utf-8'))
    gh = data.get('github') or {}
    if not gh.get('username') or not gh.get('token'):
        raise RuntimeError('github-context.json 缺 username/token')
    author = data.get('gitAuthor') or {}
    if not author.get('name') or not author.get('email'):
        raise RuntimeError('github-context.json 缺 gitAuthor.name/email')
    return data


def auth_env(remote_url: str, username: str, token: str, author: dict) -> dict[str, str]:
    parsed = urlparse(remote_url.strip())
    env = os.environ.copy()
    env['GIT_TERMINAL_PROMPT'] = '0'
    if parsed.scheme and parsed.netloc:
        base = f"{parsed.scheme}://{parsed.netloc}/"
        auth = base64.b64encode(f'{username}:{token}'.encode()).decode()
        env['GIT_CONFIG_COUNT'] = '1'
        env['GIT_CONFIG_KEY_0'] = f'http.{base}.extraHeader'
        env['GIT_CONFIG_VALUE_0'] = f'Authorization: Basic {auth}'
    env['GIT_AUTHOR_NAME'] = author['name']; env['GIT_AUTHOR_EMAIL'] = author['email']
    env['GIT_COMMITTER_NAME'] = author['name']; env['GIT_COMMITTER_EMAIL'] = author['email']
    return env


def normalize_repo_url(url: str) -> str:
    url = url.strip()
    if url.startswith('git@'):
        host, _, path = url[len('git@'):].partition(':')
        url = f'https://{host}/{path}'
    elif url.startswith('ssh://'):
        p = urlparse(url); url = f'https://{p.hostname}{p.path}'
    return url[:-len('.git')] if url.endswith('.git') else url.rstrip('/')


def verify_reachable(repo_url: str, sha: str, username: str, token: str) -> dict:
    host = urlparse(repo_url).hostname or ''
    path = urlparse(repo_url).path.strip('/')
    if 'github' in host:
        api = f'https://api.github.com/repos/{path}/commits/{sha}'
    elif 'gitlab' in host:
        from urllib.parse import quote
        api = f'https://{host}/api/v4/projects/{quote(path, safe="")}/repository/commits/{sha}'
    else:
        return {'reachable': None, 'detail': f'unknown host {host}'}
    req = urllib.request.Request(api)
    req.add_header('Authorization', 'Basic ' + base64.b64encode(f'{username}:{token}'.encode()).decode())
    req.add_header('User-Agent', 'solo-annotation-loop')
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return {'reachable': r.status == 200, 'status': r.status}
    except urllib.error.HTTPError as e:
        return {'reachable': False, 'status': e.code, 'detail': e.reason}
    except urllib.error.URLError as e:
        return {'reachable': False, 'detail': str(e.reason)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('workspace', type=Path)
    ap.add_argument('--round', required=True, type=int)
    ap.add_argument('--session-id', required=True)
    ap.add_argument('--remote', default='origin')
    ap.add_argument('--output', type=Path)
    args = ap.parse_args()
    repo = args.workspace.resolve()
    if not (repo / '.git').exists():
        raise SystemExit(f'不是 git 仓库: {repo}')

    ctx = load_context()
    gh = ctx['github']; author = ctx['gitAuthor']
    username, token = gh['username'].strip(), gh['token'].strip()

    remote_url = run_git(repo, 'remote', 'get-url', args.remote).stdout.strip()
    if not remote_url:
        raise SystemExit(f'仓库没有 {args.remote} remote，无法提交推送')
    env = auth_env(remote_url, username, token, author)

    cur = run_git(repo, 'branch', '--show-current').stdout.strip()
    if cur != 'main':
        run_git(repo, 'checkout', '-B', 'main')

    message = f'round {args.round}: {args.session_id.strip()}'
    run_git(repo, 'config', 'user.name', author['name'])
    run_git(repo, 'config', 'user.email', author['email'])
    run_git(repo, 'add', '-A', '--', '.', *EXCLUDE_PATHSPECS)
    files = [l for l in run_git(repo, 'diff', '--cached', '--name-only').stdout.splitlines() if l.strip()]
    if not files:
        print(json.dumps({'committed': False, 'blocked': True, 'message': '无变更文件，无本轮可提交的产物'}, ensure_ascii=False))
        return 1
    run_git(repo, 'commit', '-m', message, env=env)
    sha = run_git(repo, 'rev-parse', 'HEAD').stdout.strip()
    ident = run_git(repo, 'show', '-s', '--format=%an%x00%ae%x00%cn%x00%ce', sha).stdout.rstrip('\n').split('\0')
    if ident != [author['name'], author['email'], author['name'], author['email']]:
        raise RuntimeError(f'commit 身份不符: {ident}')
    run_git(repo, 'push', args.remote, 'HEAD:main', env=env)
    repo_url = normalize_repo_url(remote_url)
    commit_url = f'{repo_url}/commit/{sha}' if 'gitlab' not in (urlparse(repo_url).hostname or '') else f'{repo_url}/-/commit/{sha}'
    reachable = verify_reachable(repo_url, sha, username, token)
    out = {
        'round': args.round, 'sessionId': args.session_id.strip(), 'commitHash': sha,
        'commitUrl': commit_url, 'repoUrl': repo_url, 'reachable': reachable,
        'fileCount': len(files), 'files': files[:200],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(out, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return 0 if reachable.get('reachable') is not False else 1


if __name__ == '__main__':
    raise SystemExit(main())

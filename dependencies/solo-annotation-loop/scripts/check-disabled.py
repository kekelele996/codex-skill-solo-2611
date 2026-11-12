#!/usr/bin/env python3
"""按禁用范围检查一个项目目录，命中则返回 disabled=true 并给出原因。

用法：
  python3 check-disabled.py --project <项目目录> [--domain <业务领域>] [--theme <主题>] [--json]
只读，不修改任何文件。文本证据：项目目录名 + README/说明标题 + 项目概述/核心功能。
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

DOMAIN_DISABLED = {'游戏开发', '命令行工具', '桌面应用（含GUI）', '数据分析与可视化', '自动化与工具脚本'}
THEME_SUFFIXES = ('博客主题项目提示词', '游戏相关主题项目提示词', '纯前端小游戏主题项目提示词')

KEYWORDS = {
    '游戏和图形': ['贪吃蛇', '打砖块', '俄罗斯方块', '坦克大战', '塔防', '解谜', '潜行', '平台跳跃', '翻牌', '连连看',
                 '五子棋', '2048', '扫雷', '打地鼠', '华容道', '推箱子', '像素小鸟', '猜词达人', '数独', '物理模拟', '粒子系统', '喂食小动物'],
    '本地和桌面工具': ['代码片段', 'snippet', '批量重命名', '截图标注', '截图工具', '文件管理', '文件同步', '网盘', '书签', '密码管理'],
    '平台和业务系统': ['电商', '购物车', 'RBAC', '权限管理', '管理后台', '库存管理', '库存系统', '进销存', '仓储', '投票', '问卷',
                 '考勤', '办公自动化', 'OA系统', '图书借阅', '借阅', '博客', 'CMS', '挂号', '问诊', '门诊', '外卖', '点餐',
                 'CRM', '即时通讯', '聊天室', '拍卖', '停车场', '客服', 'Excel 导入', '积分商城', '商城', '预约'],
    '数据可视化和前端': ['报表', '统计看板', '数据统计', '看板', '大屏', 'dashboard', 'Streamlit', 'CSV', '记账', '菜谱', '食谱',
                   '番茄钟', '习惯', '打卡', '音乐播放器', '旅行日记', '观影记录', '健身', '卡路里', '体重管理'],
}
ASCII_WORDS = {'RBAC', 'CMS', 'CRM', 'dashboard', 'Streamlit', 'CSV', 'snippet'}


def _match_ascii(text: str, token: str) -> bool:
    return bool(re.search(r'(?<![a-z0-9])' + re.escape(token.lower()) + r'(?![a-z0-9])', text.lower()))


def gather_text(project: Path) -> str:
    """只采信项目名 + 文档标题 + 项目概述，避免把正文里的子功能误判成项目主类型。"""
    parts = [project.name]
    files = [f for f in project.glob('*.md') if f.is_file()]
    files += [f for f in project.glob('*.txt') if f.is_file()]
    files = files[:5]
    for f in files:
        try:
            text = f.read_text(encoding='utf-8', errors='ignore')
        except OSError:
            continue
        for line in text.splitlines()[:80]:
            line = line.strip()
            if re.match(r'^#{1,6}\s+', line):
                parts.append(re.sub(r'^#+\s*', '', line))
        m = re.search(r'(?:项目概述|项目简介)\s*[:：]?\s*([^\n。；]{4,200})', text[:4000])
        if m:
            parts.append(m.group(1))
        # 没有概述时，取首段非标题行
        else:
            for line in text.splitlines()[:20]:
                line = line.strip()
                if line and not re.match(r'^#{1,6}\s+', line) and not line.startswith('```'):
                    parts.append(line[:200])
                    break
    return ' '.join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--project', required=True, type=Path)
    ap.add_argument('--domain', default='')
    ap.add_argument('--theme', default='')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()
    project = args.project.resolve()
    if not project.is_dir():
        ap.error(f'项目目录不存在: {project}')
    reasons: list[str] = []
    if args.domain in DOMAIN_DISABLED:
        reasons.append(f'领域:{args.domain}')
    if args.theme.endswith(THEME_SUFFIXES):
        reasons.append(f'主题:{args.theme}')
    text = gather_text(project)
    for cat, kws in KEYWORDS.items():
        for k in kws:
            hit = _match_ascii(text, k) if k in ASCII_WORDS else (k.lower() in text.lower())
            if hit:
                reasons.append(f'{cat}:{k}')
    result = {'project': str(project), 'disabled': bool(reasons), 'reasons': reasons}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not result['disabled'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

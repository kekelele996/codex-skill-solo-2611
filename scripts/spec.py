"""26.11 领域专项的字段、枚举与出题禁区（来源：CC_Codex 用户反馈标注说明（26.11领域专项）最新.md）。"""

from __future__ import annotations

import re

# 提交表表头，顺序与官方模板一致
SUBMIT_COLUMNS: list[str] = [
    "User Prompt",
    "所属领域",
    "子方向",
    "任务类型",
    "任务难度",
    "语言/框架",
    "Harness",
    "Harness 版本",
    "操作系统",
    "环境可复现等级",
    "初始环境快照",
    "SessionID",
    "TurnID/PromptID",
    "轨迹文件",
    "产物快照",
    "运行录屏",
    "交付完整性",
    "Feedback",
    "备注",
]

COLUMN_GROUPS: list[tuple[str, int]] = [
    ("题目与环境信息", 11),
    ("会话与产物信息（单轮执行）", 5),
    ("Feedback 评价", 2),
    ("其他", 1),
]

DOMAINS: dict[str, list[str]] = {
    "安全": ["漏洞检测与攻防", "身份认证", "网络与通信安全", "系统安全", "其他"],
    "游戏、图形与多媒体": ["音视频处理", "图形渲染", "游戏开发", "图像处理"],
    "数据工程": ["数据采集、爬虫与 RPA", "数仓与数据湖", "数据分析与数据应用"],
}


def validate_classification(domain: str, sub_direction: str) -> tuple[str, list[str]]:
    """归一并校验领域/子方向；合法值来自 DOMAINS 中该领域的完整子方向列表。"""
    normalized = "其他" if sub_direction == "*其他" else sub_direction
    errors: list[str] = []
    if domain not in DOMAINS:
        errors.append(f"所属领域 {domain} 不在最新分类中：{list(DOMAINS)}")
    elif normalized not in DOMAINS[domain]:
        errors.append(f"子方向 {normalized} 不属于 {domain}：{DOMAINS[domain]}")
    return normalized, errors


TASK_TYPES: list[str] = [
    "功能开发",
    "Bugfix",
    "代码重构",
    "功能优化",
    "依赖升级与迁移",
    "质量保障",
    "项目维护",
    "技术讨论",
    "问题排查与诊断",
    "部署运维",
    "泛 Agent 需求",
]

DIFFICULTIES: list[str] = ["困难", "地狱"]
HARNESSES: list[str] = ["Claude Code", "Codex CLI"]
OS_OPTIONS: list[str] = ["MacOS/Linux", "Windows"]
REPRO_LEVELS: list[str] = ["无外部依赖", "有外部依赖，未容器化", "已容器化，可一键起环境"]

# 第四节「不被允许出的雷同题」：命中即阻断
BANNED_TOPICS: dict[str, list[str]] = {
    "游戏/图形经典小游戏": [
        "贪吃蛇", "打砖块", "俄罗斯方块", "坦克大战", "塔防", "平台跳跃", "潜行", "喂食",
        "记忆翻牌", "翻牌", "连连看", "五子棋", "2048 游戏", "2048游戏", "2048 小游戏", "扫雷", "打地鼠", "象棋", "围棋", "跳棋",
        "snake", "tetris", "breakout", "minesweeper", "tower defense", "gomoku",
    ],
    "粒子/物理模拟演示": [
        "星系", "烟花", "落沙", "布料模拟", "布料仿真", "fireworks", "falling sand", "galaxy simulation",
        "cloth simulation", "n-body",
    ],
    "平台/业务系统": [
        "购物车", "电商订单", "RBAC", "权限后台", "仓库库存", "库存管理", "投票", "问卷", "考勤", "OA",
        "图书借阅", "博客", "CMS", "挂号", "问诊", "外卖", "点餐", "CRM", "私信", "拍卖", "停车场",
        "工单", "客服系统", "Excel 导入", "积分商城", "预约系统",
    ],
    "本地/桌面工具": [
        "代码片段管理", "批量重命名", "截图标注", "文件同步", "书签管理", "密码管理器",
    ],
    "数据可视化/前端页面": [
        "Streamlit", "CSV 看板", "报表统计", "记账", "健身", "菜谱", "天气预报", "天气应用", "番茄钟", "习惯打卡",
        "音乐播放器", "旅行日记", "观影记录",
    ],
}

# 命中只给警告，需要人工确认不是雷同题（例如真实渲染引擎里的粒子系统是允许的）
WARN_TOPICS: list[str] = ["粒子", "物理模拟", "小游戏", "命令行工具", "CLI", "todo", "CRUD", "解谜", "天气", "棋"]

# 数据工程题很容易把 ETL/对账/调度误写成通用后台或看板。这里只拦已经被出烂的成品形态，
# 不拦真实管道里的报表输出、采集调度或对账步骤。
DATA_ENGINEERING_BANNED: list[str] = [
    "CSV 看板", "CSV看板", "Streamlit", "Excel 导入工具", "Excel导入工具",
    "爬虫入门", "静态页面抓取演示", "数据大屏", "可视化大屏",
]

# 第三步评分表原文中的描述，Feedback 禁止照抄
RUBRIC_PHRASES: list[str] = [
    "一次性完美跑通",
    "不仅实现了所有明示需求",
    "主动补全了隐性需求",
    "零虚假成功",
    "基本完美",
    "达成所有主要需求，代码可运行",
    "有极少量的细节遗漏",
    "勉强达标",
    "核心功能可跑，但有明显 Bug 需人工微调",
    "未达成主要需求",
    "代码因逻辑或依赖问题无法运行",
    "存在较大比例的虚假成功",
    "完全失败",
    "代码严重编译报错完全不可运行",
    "满口答应但一行代码没写",
]

# 不可核验的一句话评价（第三步：高分同样要写实证）
VAGUE_PHRASES: list[str] = ["完美完成", "效果很好", "基本可用", "效果不好", "不行", "一般般", "还行", "没完成任务", "代码有 bug", "过程比较乱"]

NEGATIVE_SIGNALS = re.compile(
    r"(无法运行|跑不起来|编译失败|编译报错|构建失败|启动失败|未实现|没有实现|没实现|缺失|崩溃|报错|"
    r"虚假成功|宣称完成|就报告完成|答非所问|一行代码没写|白屏|黑屏)"
)
ANCHOR_PATTERN = re.compile(
    r"(`[^`]+`|[\w./-]+\.(?:ts|tsx|js|jsx|mjs|cjs|glsl|wgsl|hlsl|vert|frag|comp|c|cc|cpp|cxx|h|hpp|py|rs|go|java|"
    r"cs|json|html|css|vue|md|toml|yaml|yml|mk|cmake|shader|sql|csv|tsv|parquet|avro|proto|ipynb)\b|"
    r"\b[A-Za-z_][A-Za-z0-9_]*\(\)|"
    r"\b(?:Error|Exception|TypeError|ReferenceError|SyntaxError|undefined|NaN|exit code|退出码)\b|第\s*\d+\s*步)"
)
PROCESS_HINT = re.compile(
    r"(过程|第\s*\d+\s*步|轨迹|调用|执行了|读取|运行了|验证|规划|反复|重试|没有运行|未运行|"
    r"重复执行|失败后|续跑)"
)
ARTIFACT_HINT = re.compile(
    r"(产物|页面|画面|输出|文件|接口|功能|效果|运行后|打开|结果|记录|批次|对账|入库|回读|"
    r"订单|数据|行数|字段|分区)"
)


def scan_banned(text: str, domain: str = "") -> tuple[list[str], list[str]]:
    """返回 (阻断命中, 警告命中)。"""
    lowered = text.lower()
    blocks: list[str] = []
    if domain == "数据工程":
        for word in DATA_ENGINEERING_BANNED:
            if word.lower() in lowered:
                blocks.append(f"数据工程成品形态:{word}")
        warns = [w for w in WARN_TOPICS if w.lower() in lowered]
        return blocks, warns
    for group, words in BANNED_TOPICS.items():
        for word in words:
            w = word.lower()
            if re.fullmatch(r"[a-z0-9 -]+", w):
                if re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", lowered):
                    blocks.append(f"{group}:{word}")
            elif w in lowered:
                blocks.append(f"{group}:{word}")
    warns = [w for w in WARN_TOPICS if w.lower() in lowered]
    return blocks, warns

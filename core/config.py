#!/usr/bin/env python3
"""全局配置：环境变量加载 + 常量"""
import os
from dotenv import load_dotenv

load_dotenv()

# core/ 目录（本文件所在目录）
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# 项目根目录（core 的上一级，用于定位 scripts/ 等外部资源）
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)

# ===== LLM =====
DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
DEEPSEEK_BASE_URL = os.environ.get('DEEPSEEK_BASE_URL', 'https://api.deepseek.com')
DEEPSEEK_MODEL = os.environ.get('DEEPSEEK_MODEL', 'deepseek-v4-flash')

# ===== 工具调用 =====
# 工具调用最大轮数：对话开始批量并行执行模型请求的工具，结果一次性回传后给出最终回答；
# 若模型在最终轮仍请求工具，在预算内可再执行，超出则强制基于已有结果作答。
MAX_TOOL_ROUNDS = int(os.environ.get('MAX_TOOL_ROUNDS', '6'))

# ===== LLM 内置工具（服务端执行，非本地 function 工具）=====
# DeepSeek Responses API 内置 web_search 工具的开关。
# true=向模型提供内置联网搜索（默认）；false=关闭，模型不再触发服务端网页搜索。
LLM_WEB_SEARCH = os.environ.get('LLM_WEB_SEARCH', 'true') in ('1', 'true', 'True', 'yes')

# ===== 调试 =====
# 打印每轮发送给 API 的消息序列 hash（role:md5），默认关闭
DEBUG_SEND_SEQ = os.environ.get('DEBUG_SEND_SEQ', '0') in ('1', 'true', 'True')

# ===== OpenViking 记忆检索 =====
# search / find 两个记忆工具的默认阈值与条数；运行时 LLM 可自主传参覆盖。
# search 接口（上下文感知）：默认阈值 0.4，默认返回 3 条
OV_SEARCH_THRESHOLD = float(os.environ.get('OV_SEARCH_THRESHOLD', '0.4'))
OV_SEARCH_LIMIT = int(os.environ.get('OV_SEARCH_LIMIT', '3'))
# find 接口（纯向量语义搜索）：默认阈值 0.4，默认返回 3 条。
# 自动注入（openviking_load_context）也使用 find 接口，故复用本组配置。
OV_FIND_THRESHOLD = float(os.environ.get('OV_FIND_THRESHOLD', '0.4'))
OV_FIND_LIMIT = int(os.environ.get('OV_FIND_LIMIT', '3'))

# ===== OpenViking 官方结构对齐（可选）=====
# 召回 peer 隔离：all=跨项目召回；actor=仅本 workspace 隔离召回
OV_RECALL_PEER_SCOPE = os.environ.get('OV_RECALL_PEER_SCOPE', 'all')
# 按项目目录派生 peer（ws-<hash>），使不同项目记忆读写隔离
OV_WORKSPACE_PEER = os.environ.get('OV_WORKSPACE_PEER', 'false') in ('1', 'true', 'True', 'yes')
# 显式指定 peer（优先级最高，覆盖上面两项）
OV_PEER_ID = os.environ.get('OV_PEER_ID', '')
# 会话开始记忆索引块字符预算
OV_PROFILE_TOKEN_BUDGET = int(os.environ.get('OV_PROFILE_TOKEN_BUDGET', '1000'))
# 召回 query 最小长度，过短则跳过
OV_MIN_QUERY_LENGTH = int(os.environ.get('OV_MIN_QUERY_LENGTH', '3'))
# 会话自动捕获/提交开关：true=把对话写入 OV session 并 commit 触发记忆提取
OV_AUTO_CAPTURE = os.environ.get('OV_AUTO_CAPTURE', 'false') in ('1', 'true', 'True', 'yes')
# 自动捕获时的选择策略（对齐官方 openviking 插件）
OV_CAPTURE_ASSISTANT_TURNS = os.environ.get('OV_CAPTURE_ASSISTANT_TURNS', 'true') in ('1', 'true', 'True', 'yes')
OV_CAPTURE_MAX_LENGTH = int(os.environ.get('OV_CAPTURE_MAX_LENGTH', '24000') or '24000')
OV_CAPTURE_TOOL_MAX_CHARS = int(os.environ.get('OV_CAPTURE_TOOL_MAX_CHARS', '2000') or '2000')
# 跨轮去重轮数（对齐官方 dedup_turns）：最近 N 轮已注入过的记忆 URI 不再召回，避免同一份记忆反复注入刷屏；0=关闭
OV_RECALL_DEDUP_TURNS = int(os.environ.get('OV_RECALL_DEDUP_TURNS', '5'))

# ===== 数据库存储 =====
# DB_ONLINE: true=在线 MySQL；false=本地 SQLite（MySQL 连接失败也会自动降级 SQLite）
DB_ONLINE = os.environ.get('DB_ONLINE', 'true') in ('1', 'true', 'True', 'yes')
# 本地 SQLite 文件路径（DB_ONLINE=false 或 MySQL 不可用时使用）
# 注意：.env 中留空时必须回退默认值，否则 sqlite3.connect('') 会打开内存库导致表丢失
SQLITE_DB_PATH = os.environ.get('SQLITE_DB_PATH') or os.path.join(PROJECT_ROOT, 'data', 'shell_tool.db')

# ===== MySQL 在线存储 =====
DB_CONFIG = {
    'host': os.environ.get('MYSQL_HOST', ''),
    'user': os.environ.get('MYSQL_USER', ''),
    'password': os.environ.get('MYSQL_PASSWORD', ''),
    'database': os.environ.get('MYSQL_DB', ''),
    'port': int(os.environ.get('MYSQL_PORT', '3306') or '3306'),
    'charset': 'utf8mb4',
}

# 是否已配置 MySQL 连接信息（host/user/database 任一为空视为未配置）
DB_CONFIGURED = bool(DB_CONFIG['host'] and DB_CONFIG['user'] and DB_CONFIG['database'])

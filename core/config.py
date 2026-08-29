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

# ===== 调试 =====
# 打印每轮发送给 API 的消息序列 hash（role:md5），默认关闭
DEBUG_SEND_SEQ = os.environ.get('DEBUG_SEND_SEQ', '0') in ('1', 'true', 'True')

# ===== OpenViking 记忆检索 =====
# 自动注入的相似度阈值与条数；openviking_search 工具默认值由此提供，
# 但工具运行时由 LLM 自行传参判断（阈值收敛 0~1，条数收敛 0~10）
OV_SCORE_THRESHOLD = float(os.environ.get('OV_SCORE_THRESHOLD', '0.35'))
OV_SEARCH_LIMIT = int(os.environ.get('OV_SEARCH_LIMIT', '3'))
OV_INJECT_LIMIT = int(os.environ.get('OV_INJECT_LIMIT', '5'))

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

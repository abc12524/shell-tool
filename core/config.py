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
# 按 think.txt 官方推荐流程：工具只在对话开始时批量调用一次，之后直接给出最终回答
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

# ===== MySQL 存储 =====
DB_CONFIG = {
    'host': os.environ.get('MYSQL_HOST', ''),
    'user': os.environ.get('MYSQL_USER', ''),
    'password': os.environ.get('MYSQL_PASSWORD', ''),
    'database': os.environ.get('MYSQL_DB', ''),
    'port': int(os.environ.get('MYSQL_PORT', '')),
    'charset': 'utf8mb4',
}

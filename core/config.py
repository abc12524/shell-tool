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

# ===== MySQL 存储 =====
DB_CONFIG = {
    'host': os.environ.get('MYSQL_HOST', ''),
    'user': os.environ.get('MYSQL_USER', ''),
    'password': os.environ.get('MYSQL_PASSWORD', ''),
    'database': os.environ.get('MYSQL_DB', ''),
    'port': int(os.environ.get('MYSQL_PORT', '')),
    'charset': 'utf8mb4',
}

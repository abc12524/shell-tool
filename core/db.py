#!/usr/bin/env python3
"""存储层：会话（chat_sessions）与消息（chat_messages），支持 MySQL 与本地 SQLite 双后端。

后端选择规则（见 config.resolve_backend）：
  1. 配置文件未提供 MySQL 连接信息（MYSQL_HOST/USER/DB 任一为空） → 本地 SQLite
  2. DB_ONLINE=false → 本地 SQLite
  3. 其余情况优先在线 MySQL；连接失败自动降级到本地 SQLite
"""
import json
import os
import sqlite3

import pymysql

from .config import DB_CONFIG, DB_ONLINE, DB_CONFIGURED, SQLITE_DB_PATH

# 当前生效后端：'mysql' 或 'sqlite'（首次调用时根据配置解析，MySQL 失败自动降级）
_backend = None


def backend_mode():
    """返回当前生效后端：'mysql' 或 'sqlite'"""
    if _backend is None:
        resolve_backend()
    return _backend


def resolve_backend():
    """根据配置解析并初始化后端，返回当前后端名：'mysql' 或 'sqlite'。

    - 未配置 MySQL 或 DB_ONLINE=false  → 本地 SQLite
    - 配置了 MySQL 但连接失败          → 自动降级 SQLite（仅降级，不升回）
    """
    global _backend
    if _backend is not None:
        return _backend

    if DB_ONLINE and DB_CONFIGURED:
        try:
            with _mysql_conn():
                pass
            _backend = 'mysql'
        except Exception as e:
            print(f"⚠️  MySQL 连接失败，自动切换到本地 SQLite 数据库：{e}")
            _backend = 'sqlite'
    else:
        _backend = 'sqlite'

    if _backend == 'mysql':
        _init_schema_mysql()
    else:
        _init_schema_sqlite()
    return _backend


def init_schema():
    """兼容旧入口：同 resolve_backend"""
    return resolve_backend()


# ============= 连接 =============
def _mysql_conn():
    cfg = dict(DB_CONFIG)
    cfg['cursorclass'] = pymysql.cursors.DictCursor
    cfg['autocommit'] = True
    return pymysql.connect(**cfg)


def _sqlite_conn():
    os.makedirs(os.path.dirname(SQLITE_DB_PATH) or '.', exist_ok=True)
    conn = sqlite3.connect(SQLITE_DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    return conn


# ============= 建表 =============
_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_sessions (
    id         TEXT PRIMARY KEY,
    status     TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id        TEXT NOT NULL,
    role              TEXT NOT NULL,
    content           TEXT NULL,
    tool_calls_json   TEXT NULL,
    tool_call_id      TEXT NULL,
    reasoning_content TEXT NULL,
    output_items_json TEXT NULL,
    created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_session_id ON chat_messages (session_id, id);
"""


def _init_schema_mysql():
    """MySQL 建表（幂等，可重复调用）"""
    sql_sessions = """
    CREATE TABLE IF NOT EXISTS chat_sessions (
        id         VARCHAR(64) NOT NULL PRIMARY KEY,
        status     ENUM('active','closed') NOT NULL DEFAULT 'active',
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """
    sql_messages = """
    CREATE TABLE IF NOT EXISTS chat_messages (
        id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
        session_id     VARCHAR(64) NOT NULL,
        role           VARCHAR(20) NOT NULL,
        content        MEDIUMTEXT NULL,
        tool_calls_json TEXT NULL,
        tool_call_id   VARCHAR(64) NULL,
        reasoning_content MEDIUMTEXT NULL,
        output_items_json TEXT NULL,
        created_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        KEY idx_session_id (session_id, id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """
    with _mysql_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql_sessions)
            cur.execute(sql_messages)
            # 兼容旧表：缺列则补
            cur.execute("SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='chat_messages' AND COLUMN_NAME='reasoning_content'")
            if cur.fetchone()['COUNT(*)'] == 0:
                cur.execute("ALTER TABLE chat_messages ADD COLUMN reasoning_content MEDIUMTEXT NULL AFTER tool_call_id")
            cur.execute("SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='chat_messages' AND COLUMN_NAME='output_items_json'")
            if cur.fetchone()['COUNT(*)'] == 0:
                cur.execute("ALTER TABLE chat_messages ADD COLUMN output_items_json TEXT NULL AFTER reasoning_content")
        conn.commit()


def _init_schema_sqlite():
    """SQLite 建表（幂等，可重复调用）"""
    with _sqlite_conn() as conn:
        conn.executescript(_SQLITE_SCHEMA)
        # 兼容旧库：缺列则补
        cols = [row['name'] for row in conn.execute("PRAGMA table_info(chat_messages)")]
        if 'reasoning_content' not in cols:
            conn.execute("ALTER TABLE chat_messages ADD COLUMN reasoning_content TEXT")
        if 'output_items_json' not in cols:
            conn.execute("ALTER TABLE chat_messages ADD COLUMN output_items_json TEXT")
        conn.commit()


# ============= 会话 =============
def get_active_session_id():
    """取最近有活动的活跃会话；无则返回 None"""
    return _mysql_get_active_session_id() if backend_mode() == 'mysql' else _sqlite_get_active_session_id()


def session_exists(session_id):
    return _mysql_session_exists(session_id) if backend_mode() == 'mysql' else _sqlite_session_exists(session_id)


def create_session(session_id):
    """新建会话；若 id 已存在则重新激活"""
    return _mysql_create_session(session_id) if backend_mode() == 'mysql' else _sqlite_create_session(session_id)


def close_session(session_id):
    """终结指定会话"""
    return _mysql_close_session(session_id) if backend_mode() == 'mysql' else _sqlite_close_session(session_id)


def close_all_active_sessions():
    """终结当前所有活跃会话（用于 -n 新开对话）"""
    return _mysql_close_all_active_sessions() if backend_mode() == 'mysql' else _sqlite_close_all_active_sessions()


def get_session_info(session_id):
    return _mysql_get_session_info(session_id) if backend_mode() == 'mysql' else _sqlite_get_session_info(session_id)


def count_messages(session_id):
    return _mysql_count_messages(session_id) if backend_mode() == 'mysql' else _sqlite_count_messages(session_id)


# ============= 消息 =============
def append_messages(session_id, messages):
    """批量追加消息（字典格式：role/content/tool_calls/tool_call_id）"""
    return _mysql_append_messages(session_id, messages) if backend_mode() == 'mysql' else _sqlite_append_messages(session_id, messages)


def load_messages(session_id):
    return _mysql_load_messages(session_id) if backend_mode() == 'mysql' else _sqlite_load_messages(session_id)


# ============= MySQL 实现 =============
def _mysql_get_active_session_id():
    with _mysql_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM chat_sessions WHERE status='active' ORDER BY updated_at DESC LIMIT 1")
            row = cur.fetchone()
    return row['id'] if row else None


def _mysql_session_exists(session_id):
    with _mysql_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM chat_sessions WHERE id=%s", (session_id,))
            return cur.fetchone() is not None


def _mysql_create_session(session_id):
    with _mysql_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO chat_sessions (id) VALUES (%s) "
                "ON DUPLICATE KEY UPDATE status='active', updated_at=NOW()",
                (session_id,))
        conn.commit()
    return session_id


def _mysql_close_session(session_id):
    with _mysql_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE chat_sessions SET status='closed' WHERE id=%s", (session_id,))
        conn.commit()


def _mysql_close_all_active_sessions():
    with _mysql_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE chat_sessions SET status='closed' WHERE status='active'")
        conn.commit()


def _mysql_get_session_info(session_id):
    with _mysql_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, status, created_at, updated_at FROM chat_sessions WHERE id=%s", (session_id,))
            return cur.fetchone()


def _mysql_count_messages(session_id):
    with _mysql_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM chat_messages WHERE session_id=%s", (session_id,))
            return cur.fetchone()['n']


def _mysql_append_messages(session_id, messages):
    if not messages:
        return
    with _mysql_conn() as conn:
        with conn.cursor() as cur:
            for m in messages:
                tool_calls = m.get('tool_calls')
                output_items = m.get('output_items')
                cur.execute(
                    "INSERT INTO chat_messages (session_id, role, content, tool_calls_json, tool_call_id, reasoning_content, output_items_json) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (session_id,
                     m.get('role'),
                     m.get('content'),
                     json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None,
                     m.get('tool_call_id'),
                     m.get('reasoning_content'),
                     json.dumps(output_items, ensure_ascii=False) if output_items else None))
            # 触碰会话更新时间，保证"默认同一对话"能找到最近会话
            cur.execute("UPDATE chat_sessions SET updated_at=NOW() WHERE id=%s", (session_id,))
        conn.commit()


def _mysql_load_messages(session_id):
    sql = ("SELECT role, content, tool_calls_json, tool_call_id, reasoning_content, output_items_json FROM chat_messages "
           "WHERE session_id=%s ORDER BY id")
    with _mysql_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, [session_id])
            rows = cur.fetchall()
    return _build_msgs(rows)


# ============= SQLite 实现 =============
def _sqlite_get_active_session_id():
    with _sqlite_conn() as conn:
        row = conn.execute(
            "SELECT id FROM chat_sessions WHERE status='active' ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
    return row['id'] if row else None


def _sqlite_session_exists(session_id):
    with _sqlite_conn() as conn:
        row = conn.execute("SELECT id FROM chat_sessions WHERE id=?", (session_id,)).fetchone()
    return row is not None


def _sqlite_create_session(session_id):
    with _sqlite_conn() as conn:
        conn.execute(
            "INSERT INTO chat_sessions (id) VALUES (?) "
            "ON CONFLICT(id) DO UPDATE SET status='active', updated_at=CURRENT_TIMESTAMP",
            (session_id,))
        conn.commit()
    return session_id


def _sqlite_close_session(session_id):
    with _sqlite_conn() as conn:
        conn.execute("UPDATE chat_sessions SET status='closed' WHERE id=?", (session_id,))
        conn.commit()


def _sqlite_close_all_active_sessions():
    with _sqlite_conn() as conn:
        conn.execute("UPDATE chat_sessions SET status='closed' WHERE status='active'")
        conn.commit()


def _sqlite_get_session_info(session_id):
    with _sqlite_conn() as conn:
        row = conn.execute(
            "SELECT id, status, created_at, updated_at FROM chat_sessions WHERE id=?", (session_id,)
        ).fetchone()
    return dict(row) if row else None


def _sqlite_count_messages(session_id):
    with _sqlite_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM chat_messages WHERE session_id=?", (session_id,)).fetchone()
    return row['n']


def _sqlite_append_messages(session_id, messages):
    if not messages:
        return
    with _sqlite_conn() as conn:
        for m in messages:
            tool_calls = m.get('tool_calls')
            output_items = m.get('output_items')
            conn.execute(
                "INSERT INTO chat_messages (session_id, role, content, tool_calls_json, tool_call_id, reasoning_content, output_items_json) "
                "VALUES (?,?,?,?,?,?,?)",
                (session_id,
                 m.get('role'),
                 m.get('content'),
                 json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None,
                 m.get('tool_call_id'),
                 m.get('reasoning_content'),
                 json.dumps(output_items, ensure_ascii=False) if output_items else None))
        # 触碰会话更新时间，保证"默认同一对话"能找到最近会话
        conn.execute("UPDATE chat_sessions SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (session_id,))
        conn.commit()


def _sqlite_load_messages(session_id):
    with _sqlite_conn() as conn:
        rows = conn.execute(
            "SELECT role, content, tool_calls_json, tool_call_id, reasoning_content, output_items_json FROM chat_messages "
            "WHERE session_id=? ORDER BY id", (session_id,)
        ).fetchall()
    return _build_msgs(rows)


# ============= 消息序列构建（两后端共用） =============
def _build_msgs(rows):
    msgs = []
    for row in rows:
        role = row['role']
        if role == 'assistant' and row['tool_calls_json']:
            m = {
                'role': 'assistant',
                'content': row['content'] or '',
                'tool_calls': json.loads(row['tool_calls_json']),
            }
            if row['reasoning_content']:
                m['reasoning_content'] = row['reasoning_content']
            if row['output_items_json']:
                m['output_items'] = json.loads(row['output_items_json'])
            msgs.append(m)
        else:
            m = {'role': role, 'content': row['content'] or ''}
            if role == 'tool' and row['tool_call_id']:
                m['tool_call_id'] = row['tool_call_id']
            if role == 'assistant' and row['reasoning_content']:
                m['reasoning_content'] = row['reasoning_content']
            if role == 'assistant' and row['output_items_json']:
                m['output_items'] = json.loads(row['output_items_json'])
            msgs.append(m)

    # 保证消息序列合法：开头不能是孤立的 tool 结果或未配对的 tool_calls
    while msgs and (
        msgs[0].get('role') == 'tool'
        or (msgs[0].get('role') == 'assistant' and msgs[0].get('tool_calls'))
    ):
        msgs.pop(0)
    return msgs

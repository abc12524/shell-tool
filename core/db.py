#!/usr/bin/env python3
"""MySQL 存储层：会话（chat_sessions）与消息（chat_messages）"""
import json

import pymysql

from .config import DB_CONFIG


def _conn():
    cfg = dict(DB_CONFIG)
    cfg['cursorclass'] = pymysql.cursors.DictCursor
    cfg['autocommit'] = True
    return pymysql.connect(**cfg)


def init_schema():
    """建表（幂等，可重复调用）"""
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
        created_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        KEY idx_session_id (session_id, id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql_sessions)
            cur.execute(sql_messages)
        conn.commit()


# ============= 会话 =============
def get_active_session_id():
    """取最近有活动的活跃会话；无则返回 None"""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM chat_sessions WHERE status='active' ORDER BY updated_at DESC LIMIT 1")
            row = cur.fetchone()
    return row['id'] if row else None


def session_exists(session_id):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM chat_sessions WHERE id=%s", (session_id,))
            return cur.fetchone() is not None


def create_session(session_id):
    """新建会话；若 id 已存在则重新激活"""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO chat_sessions (id) VALUES (%s) "
                "ON DUPLICATE KEY UPDATE status='active', updated_at=NOW()",
                (session_id,))
        conn.commit()
    return session_id


def close_session(session_id):
    """终结指定会话"""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE chat_sessions SET status='closed' WHERE id=%s", (session_id,))
        conn.commit()


def close_all_active_sessions():
    """终结当前所有活跃会话（用于 -n 新开对话）"""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE chat_sessions SET status='closed' WHERE status='active'")
        conn.commit()


def get_session_info(session_id):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, status, created_at, updated_at FROM chat_sessions WHERE id=%s", (session_id,))
            return cur.fetchone()


def count_messages(session_id):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM chat_messages WHERE session_id=%s", (session_id,))
            return cur.fetchone()['n']


# ============= 消息 =============
def append_messages(session_id, messages):
    """批量追加消息（字典格式：role/content/tool_calls/tool_call_id）"""
    if not messages:
        return
    with _conn() as conn:
        with conn.cursor() as cur:
            for m in messages:
                tool_calls = m.get('tool_calls')
                cur.execute(
                    "INSERT INTO chat_messages (session_id, role, content, tool_calls_json, tool_call_id) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (session_id,
                     m.get('role'),
                     m.get('content'),
                     json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None,
                     m.get('tool_call_id')))
            # 触碰会话更新时间，保证"默认同一对话"能找到最近会话
            cur.execute("UPDATE chat_sessions SET updated_at=NOW() WHERE id=%s", (session_id,))
        conn.commit()


def load_messages(session_id):

    sql = ("SELECT role, content, tool_calls_json, tool_call_id FROM chat_messages "
           "WHERE session_id=%s ORDER BY id")
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, [session_id])
            rows = cur.fetchall()

    msgs = []
    for row in rows:
        role = row['role']
        if role == 'assistant' and row['tool_calls_json']:
            msgs.append({
                'role': 'assistant',
                'content': row['content'] or '',
                'tool_calls': json.loads(row['tool_calls_json']),
            })
        else:
            m = {'role': role, 'content': row['content'] or ''}
            if role == 'tool' and row['tool_call_id']:
                m['tool_call_id'] = row['tool_call_id']
            msgs.append(m)

    # 保证消息序列合法：开头不能是孤立的 tool 结果或未配对的 tool_calls
    while msgs and (
        msgs[0].get('role') == 'tool'
        or (msgs[0].get('role') == 'assistant' and msgs[0].get('tool_calls'))
    ):
        msgs.pop(0)
    return msgs

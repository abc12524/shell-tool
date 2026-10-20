#!/usr/bin/env python3
"""存储层：会话（chat_sessions）与消息（chat_messages）。

单一读写源 + 在线副本：
  1. 本地 SQLite 是唯一读写源。无论是否启用在线库，所有会话/消息都先落 SQLite；
  2. 在线 MySQL 仅作同步副本。SQLite 落库成功后，把「未同步」的行幂等推送到 MySQL；
  3. 每条消息带全局唯一 uid，MySQL 端对 uid 建唯一索引——重复推送不会产生重复行；
  4. 每条记录带 synced 标记，推送失败保持未同步，下次启动/写入时自动重试——不丢失。

MySQL 未配置、DB_ONLINE=false 或连接失败时，仅使用本地 SQLite。
"""
import json
import os
import sqlite3
import uuid

import pymysql

from .config import DB_CONFIG, DB_ONLINE, DB_CONFIGURED, SQLITE_DB_PATH
from . import events

# 是否已初始化；_sync_enabled 表示在线 MySQL 同步是否开启（SQLite 始终可用）
_ready = False
_sync_enabled = False


def resolve_backend():
    """初始化本地 SQLite（唯一读写源），并在配置允许时开启在线 MySQL 同步。

    在线同步采用「开启即取基线」：打开在线的那一次，把当前已有记录标记为已同步、
    记下基线 id/rowid，之后只同步基线之后新增的会话/消息——即只记录「打开在线之后」
    的对话，而不是把历史记录全量上传。

    返回后端名，固定为 'sqlite'（保持既有调用方兼容）。
    """
    global _ready, _sync_enabled
    if _ready:
        return 'sqlite'

    _init_schema_sqlite()
    _ready = True

    if DB_ONLINE and DB_CONFIGURED:
        try:
            with _mysql_conn():
                pass
            _init_schema_mysql()
            _sync_enabled = True
            _baseline_online_sync()
        except Exception as e:
            events.ev_warning(f"在线 MySQL 不可用，仅使用本地 SQLite：{e}", code="mysql_unavailable")
            _sync_enabled = False
    else:
        # 在线未启用：清除 epoch，使下次开启时重新取基线（离线期间的记录不回传）
        _mark_online_disabled()
        _sync_enabled = False

    if _sync_enabled:
        _sync_pending()
    return 'sqlite'


def init_schema():
    """兼容旧入口：同 resolve_backend"""
    return resolve_backend()


def backend_mode():
    """返回当前后端名（固定 'sqlite'）"""
    if not _ready:
        resolve_backend()
    return 'sqlite'


def online_enabled():
    """在线 MySQL 同步是否开启"""
    if not _ready:
        resolve_backend()
    return _sync_enabled


# 合成注入的用户消息角色（skill 名单 / OV profile / OV 召回块）。
# 只改变落库标识，便于与真实用户输入区分；读取/重放时统一映射回 'user'，
# 因此旧库（role='user'）与既有调用方都不受影响。
INSERT_USER_ROLE = 'insert_user'


def _chat_role(role):
    """DB 角色 → 聊天协议角色：insert_user 在模型看来就是 user。"""
    return 'user' if role == INSERT_USER_ROLE else role


# ============= 连接 =============
def _mysql_conn():
    cfg = dict(DB_CONFIG)
    cfg['cursorclass'] = pymysql.cursors.DictCursor
    cfg['autocommit'] = True
    return pymysql.connect(**cfg)


def _sqlite_conn():
    os.makedirs(os.path.dirname(SQLITE_DB_PATH) or '.', exist_ok=True)
    conn = sqlite3.connect(SQLITE_DB_PATH, timeout=30, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    return conn


# ============= 在线同步基线 =============
def _meta_get(conn, key):
    row = conn.execute("SELECT value FROM sync_meta WHERE key=?", (key,)).fetchone()
    return row['value'] if row else None


def _meta_set(conn, key, value):
    conn.execute(
        "INSERT INTO sync_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)))


def _baseline_online_sync():
    """在线同步「开启」时取一次基线：开启之前的历史记录不再回传在线库。

    仅在进入新一轮开启（sync_meta.online_epoch != '1'）时执行：把当前已有的会话/消息
    标记为 synced=1，并记录基线 id/rowid。此后只有基线之后新增的记录才会同步——即
    「打开在线」只记录打开之后的会话，而不是把之前所有记录全量上传。
    """
    with _sqlite_conn() as conn:
        if _meta_get(conn, "online_epoch") == "1":
            return
        msg_max = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM chat_messages").fetchone()['m']
        sess_max = conn.execute("SELECT COALESCE(MAX(rowid), 0) AS m FROM chat_sessions").fetchone()['m']
        conn.execute("UPDATE chat_messages SET synced=1 WHERE id<=?", (msg_max,))
        conn.execute("UPDATE chat_sessions SET synced=1 WHERE rowid<=?", (sess_max,))
        _meta_set(conn, "online_epoch", "1")
        _meta_set(conn, "online_baseline_msg", msg_max)
        _meta_set(conn, "online_baseline_sess", sess_max)
        conn.commit()


def _mark_online_disabled():
    """在线未启用时清除 epoch，使下次开启重新取基线（离线期间记录不回传）。"""
    with _sqlite_conn() as conn:
        if _meta_get(conn, "online_epoch") != "0":
            _meta_set(conn, "online_epoch", "0")
            conn.commit()


# ============= 建表 =============
# uid 允许为空是为了兼容旧库（旧行补 'legacy-<id>' 后再建唯一索引）
_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_sessions (
    id         TEXT PRIMARY KEY,
    status     TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    synced     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    uid               TEXT NULL,
    session_id        TEXT NOT NULL,
    role              TEXT NOT NULL,
    content           TEXT NULL,
    tool_calls_json   TEXT NULL,
    tool_call_id      TEXT NULL,
    reasoning_content TEXT NULL,
    output_items_json TEXT NULL,
    created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    synced            INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sync_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _init_schema_sqlite():
    """SQLite 建表 + 旧库迁移（幂等，可重复调用）"""
    with _sqlite_conn() as conn:
        conn.executescript(_SQLITE_SCHEMA)

        cols = [row['name'] for row in conn.execute("PRAGMA table_info(chat_messages)")]
        if 'reasoning_content' not in cols:
            conn.execute("ALTER TABLE chat_messages ADD COLUMN reasoning_content TEXT")
        if 'output_items_json' not in cols:
            conn.execute("ALTER TABLE chat_messages ADD COLUMN output_items_json TEXT")
        if 'uid' not in cols:
            conn.execute("ALTER TABLE chat_messages ADD COLUMN uid TEXT")
        if 'synced' not in cols:
            conn.execute("ALTER TABLE chat_messages ADD COLUMN synced INTEGER NOT NULL DEFAULT 0")

        s_cols = [row['name'] for row in conn.execute("PRAGMA table_info(chat_sessions)")]
        if 'synced' not in s_cols:
            conn.execute("ALTER TABLE chat_sessions ADD COLUMN synced INTEGER NOT NULL DEFAULT 0")

        # 旧数据补 uid（uid 唯一是幂等同步的去重键）
        conn.execute("UPDATE chat_messages SET uid = 'legacy-' || id WHERE uid IS NULL OR uid = ''")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_msg_uid ON chat_messages (uid)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_session_id ON chat_messages (session_id, id)")
        conn.commit()


def _init_schema_mysql():
    """MySQL 建表 + 旧表迁移（幂等，可重复调用）"""
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
        uid            VARCHAR(64) NULL,
        session_id     VARCHAR(64) NOT NULL,
        role           VARCHAR(20) NOT NULL,
        content        MEDIUMTEXT NULL,
        tool_calls_json TEXT NULL,
        tool_call_id   VARCHAR(64) NULL,
        reasoning_content MEDIUMTEXT NULL,
        output_items_json TEXT NULL,
        created_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        KEY idx_session_id (session_id, id),
        UNIQUE KEY uidx_msg_uid (uid)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """
    with _mysql_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql_sessions)
            cur.execute(sql_messages)
            # 兼容旧表：缺列则补
            cur.execute("SELECT COUNT(*) AS n FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='chat_messages' AND COLUMN_NAME='reasoning_content'")
            if cur.fetchone()['n'] == 0:
                cur.execute("ALTER TABLE chat_messages ADD COLUMN reasoning_content MEDIUMTEXT NULL AFTER tool_call_id")
            cur.execute("SELECT COUNT(*) AS n FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='chat_messages' AND COLUMN_NAME='output_items_json'")
            if cur.fetchone()['n'] == 0:
                cur.execute("ALTER TABLE chat_messages ADD COLUMN output_items_json TEXT NULL AFTER reasoning_content")
            cur.execute("SELECT COUNT(*) AS n FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='chat_messages' AND COLUMN_NAME='uid'")
            if cur.fetchone()['n'] == 0:
                cur.execute("ALTER TABLE chat_messages ADD COLUMN uid VARCHAR(64) NULL AFTER id")
            # uid 唯一索引（幂等同步去重键；旧行 uid 为 NULL 不冲突）
            cur.execute("SELECT COUNT(*) AS n FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='chat_messages' AND INDEX_NAME='uidx_msg_uid'")
            if cur.fetchone()['n'] == 0:
                cur.execute("CREATE UNIQUE INDEX uidx_msg_uid ON chat_messages (uid)")
        conn.commit()


# ============= 会话 =============
def get_active_session_id():
    """取最近有活动的活跃会话；无则返回 None"""
    with _sqlite_conn() as conn:
        row = conn.execute(
            "SELECT id FROM chat_sessions WHERE status='active' ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
    return row['id'] if row else None


def session_exists(session_id):
    with _sqlite_conn() as conn:
        row = conn.execute("SELECT id FROM chat_sessions WHERE id=?", (session_id,)).fetchone()
    return row is not None


def create_session(session_id):
    """新建会话；若 id 已存在则重新激活"""
    with _sqlite_conn() as conn:
        conn.execute(
            "INSERT INTO chat_sessions (id, synced) VALUES (?, 0) "
            "ON CONFLICT(id) DO UPDATE SET status='active', updated_at=CURRENT_TIMESTAMP, synced=0",
            (session_id,))
        conn.commit()
    _sync_pending()
    return session_id


def close_session(session_id):
    """终结指定会话"""
    with _sqlite_conn() as conn:
        conn.execute("UPDATE chat_sessions SET status='closed', synced=0 WHERE id=?", (session_id,))
        conn.commit()
    _sync_pending()


def close_all_active_sessions():
    """终结当前所有活跃会话（用于 -n 新开对话）"""
    with _sqlite_conn() as conn:
        conn.execute("UPDATE chat_sessions SET status='closed', synced=0 WHERE status='active'")
        conn.commit()
    _sync_pending()


def get_session_info(session_id):
    with _sqlite_conn() as conn:
        row = conn.execute(
            "SELECT id, status, created_at, updated_at FROM chat_sessions WHERE id=?", (session_id,)
        ).fetchone()
    return dict(row) if row else None


def count_messages(session_id):
    with _sqlite_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM chat_messages WHERE session_id=?", (session_id,)).fetchone()
    return row['n']


def list_recent_sessions(limit=5):
    """列出最近活动的会话（含消息条数），用于 -s 查看"""
    with _sqlite_conn() as conn:
        rows = conn.execute(
            "SELECT s.id AS id, s.status AS status, s.created_at AS created_at, "
            "s.updated_at AS updated_at, "
            "(SELECT COUNT(*) FROM chat_messages m WHERE m.session_id = s.id) AS n "
            "FROM chat_sessions s ORDER BY s.updated_at DESC, s.id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# ============= 消息 =============
def append_messages(session_id, messages):
    """批量追加消息（字典格式：role/content/tool_calls/tool_call_id/output_items）

    每条消息生成全局唯一 uid 作为同步去重键；先落 SQLite，再幂等同步到 MySQL。
    """
    if not messages:
        return
    with _sqlite_conn() as conn:
        for m in messages:
            tool_calls = m.get('tool_calls')
            output_items = m.get('output_items')
            conn.execute(
                "INSERT INTO chat_messages (uid, session_id, role, content, tool_calls_json, tool_call_id, reasoning_content, output_items_json, synced) "
                "VALUES (?,?,?,?,?,?,?,?,0)",
                (m.get('uid') or uuid.uuid4().hex,
                 session_id,
                 m.get('role'),
                 m.get('content'),
                 json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None,
                 m.get('tool_call_id'),
                 m.get('reasoning_content'),
                 json.dumps(output_items, ensure_ascii=False) if output_items else None))
        # 触碰会话更新时间，保证"默认同一对话"能找到最近会话；并标记需同步
        conn.execute("UPDATE chat_sessions SET updated_at=CURRENT_TIMESTAMP, synced=0 WHERE id=?", (session_id,))
        conn.commit()
    _sync_pending()


def load_messages(session_id):
    with _sqlite_conn() as conn:
        rows = conn.execute(
            "SELECT role, content, tool_calls_json, tool_call_id, reasoning_content, output_items_json FROM chat_messages "
            "WHERE session_id=? ORDER BY id", (session_id,)
        ).fetchall()
    return _build_msgs(rows)


def has_message_with_prefix(session_id, prefix):
    """会话中是否已有以 prefix 开头的内容（用于一次性注入的上下文消息去重）。

    用 substr 做精确前缀匹配，避免 LIKE 通配符转义问题。
    """
    if not prefix:
        return False
    with _sqlite_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM chat_messages WHERE session_id=? AND substr(content, 1, ?) = ? LIMIT 1",
            (session_id, len(prefix), prefix)).fetchone()
    return row is not None


def load_injected_contents(session_id, prefixes):
    """按 id 升序返回会话内以任一 prefix 开头的消息内容（用于增量注入的已声明状态）。"""
    prefixes = [p for p in (prefixes or []) if p]
    if not prefixes:
        return []
    cond = " OR ".join("substr(content, 1, ?) = ?" for _ in prefixes)
    params = [session_id]
    for p in prefixes:
        params.extend([len(p), p])
    with _sqlite_conn() as conn:
        rows = conn.execute(
            f"SELECT content FROM chat_messages WHERE session_id=? AND ({cond}) ORDER BY id",
            params).fetchall()
    return [r['content'] or '' for r in rows]


# ============= 在线同步（SQLite → MySQL，幂等） =============
def _sync_pending():
    """把 SQLite 中 synced=0 的会话/消息推送到在线 MySQL。

    - 幂等：会话按主键 id upsert，消息按唯一索引 uid 去重，重复推送不产生重复行；
    - 不丢失：仅当 MySQL 提交成功后才回写 synced=1，失败保持 0 待下次重试。
    """
    if not _sync_enabled:
        return
    try:
        with _sqlite_conn() as conn:
            sessions = conn.execute(
                "SELECT id, status, created_at, updated_at FROM chat_sessions WHERE synced=0"
            ).fetchall()
            messages = conn.execute(
                "SELECT id, uid, session_id, role, content, tool_calls_json, tool_call_id, "
                "reasoning_content, output_items_json, created_at FROM chat_messages "
                "WHERE synced=0 ORDER BY id"
            ).fetchall()

        if not sessions and not messages:
            return

        with _mysql_conn() as mconn:
            with mconn.cursor() as cur:
                for s in sessions:
                    cur.execute(
                        "INSERT INTO chat_sessions (id, status, created_at, updated_at) VALUES (%s,%s,%s,%s) "
                        "ON DUPLICATE KEY UPDATE status=VALUES(status), updated_at=VALUES(updated_at)",
                        (s['id'], s['status'], s['created_at'], s['updated_at']))
                for m in messages:
                    cur.execute(
                        "INSERT INTO chat_messages (uid, session_id, role, content, tool_calls_json, tool_call_id, reasoning_content, output_items_json, created_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                        "ON DUPLICATE KEY UPDATE session_id=VALUES(session_id)",
                        (m['uid'], m['session_id'], m['role'], m['content'], m['tool_calls_json'],
                         m['tool_call_id'], m['reasoning_content'], m['output_items_json'], m['created_at']))
            mconn.commit()

        # MySQL 提交成功后才标记已同步（失败则保留待重试）
        with _sqlite_conn() as conn:
            conn.executemany("UPDATE chat_sessions SET synced=1 WHERE id=?",
                             [(s['id'],) for s in sessions])
            conn.executemany("UPDATE chat_messages SET synced=1 WHERE id=?",
                             [(m['id'],) for m in messages])
            conn.commit()
    except Exception as e:
        events.ev_warning(f"同步到在线 MySQL 失败（数据已安全保存于本地 SQLite，将在下次启动/写入时重试）：{e}",
                          code="mysql_sync_failed")


# ============= 消息序列构建 =============
def _build_msgs(rows):
    msgs = []
    for row in rows:
        # 注入消息（skill 名单 / OV 块）落库为 insert_user，这里映射回 user，
        # 保证重放给模型的序列与旧库（role='user'）完全一致。
        role = _chat_role(row['role'])
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

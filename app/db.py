"""SQLite 数据访问层。单文件库，表：records / baselines / previews / settings。

并发模型（两个共库 Web 进程也适用）：
- 进程内：所有写操作共用同一把 RLock，保证同进程内“检查-写入”不被插入；
- 跨进程：每个写事务以 BEGIN IMMEDIATE 立即取得 SQLite 全库写锁，
  配合 busy_timeout 排队等待，同一时刻全库只有一个写事务；
- 唯一性启用后，写事务提交前按“整批最终状态”做重复检查，
  发现重复立即回滚、抛出 DuplicateError，记录保持原样。
  因此同事务内交换两条记录的物种等中间重复的操作可以成功，
  而最终状态重复的整批（含离线合并）一律拒绝。
"""

import json
import os
import sqlite3
import threading
from datetime import datetime

from .uniqueness import DuplicateError, find_duplicate_groups

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "..", "survey.db"))

# 所有写操作与确认合并共用同一把锁，保证同进程内“检查-应用”之间不被插入
LOCK = threading.RLock()

# 跨进程排队等待写锁的最长时间
BUSY_TIMEOUT_MS = 10_000


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL 是数据库文件的持久属性，init_db 中设置一次即可；
    # 这里只设 busy_timeout，避免每连接切换 journal_mode 触发排他锁竞争
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def init_db(seed_csv: str | None = None):
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with LOCK:
        conn = connect()
        try:
            # WAL 为数据库持久属性，只需设置一次；两个进程首次并发启动时
            # 该 PRAGMA 需要排他锁，靠 busy_timeout 排队重试
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id      TEXT PRIMARY KEY,
                    quadrat TEXT NOT NULL,
                    species TEXT NOT NULL,
                    count   INTEGER NOT NULL CHECK (count >= 0)
                );
                CREATE TABLE IF NOT EXISTS baselines (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    name       TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    data       TEXT NOT NULL          -- JSON: {id: record}
                );
                CREATE TABLE IF NOT EXISTS previews (
                    token       TEXT PRIMARY KEY,
                    baseline_id INTEGER NOT NULL,
                    created_at  TEXT NOT NULL,
                    status      TEXT NOT NULL,        -- pending/applied/discarded
                    message     TEXT NOT NULL DEFAULT '',
                    revision    TEXT NOT NULL,        -- JSON: {id: record}
                    snapshot    TEXT NOT NULL,        -- JSON: 预览时涉及记录的当前值 {id: record|null}
                    plan        TEXT NOT NULL         -- JSON: compute_plan 的结果
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES ('uniqueness_enabled', '0')"
            )
            conn.commit()
            empty = conn.execute("SELECT COUNT(*) AS n FROM records").fetchone()["n"] == 0
        finally:
            conn.close()

    if empty and seed_csv and os.path.exists(seed_csv):
        from .csvio import parse_csv

        with open(seed_csv, encoding="utf-8") as f:
            seed = parse_csv(f.read())

        def _seed(conn):
            for rid, r in seed.items():
                conn.execute(
                    "INSERT OR IGNORE INTO records (id, quadrat, species, count) VALUES (?,?,?,?)",
                    (rid, r["quadrat"], r["species"], r["count"]),
                )

        # 初始种子数据本身按新口径无重复；仍走统一的带检查写事务
        _checked_commit(_seed)


# ---------- 唯一性启用状态 ----------


def is_enabled() -> bool:
    """每次都从库里读，确保两个共库进程对启用状态的视图一致（重启也保留）。"""
    conn = connect()
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'uniqueness_enabled'"
        ).fetchone()
        return bool(row and row["value"] == "1")
    finally:
        conn.close()


def enabled_at() -> str:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'uniqueness_enabled_at'"
        ).fetchone()
        return row["value"] if row else ""
    finally:
        conn.close()


def snapshot_duplicate_groups() -> list[dict]:
    """只读全库检查：不开写事务、不持写锁，维护与合并可照常进行。

    反映调用瞬间的库内容；真正启用时会在写事务内以切换瞬间的
    完整数据再次复检。
    """
    conn = connect()
    try:
        rows = conn.execute("SELECT id, quadrat, species, count FROM records").fetchall()
    finally:
        conn.close()
    records = {
        row["id"]: {"quadrat": row["quadrat"], "species": row["species"], "count": row["count"]}
        for row in rows
    }
    return find_duplicate_groups(records)


def enable_uniqueness() -> dict:
    """以切换瞬间的完整数据为准启用唯一性。

    - 已启用：幂等返回 ok；
    - 有重复：回滚、不启用，返回 ok=False 与重复组；
    - 启用与维护/合并并发时，由 BEGIN IMMEDIATE 串行化，本事务拿到
      写锁后看到的就是切换瞬间的完整数据，任何失败/中断都不会留下
      “半启用”状态（settings 与复检在同一事务内提交或一起回滚）。
    """
    with LOCK:
        conn = connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if _enabled_in_conn(conn):
                    conn.rollback()
                    return {"ok": True, "already": True, "groups": []}
                rows = conn.execute(
                    "SELECT id, quadrat, species, count FROM records"
                ).fetchall()
                records = {
                    row["id"]: {
                        "quadrat": row["quadrat"],
                        "species": row["species"],
                        "count": row["count"],
                    }
                    for row in rows
                }
                groups = find_duplicate_groups(records)
                if groups:
                    conn.rollback()
                    return {"ok": False, "already": False, "groups": groups}
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES ('uniqueness_enabled', '1')"
                    " ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                )
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES ('uniqueness_enabled_at', ?)"
                    " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (now(),),
                )
                conn.commit()
                return {"ok": True, "already": False, "groups": []}
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()


# ---------- 带唯一性检查的写事务 ----------


def _fetch_all_records(conn) -> dict:
    rows = conn.execute("SELECT id, quadrat, species, count FROM records").fetchall()
    return {
        row["id"]: {
            "quadrat": row["quadrat"],
            "species": row["species"],
            "count": row["count"],
        }
        for row in rows
    }


def _checked_commit(mutate):
    """统一的写事务入口。

    mutate(conn) 在 BEGIN IMMEDIATE 事务内执行全部写入；
    若唯一性已启用，提交前按整批最终状态全库复检，
    发现重复回滚并抛 DuplicateError，调用方数据不变。
    未启用时不做唯一性拦截，保持旧行为。
    """
    with LOCK:
        conn = connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                mutate(conn)
                if _enabled_in_conn(conn):
                    groups = find_duplicate_groups(_fetch_all_records(conn))
                    if groups:
                        conn.rollback()
                        raise DuplicateError(groups)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()


def _enabled_in_conn(conn) -> bool:
    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'uniqueness_enabled'"
    ).fetchone()
    return bool(row and row["value"] == "1")


# ---------- 当前记录 ----------


def get_records() -> dict:
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM records ORDER BY id").fetchall()
    finally:
        conn.close()
    return {
        row["id"]: {"quadrat": row["quadrat"], "species": row["species"], "count": row["count"]}
        for row in rows
    }


def get_records_by_ids(ids) -> dict:
    """返回 {id: record|None}，不存在的 ID 值为 None。"""
    out = {}
    conn = connect()
    try:
        for rid in ids:
            row = conn.execute("SELECT * FROM records WHERE id = ?", (rid,)).fetchone()
            out[rid] = (
                {"quadrat": row["quadrat"], "species": row["species"], "count": row["count"]}
                if row
                else None
            )
    finally:
        conn.close()
    return out


def add_record(rid, quadrat, species, count):
    def _mutate(conn):
        conn.execute(
            "INSERT INTO records (id, quadrat, species, count) VALUES (?,?,?,?)",
            (rid, quadrat, species, count),
        )

    _checked_commit(_mutate)


def update_record(rid, quadrat, species, count) -> bool:
    state = {"changed": False}

    def _mutate(conn):
        cur = conn.execute(
            "UPDATE records SET quadrat=?, species=?, count=? WHERE id=?",
            (quadrat, species, count, rid),
        )
        state["changed"] = cur.rowcount > 0

    _checked_commit(_mutate)
    return state["changed"]


def delete_record(rid) -> bool:
    state = {"changed": False}

    def _mutate(conn):
        cur = conn.execute("DELETE FROM records WHERE id=?", (rid,))
        state["changed"] = cur.rowcount > 0

    _checked_commit(_mutate)
    return state["changed"]


def batch_update_records(changes: dict) -> None:
    """整批人工修改，单事务提交并按整批最终状态检查。

    changes: {id: {"quadrat", "species", "count"}}
    允许同批交换两条记录的物种等“中间重复、最终不重复”的操作；
    最终状态重复则整批回滚、不改变任何记录。
    """
    if not changes:
        return

    def _mutate(conn):
        for rid, r in changes.items():
            cur = conn.execute(
                "UPDATE records SET quadrat=?, species=?, count=? WHERE id=?",
                (r["quadrat"], r["species"], r["count"], rid),
            )
            if cur.rowcount == 0:
                raise ValueError(f"记录 {rid} 不存在")

    _checked_commit(_mutate)


# ---------- 基线 ----------


def save_baseline(name: str, data: dict) -> int:
    state = {"bid": None}

    def _mutate(conn):
        cur = conn.execute(
            "INSERT INTO baselines (name, created_at, data) VALUES (?,?,?)",
            (name, now(), json.dumps(data, ensure_ascii=False)),
        )
        state["bid"] = cur.lastrowid

    _checked_commit(_mutate)
    return state["bid"]


def list_baselines() -> list:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, name, created_at, data FROM baselines ORDER BY id DESC"
        ).fetchall()
    finally:
        conn.close()
    return [
        {"id": r["id"], "name": r["name"], "created_at": r["created_at"], "size": len(json.loads(r["data"]))}
        for r in rows
    ]


def get_baseline(bid: int):
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM baselines WHERE id=?", (bid,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "created_at": row["created_at"],
        "data": json.loads(row["data"]),
    }


# ---------- 合并预览 ----------


def save_preview(token, baseline_id, revision, snapshot, plan):
    def _mutate(conn):
        conn.execute(
            "INSERT INTO previews (token, baseline_id, created_at, status, message, revision, snapshot, plan)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                token,
                baseline_id,
                now(),
                "pending",
                "",
                json.dumps(revision, ensure_ascii=False),
                json.dumps(snapshot, ensure_ascii=False),
                json.dumps(plan, ensure_ascii=False),
            ),
        )

    _checked_commit(_mutate)


def get_preview(token):
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM previews WHERE token=?", (token,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {
        "token": row["token"],
        "baseline_id": row["baseline_id"],
        "created_at": row["created_at"],
        "status": row["status"],
        "message": row["message"],
        "revision": json.loads(row["revision"]),
        "snapshot": json.loads(row["snapshot"]),
        "plan": json.loads(row["plan"]),
    }


def list_previews() -> list:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT token, baseline_id, created_at, status, message FROM previews"
            " ORDER BY created_at DESC, token DESC"
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def update_preview_plan(token, snapshot, plan, message):
    """过期预览被重新比较后，用新计划与新快照替换旧内容。"""

    def _mutate(conn):
        conn.execute(
            "UPDATE previews SET snapshot=?, plan=?, message=?, status='pending', created_at=? WHERE token=?",
            (
                json.dumps(snapshot, ensure_ascii=False),
                json.dumps(plan, ensure_ascii=False),
                message,
                now(),
                token,
            ),
        )

    _checked_commit(_mutate)


def set_preview_status(token, status, message=""):
    def _mutate(conn):
        conn.execute(
            "UPDATE previews SET status=?, message=? WHERE token=?", (status, message, token)
        )

    _checked_commit(_mutate)


def apply_actions(actions) -> None:
    """单事务应用最终动作；启用唯一性时按整批最终状态检查。

    任何失败（含重复拒绝）整体回滚，当前数据不变。
    """

    def _mutate(conn):
        for a in actions:
            if a["action"] == "insert":
                r = a["record"]
                conn.execute(
                    "INSERT INTO records (id, quadrat, species, count) VALUES (?,?,?,?)",
                    (a["id"], r["quadrat"], r["species"], r["count"]),
                )
            elif a["action"] == "update":
                r = a["to"]
                conn.execute(
                    "UPDATE records SET quadrat=?, species=?, count=? WHERE id=?",
                    (r["quadrat"], r["species"], r["count"], a["id"]),
                )
            elif a["action"] == "delete":
                conn.execute("DELETE FROM records WHERE id=?", (a["id"],))
            else:
                raise ValueError(f"未知动作：{a['action']}")

    _checked_commit(_mutate)

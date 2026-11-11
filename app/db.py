"""SQLite 持久化：任务表 + 事件表（事件表保留完整历史，包括历次失败原因）。"""
import sqlite3
from datetime import datetime

from . import config
from .verify import verify_csv, VerifyError

# pending -> waiting -> downloading -> verifying -> done / failed
# waiting 覆盖"确保导出任务存在（查找或创建）+ 轮询等待生成完成"，
# 具体进度看 step 字段。
TRANSIENT_STATUSES = ("downloading", "verifying")
ACTIONABLE_STATUSES = ("pending", "waiting") + TRANSIENT_STATUSES

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  store_id TEXT NOT NULL,
  store_name TEXT NOT NULL,
  date_from TEXT NOT NULL,
  date_to TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  step TEXT,
  error TEXT,
  export_id TEXT,
  file_path TEXT,
  file_sha256 TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (store_id, date_from, date_to)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER,
  ts TEXT NOT NULL,
  level TEXT NOT NULL DEFAULT 'info',
  step TEXT,
  message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id);
"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def get_conn() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)


def add_event(task_id, message, level="info", step=None):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO events (task_id, ts, level, step, message) "
            "VALUES (?,?,?,?,?)", (task_id, now(), level, step, message))


def list_events(task_id):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM events WHERE task_id=? ORDER BY id",
            (task_id,)).fetchall()


def update_task(task_id, **fields):
    fields["updated_at"] = now()
    cols = ", ".join(f"{k}=?" for k in fields)
    with get_conn() as conn:
        conn.execute(f"UPDATE tasks SET {cols} WHERE id=?",
                     (*fields.values(), task_id))


def get_task(task_id):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM tasks WHERE id=?",
                            (task_id,)).fetchone()


def get_task_by_key(store_id, date_from, date_to):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM tasks WHERE store_id=? AND date_from=? AND date_to=?",
            (store_id, date_from, date_to)).fetchone()


def list_tasks():
    with get_conn() as conn:
        return conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()


def next_actionable():
    with get_conn() as conn:
        return conn.execute(
            f"SELECT * FROM tasks WHERE status IN "
            f"({','.join('?' * len(ACTIONABLE_STATUSES))}) ORDER BY id LIMIT 1",
            ACTIONABLE_STATUSES).fetchone()


def reset_inflight():
    """worker 启动时，把下载/校验中途打断的任务退回到 waiting（会重新下载）。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM tasks WHERE status IN ('downloading','verifying')"
        ).fetchall()
    for r in rows:
        update_task(r["id"], status="waiting", step="wait_ready")
        add_event(r["id"], "worker 重启，下载/校验中断的任务回退为等待下载",
                  level="warning")


def reset_failed():
    """手动重试：所有失败任务回到 pending（错误原因保留在事件表里）。"""
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM tasks WHERE status='failed'").fetchall()
    for t in rows:
        update_task(t["id"], status="pending", step=None, error=None)
        add_event(t["id"], f"手动重试（上次失败于 {t['step'] or '?'}："
                           f"{t['error'] or '?'}）")
    return len(rows)


def create_batch(stores, date_from, date_to):
    """创建一批下载任务。stores: [(store_id, store_name), ...]

    重复执行同一批时：
      * 已完成且文件仍通过内容校验 -> 复用，不动；
      * 已完成但文件损坏/内容不符 -> 记录原因，重新下载；
      * 失败/未完成 -> 重新尝试；
      * 正在流程中 -> 跳过。
    """
    summary = {"created": 0, "reused": 0, "redownload": 0,
               "retry": 0, "skipped": 0}
    for store_id, store_name in stores:
        t = get_task_by_key(store_id, date_from, date_to)
        if t is None:
            with get_conn() as conn:
                cur = conn.execute(
                    "INSERT INTO tasks (store_id, store_name, date_from, date_to,"
                    " status, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                    (store_id, store_name, date_from, date_to,
                     "pending", now(), now()))
                tid = cur.lastrowid
            add_event(tid, "创建任务")
            summary["created"] += 1
            continue

        if t["status"] == "done":
            try:
                verify_csv(t["file_path"], store_id=store_id,
                           store_name=store_name,
                           date_from=date_from, date_to=date_to)
            except VerifyError as e:
                add_event(t["id"], f"已有文件未通过校验（{e.message}），重新下载",
                          level="warning")
                update_task(t["id"], status="pending", step=None, error=None,
                            file_path=None, file_sha256=None)
                summary["redownload"] += 1
            else:
                add_event(t["id"], "复用已核验文件，无需重新下载")
                summary["reused"] += 1
        elif t["status"] == "failed":
            add_event(t["id"], f"重新尝试失败任务（上次失败于 "
                               f"{t['step'] or '?'}：{t['error'] or '?'}）")
            update_task(t["id"], status="pending", step=None, error=None)
            summary["retry"] += 1
        else:
            summary["skipped"] += 1
    return summary

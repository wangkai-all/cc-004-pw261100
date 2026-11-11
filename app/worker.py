"""下载 worker：在独立线程里用 Playwright 操作报表站点页面。

每个任务的生命周期（状态存 SQLite，可随时暂停/续跑）：
  pending -> waiting（确保导出存在：优先复用页面已有导出，否则创建；
                     轮询直到可下载或生成失败）
          -> downloading -> verifying -> done / failed

失败时记录失败步骤（step）和原因（error），历史全部留在 events 表。
"""
import re
import threading
import time

from . import config, db
from .verify import verify_csv, VerifyError

STATUS_ZH = {"queued": "排队中", "processing": "生成中",
             "ready": "可下载", "failed": "生成失败"}

STORE_RE = re.compile(r"\((S\d+)\)")


class Paused(Exception):
    """处理途中收到暂停请求；任务状态已持久化，可随时续跑。"""


class TaskFailed(Exception):
    def __init__(self, step, message):
        super().__init__(message)
        self.step = step
        self.message = message


class Worker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="report-worker")
        self._pause = threading.Event()
        self._pause.set()          # 初始为暂停状态，等用户在页面上点"开始"
        self._stop = threading.Event()
        self.state = {"mode": "paused", "current_task": None,
                      "message": "未启动"}
        self._pw = self._browser = self._context = self.page = None

    # ---------- 外部控制 ----------

    def resume(self):
        self._pause.clear()

    def pause(self):
        self._pause.set()

    def shutdown(self):
        self._stop.set()

    def is_paused(self) -> bool:
        return self._pause.is_set()

    # ---------- 浏览器 ----------

    def _ensure_browser(self):
        if self.page is not None:
            return
        config.ensure_browser_libs()
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch()
        self._context = self._browser.new_context(accept_downloads=True)
        self.page = self._context.new_page()

    def _close_browser(self):
        for obj in (self._context, self._browser, self._pw):
            try:
                if obj is not None:
                    obj.close() if obj is not self._pw else obj.stop()
            except Exception:
                pass
        self._pw = self._browser = self._context = self.page = None

    # ---------- 页面操作 ----------

    def _login(self, tid=None):
        self.page.goto(config.REPORT_BASE_URL + "/login")
        self.page.fill("#username", config.REPORT_USER)
        self.page.fill("#password", config.REPORT_PASS)
        self.page.click("#login-submit")
        self.page.wait_for_url("**/reports**", timeout=10000)

    def _open_reports(self, tid=None):
        """打开报表页；若被重定向到登录页（会话过期）则重新登录。"""
        self.page.goto(config.REPORT_BASE_URL + "/reports")
        if "/login" in self.page.url or self.page.query_selector("#login-form"):
            self._login(tid)
            self.page.goto(config.REPORT_BASE_URL + "/reports")
            db.add_event(tid, "会话已过期，已重新登录", level="warning",
                         step="login")

    def _reload_reports(self, tid):
        self.page.reload()
        if "/login" in self.page.url or self.page.query_selector("#login-form"):
            self._login(tid)
            self.page.goto(config.REPORT_BASE_URL + "/reports")
            db.add_event(tid, "会话已过期，已重新登录", level="warning",
                         step="login")

    def _read_rows(self):
        """解析报表页上的导出任务列表（新任务在最上面）。"""
        rows = []
        for tr in self.page.query_selector_all("tr.export-row"):
            def txt(sel):
                el = tr.query_selector(sel)
                return (el.text_content() or "").strip() if el else ""
            store_txt = txt(".c-store")
            m = STORE_RE.search(store_txt)
            dates = [p.strip() for p in txt(".c-dates").split("~")]
            st_el = tr.query_selector(".c-status span")
            link = tr.query_selector("a.download-link")
            rows.append({
                "id": tr.get_attribute("data-export-id"),
                "store_id": m.group(1) if m else None,
                "store_text": store_txt,
                "date_from": dates[0] if len(dates) == 2 else None,
                "date_to": dates[1] if len(dates) == 2 else None,
                "status": st_el.get_attribute("data-status") if st_el else None,
                "reason": txt(".c-action") if not link else "",
                "link": link,
            })
        return rows

    def _find_row_by_id(self, eid):
        for r in self._read_rows():
            if r["id"] == eid:
                return r
        return None

    def _find_row_by_key(self, task):
        for r in self._read_rows():
            if (r["store_id"] == task["store_id"]
                    and r["date_from"] == task["date_from"]
                    and r["date_to"] == task["date_to"]):
                return r
        return None

    def _ensure_export(self, task):
        """确保站点上存在该任务的导出：优先复用，不重复创建仍在排队的导出。"""
        tid = task["id"]
        eid = task["export_id"]
        if eid:
            row = self._find_row_by_id(eid)
            if row and row["status"] != "failed":
                db.add_event(tid, f"复用页面已有导出任务 {eid}"
                                  f"（状态：{STATUS_ZH.get(row['status'])}），"
                                  f"不重复创建", step="create_export")
                return eid
        row = self._find_row_by_key(task)
        if row and row["status"] != "failed":
            db.update_task(tid, export_id=row["id"])
            db.add_event(tid, f"页面已存在匹配的导出任务 {row['id']}"
                              f"（状态：{STATUS_ZH.get(row['status'])}），"
                              f"直接复用，不重复创建", step="create_export")
            return row["id"]

        # 创建新导出
        self.page.select_option("#store", task["store_id"])
        self.page.fill("#date_from", task["date_from"])
        self.page.fill("#date_to", task["date_to"])
        try:
            with self.page.expect_navigation(timeout=8000):
                self.page.click("#export-submit")
        except Exception:
            pass
        err = self.page.query_selector(".form-error")
        if err:
            raise TaskFailed("create_export",
                             f"站点拒绝导出请求：{err.text_content().strip()}")
        row = self._find_row_by_key(task)
        if row is None:
            self._reload_reports(tid)
            row = self._find_row_by_key(task)
        if row is None:
            raise TaskFailed("create_export", "提交导出后页面上找不到对应任务")
        db.update_task(tid, export_id=row["id"])
        db.add_event(tid, f"已创建导出任务 {row['id']}", step="create_export")
        return row["id"]

    def _wait_ready(self, task, eid):
        tid = task["id"]
        db.update_task(tid, step="wait_ready")
        last = None
        while True:
            if self.is_paused():
                raise Paused()
            row = self._find_row_by_id(eid)
            if row is None:
                raise TaskFailed("wait_ready",
                                 f"导出任务 {eid} 在页面上消失了")
            st = row["status"]
            if st != last:
                db.add_event(tid, f"导出任务 {eid} 状态：{STATUS_ZH.get(st, st)}",
                             step="wait_ready")
                last = st
            if st == "ready":
                return row
            if st == "failed":
                raise TaskFailed("wait_ready",
                                 f"报表生成失败：{row['reason'] or '站点未给出原因'}")
            time.sleep(config.POLL_INTERVAL)
            self._reload_reports(tid)

    def _check_page_row(self, task, row):
        """下载前核对页面上显示的门店和日期。"""
        problems = []
        if row["store_id"] != task["store_id"]:
            problems.append(f"页面门店 {row['store_text']!r} "
                            f"与任务 {task['store_id']} 不符")
        if (row["date_from"], row["date_to"]) != (task["date_from"],
                                                  task["date_to"]):
            problems.append(f"页面日期 {row['date_from']} ~ {row['date_to']} "
                            f"与任务 {task['date_from']} ~ {task['date_to']} 不符")
        if problems:
            raise TaskFailed("verify_page", "；".join(problems))

    def _quarantine(self, tmp, tid):
        config.QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
        dst = config.QUARANTINE_DIR / f"task{tid}-{int(time.time())}.csv"
        tmp.replace(dst)
        return dst

    def _download_and_verify(self, task, eid):
        tid = task["id"]
        for attempt in range(1, config.MAX_DOWNLOAD_ATTEMPTS + 1):
            if self.is_paused():
                raise Paused()
            db.update_task(tid, status="downloading", step="download")
            db.add_event(tid, f"开始下载（第 {attempt} 次）", step="download")
            row = self._find_row_by_id(eid)
            if row is None or row["status"] != "ready" or row["link"] is None:
                self._open_reports(tid)
                row = self._find_row_by_id(eid)
            if row is None or row["status"] != "ready" or row["link"] is None:
                raise TaskFailed("download", f"导出任务 {eid} 当前不可下载")

            config.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
            tmp = config.DOWNLOAD_DIR / f".tmp-{tid}-{attempt}.csv"
            with self.page.expect_download(timeout=30000) as dl_info:
                row["link"].click()
            dl_info.value.save_as(str(tmp))

            db.update_task(tid, status="verifying", step="verify_content")
            try:
                sha = verify_csv(tmp, store_id=task["store_id"],
                                 store_name=task["store_name"],
                                 date_from=task["date_from"],
                                 date_to=task["date_to"])
            except VerifyError as e:
                if e.code == "session_expired":
                    tmp.unlink(missing_ok=True)
                    db.add_event(tid, "下载到登录页（会话已过期），"
                                      "重新登录后重试",
                                 level="warning", step="download")
                    self._login(tid)
                    self._open_reports(tid)
                    continue
                bad = self._quarantine(tmp, tid)
                raise TaskFailed("verify_content",
                                 f"{e.message}（文件已隔离：{bad.name}）")

            final = (config.DOWNLOAD_DIR /
                     f"{task['store_id']}_{task['date_from']}_{task['date_to']}.csv")
            tmp.replace(final)
            db.update_task(tid, status="done", step="verify_content",
                           error=None, file_path=str(final), file_sha256=sha)
            db.add_event(tid, f"内容校验通过（门店/日期范围/必需字段），"
                              f"sha256={sha[:12]}…", step="verify_content")
            db.add_event(tid, "任务完成")
            return
        raise TaskFailed("download",
                         f"连续 {config.MAX_DOWNLOAD_ATTEMPTS} 次下载到登录页")

    # ---------- 任务处理 ----------

    def process(self, task):
        tid = task["id"]
        if task["status"] == "pending":
            db.update_task(tid, status="waiting", step="create_export",
                           error=None, attempts=task["attempts"] + 1)
            db.add_event(tid, "开始处理任务", step="create_export")
        self._ensure_browser()
        self._open_reports(tid)
        eid = self._ensure_export(task)
        row = self._wait_ready(task, eid)
        db.update_task(tid, step="verify_page")
        self._check_page_row(task, row)
        db.add_event(tid, "页面门店与日期核对通过", step="verify_page")
        self._download_and_verify(task, eid)

    def run(self):
        db.init_db()
        db.reset_inflight()
        while not self._stop.is_set():
            if self.is_paused():
                self.state.update(mode="paused", current_task=None,
                                  message="已暂停")
                time.sleep(0.5)
                continue
            task = db.next_actionable()
            if task is None:
                self.state.update(mode="idle", current_task=None,
                                  message="没有待处理任务")
                time.sleep(1)
                continue
            tid = task["id"]
            self.state.update(mode="running", current_task=tid,
                              message=f"处理任务 #{tid}")
            try:
                self.process(task)
            except Paused:
                db.add_event(tid, "已暂停，等待继续")
            except TaskFailed as e:
                db.update_task(tid, status="failed", step=e.step,
                               error=e.message)
                db.add_event(tid, f"任务失败：{e.message}", level="error",
                             step=e.step)
            except Exception as e:  # 浏览器崩溃等：重建浏览器后继续
                db.update_task(tid, status="failed", step="worker",
                               error=f"未预期错误：{e}")
                db.add_event(tid, f"worker 异常：{e}", level="error")
                self._close_browser()
            time.sleep(0.2)
        self._close_browser()

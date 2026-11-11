"""模拟经营报表网站。

提供：登录（服务端会话，会过期）、报表页（选择门店和日期范围发起导出）、
导出队列（排队中 -> 生成中 -> 可下载 / 生成失败）、CSV 下载。

内置的确定性场景（按门店区分，便于演示和测试）：
  S001 北京朝阳店 / S002 上海徐汇店：正常
  S003 广州天河店：导出生成失败
  S004 深圳南山店：生成的 CSV 内容里门店是 S999（门店不匹配）
  S005 杭州西湖店：生成的 CSV 内容日期整体偏移到上月（日期范围不匹配）

会话相关陷阱：
  * 会话有 TTL（MOCK_SESSION_TTL 秒，默认 45），过期后页面跳登录；
    此时请求下载地址会拿到一个"扩展名为 .csv 的登录页"（Content-Disposition
    为 attachment，文件名与正常报表一致，内容是 HTML）。
  * MOCK_TRAP_DOWNLOADS=1 时，每个导出任务的第一次下载会返回上述陷阱，
    用来确定性地模拟"会话恰好在下载前过期"（重新登录后重试即可成功）。

环境变量：
  MOCK_PORT          端口（仅 __main__ 直接用，默认 5001）
  MOCK_SESSION_TTL   会话秒数，默认 45
  MOCK_QUEUE_SECS    导出排队秒数，默认 4
  MOCK_PROC_SECS     导出生成秒数，默认 3
  MOCK_TRAP_DOWNLOADS  每次登录后首次下载返回登录页陷阱，默认 1
"""
import csv
import hashlib
import io
import os
import secrets
import threading
import time
from datetime import datetime

from flask import (Flask, Response, abort, jsonify, redirect,
                   render_template_string, request, session, url_for)

STORES = [
    {"id": "S001", "name": "北京朝阳店"},
    {"id": "S002", "name": "上海徐汇店"},
    {"id": "S003", "name": "广州天河店"},
    {"id": "S004", "name": "深圳南山店"},
    {"id": "S005", "name": "杭州西湖店"},
]
STORE_BY_ID = {s["id"]: s for s in STORES}

FAIL_STORES = {"S003"}          # 导出生成失败
MISMATCH_STORE = {"S004"}       # CSV 内容门店不匹配
MISMATCH_DATES = {"S005"}       # CSV 内容日期范围不匹配

USERNAME = "admin"
PASSWORD = "admin123"

STATUS_ZH = {"queued": "排队中", "processing": "生成中",
             "ready": "可下载", "failed": "生成失败"}

LOGIN_PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>登录 - 经营报表系统</title>
<style>
 body{font-family:system-ui,sans-serif;background:#f3f4f6;display:flex;justify-content:center;padding-top:80px}
 .box{background:#fff;padding:28px 32px;border-radius:10px;box-shadow:0 2px 12px #0002;width:320px}
 h1{font-size:20px;margin:0 0 16px}
 label{display:block;margin:10px 0 4px;font-size:14px;color:#374151}
 input{width:100%;padding:8px;border:1px solid #d1d5db;border-radius:6px;box-sizing:border-box}
 button{margin-top:16px;width:100%;padding:9px;background:#2563eb;color:#fff;border:0;border-radius:6px;cursor:pointer}
 .msg{background:#fef2f2;color:#b91c1c;padding:8px 10px;border-radius:6px;font-size:13px}
</style></head><body>
<div class="box">
 <h1>经营报表系统</h1>
 {% if message %}<p class="msg">{{ message }}</p>{% endif %}
 <form id="login-form" method="post" action="/login">
  <label>账号</label><input id="username" name="username" autocomplete="username">
  <label>密码</label><input id="password" name="password" type="password" autocomplete="current-password">
  <button id="login-submit" type="submit">登录</button>
 </form>
</div></body></html>"""

REPORTS_PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>报表中心 - 经营报表系统</title>
<style>
 body{font-family:system-ui,sans-serif;margin:0;background:#f3f4f6;color:#111827}
 header{background:#1f2937;color:#fff;padding:10px 20px;display:flex;gap:16px;align-items:baseline}
 header a{color:#93c5fd;font-size:13px}
 main{max-width:1000px;margin:20px auto;padding:0 16px}
 .panel{background:#fff;border-radius:10px;padding:16px 20px;margin-bottom:18px;box-shadow:0 1px 4px #0001}
 h2{font-size:16px;margin:0 0 12px}
 label{margin-right:14px;font-size:14px}
 input,select{padding:6px;border:1px solid #d1d5db;border-radius:6px}
 button{padding:7px 16px;background:#2563eb;color:#fff;border:0;border-radius:6px;cursor:pointer}
 table{width:100%;border-collapse:collapse;font-size:14px}
 th,td{padding:8px 10px;border-bottom:1px solid #e5e7eb;text-align:left}
 th{background:#f9fafb;color:#6b7280;font-weight:600}
 .st-queued{color:#92400e}.st-processing{color:#1d4ed8}.st-ready{color:#047857}.st-failed{color:#b91c1c}
 .fail-reason{color:#b91c1c;font-size:13px}
 .form-error{background:#fef2f2;color:#b91c1c;padding:8px 10px;border-radius:6px;font-size:13px;margin-top:10px}
 .muted{color:#9ca3af;font-size:13px}
</style></head><body>
<header><b>经营报表系统 · 报表中心</b>
 <span class="muted" style="color:#9ca3af">当前用户：{{ user }}（会话剩余 {{ ttl }} 秒）</span>
 <a href="/logout">退出登录</a></header>
<main>
 <section class="panel">
  <h2>新建导出</h2>
  <form id="export-form" method="post" action="/reports/export">
   <label>门店
    <select id="store" name="store_id">
     {% for s in stores %}<option value="{{ s.id }}">{{ s.name }} ({{ s.id }})</option>{% endfor %}
    </select></label>
   <label>开始日期 <input id="date_from" name="date_from" type="date" required></label>
   <label>结束日期 <input id="date_to" name="date_to" type="date" required></label>
   <button id="export-submit" type="submit">导出报表</button>
  </form>
  {% if error %}<p class="form-error">{{ error }}</p>{% endif %}
 </section>
 <section class="panel">
  <h2>导出任务</h2>
  {% if exports %}
  <table id="exports">
   <thead><tr><th>任务ID</th><th>门店</th><th>日期范围</th><th>状态</th><th>操作</th><th>创建时间</th></tr></thead>
   <tbody>
   {% for e in exports %}
    <tr class="export-row" data-export-id="{{ e.id }}">
     <td class="c-id">{{ e.id }}</td>
     <td class="c-store">{{ e.store_name }} ({{ e.store_id }})</td>
     <td class="c-dates">{{ e.date_from }} ~ {{ e.date_to }}</td>
     <td class="c-status"><span data-status="{{ e.status }}" class="st-{{ e.status }}">{{ e.status_zh }}</span></td>
     <td class="c-action">
      {%- if e.status == 'ready' -%}
       <a class="download-link" href="/exports/{{ e.id }}/download">下载</a>
      {%- elif e.status == 'failed' -%}
       <span class="fail-reason">{{ e.fail_reason }}</span>
      {%- else -%}-{%- endif -%}
     </td>
     <td class="c-created">{{ e.created_str }}</td>
    </tr>
   {% endfor %}
   </tbody>
  </table>
  {% else %}<p class="muted">暂无导出任务</p>{% endif %}
 </section>
</main></body></html>"""


def daterange(date_from: str, date_to: str):
    d0 = datetime.strptime(date_from, "%Y-%m-%d").date()
    d1 = datetime.strptime(date_to, "%Y-%m-%d").date()
    days = []
    while d0 <= d1:
        days.append(d0.isoformat())
        d0 = d0.fromordinal(d0.toordinal() + 1)
    return days


def shift_prev_month(ds: str) -> str:
    """把日期整体偏移到上个月（日号截断到 28，保证确定性）。"""
    y, m, d = map(int, ds.split("-"))
    m -= 1
    if m == 0:
        m, y = 12, y - 1
    return f"{y:04d}-{m:02d}-{min(d, 28):02d}"


def build_csv(exp: dict) -> str:
    """生成报表 CSV。数据由 (门店, 日期) 确定性生成，重复导出内容一致。"""
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["date", "store_id", "store_name", "sales_amount", "order_count"])
    sid, sname = exp["store_id"], exp["store_name"]
    for day in daterange(exp["date_from"], exp["date_to"]):
        row_sid, row_sname, row_date = sid, sname, day
        if sid in MISMATCH_STORE:          # 场景：内容里的门店不对
            row_sid, row_sname = "S999", "未知门店"
        if sid in MISMATCH_DATES:          # 场景：内容里的日期范围不对
            row_date = shift_prev_month(day)
        seed = int(hashlib.md5(f"{row_sid}|{row_date}".encode()).hexdigest()[:8], 16)
        sales = f"{5000 + (seed % 90000) / 100:.2f}"
        orders = 50 + seed % 400
        w.writerow([row_date, row_sid, row_sname, sales, orders])
    return out.getvalue()


def report_filename(exp: dict) -> str:
    return f"report_{exp['store_id']}_{exp['date_from']}_{exp['date_to']}.csv"


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = "mock-site-secret"

    queue_secs = float(os.environ.get("MOCK_QUEUE_SECS", "4"))
    proc_secs = float(os.environ.get("MOCK_PROC_SECS", "3"))
    session_ttl = float(os.environ.get("MOCK_SESSION_TTL", "45"))
    trap_downloads = os.environ.get("MOCK_TRAP_DOWNLOADS", "1") == "1"

    lock = threading.Lock()
    sessions = {}   # token -> {"user": str, "expires_at": float}
    exports = {}    # export_id -> dict
    trapped = set()  # 已经触发过下载陷阱的 export_id
    counter = [1000]

    # ---------- 会话 ----------

    def current_user():
        tok = session.get("token")
        info = sessions.get(tok)
        if info and time.time() < info["expires_at"]:
            return info["user"]
        return None

    def session_ttl_left():
        tok = session.get("token")
        info = sessions.get(tok)
        return max(0, int(info["expires_at"] - time.time())) if info else 0

    def render_login(message=None):
        return render_template_string(LOGIN_PAGE, message=message)

    @app.before_request
    def guard():
        path = request.path
        if not path.startswith(("/reports", "/exports")):
            return None
        if current_user() is not None:
            return None
        if path.startswith("/exports/") and path.endswith("/download"):
            # 会话过期后请求下载：返回一个"扩展名为 .csv 的登录页"。
            eid = path.split("/")[2]
            exp = exports.get(eid)
            fname = report_filename(exp) if exp else "report.csv"
            html = render_login("会话已过期，请重新登录")
            return Response(
                html, status=200, content_type="text/html; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="{fname}"'})
        return redirect(url_for("login"))

    # ---------- 页面 ----------

    @app.route("/")
    def index():
        return redirect(url_for("reports") if current_user() else url_for("login"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            if (request.form.get("username") == USERNAME
                    and request.form.get("password") == PASSWORD):
                tok = secrets.token_hex(16)
                with lock:
                    sessions[tok] = {"user": USERNAME,
                                     "expires_at": time.time() + session_ttl}
                session["token"] = tok
                return redirect(url_for("reports"))
            return render_login("账号或密码错误"), 401
        return render_login()

    @app.route("/logout")
    def logout():
        with lock:
            sessions.pop(session.get("token"), None)
        session.clear()
        return redirect(url_for("login"))

    def export_status(exp):
        el = time.time() - exp["created_at"]
        if el < queue_secs:
            return "queued"
        if el < queue_secs + proc_secs:
            return "processing"
        return "failed" if exp["fail"] else "ready"

    def view_exports():
        with lock:
            items = sorted(exports.values(), key=lambda e: e["created_at"],
                           reverse=True)
        out = []
        for e in items:
            st = export_status(e)
            out.append({**e, "status": st, "status_zh": STATUS_ZH[st],
                        "created_str": datetime.fromtimestamp(e["created_at"])
                        .strftime("%Y-%m-%d %H:%M:%S")})
        return out

    @app.route("/reports")
    def reports():
        return render_template_string(
            REPORTS_PAGE, user=current_user(), ttl=session_ttl_left(),
            stores=STORES, exports=view_exports(), error=None)

    @app.route("/reports/export", methods=["POST"])
    def create_export():
        store_id = request.form.get("store_id", "")
        date_from = request.form.get("date_from", "")
        date_to = request.form.get("date_to", "")
        error = None
        if store_id not in STORE_BY_ID:
            error = "请选择有效的门店"
        else:
            try:
                days = daterange(date_from, date_to)
                if len(days) > 62:
                    error = "日期范围不能超过 62 天"
            except Exception:
                error = "日期格式不正确，或开始日期晚于结束日期"
        if error:
            return render_template_string(
                REPORTS_PAGE, user=current_user(), ttl=session_ttl_left(),
                stores=STORES, exports=view_exports(), error=error), 400
        with lock:
            counter[0] += 1
            eid = f"E{counter[0]}"
            exports[eid] = {
                "id": eid, "store_id": store_id,
                "store_name": STORE_BY_ID[store_id]["name"],
                "date_from": date_from, "date_to": date_to,
                "created_at": time.time(),
                "fail": store_id in FAIL_STORES,
                "fail_reason": "数据源连接超时" if store_id in FAIL_STORES else "",
            }
        return redirect(url_for("reports"), code=303)

    @app.route("/exports/<eid>/download")
    def download(eid):
        exp = exports.get(eid)
        if exp is None:
            abort(404)
        # 陷阱：该导出任务的第一次下载，模拟"会话恰好在下载前过期"。
        with lock:
            trap = trap_downloads and eid not in trapped
            if trap:
                trapped.add(eid)
        if trap:
            html = render_login("会话已过期，请重新登录")
            return Response(
                html, status=200, content_type="text/html; charset=utf-8",
                headers={"Content-Disposition":
                         f'attachment; filename="{report_filename(exp)}"'})
        st = export_status(exp)
        if st == "failed":
            return "报表生成失败，无法下载", 410
        if st != "ready":
            return "报表尚未生成完成", 409
        return Response(
            build_csv(exp).encode("utf-8-sig"),
            content_type="text/csv; charset=utf-8",
            headers={"Content-Disposition":
                     f'attachment; filename="{report_filename(exp)}"'})

    # ---------- 辅助接口 ----------

    @app.route("/api/stores")
    def api_stores():
        return jsonify(STORES)

    @app.route("/healthz")
    def healthz():
        return "ok"

    @app.route("/debug/expire", methods=["POST"])
    def debug_expire():
        """让所有会话立即过期（测试/演示用）。"""
        with lock:
            for info in sessions.values():
                info["expires_at"] = 0
        return "expired"

    return app


if __name__ == "__main__":
    port = int(os.environ.get("MOCK_PORT", "5001"))
    create_app().run(host="127.0.0.1", port=port)

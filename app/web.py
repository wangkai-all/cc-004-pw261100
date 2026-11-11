"""任务页面：创建下载批次、查看每个任务的状态/步骤/失败原因、控制 worker。"""
import json
import urllib.request
from datetime import date

from flask import Flask, jsonify, redirect, render_template, request, url_for

from . import config, db
from .worker import Worker

STATUS_ZH = {"pending": "待处理", "waiting": "等待导出",
             "downloading": "下载中", "verifying": "校验中",
             "done": "已完成", "failed": "失败"}
STEP_ZH = {"login": "登录", "create_export": "创建导出",
           "wait_ready": "等待生成", "verify_page": "核对页面信息",
           "download": "下载文件", "verify_content": "校验内容",
           "worker": "worker"}

# 模拟站点不可达时的兜底门店列表（正常情况从站点 /api/stores 获取）
FALLBACK_STORES = [
    {"id": "S001", "name": "北京朝阳店"},
    {"id": "S002", "name": "上海徐汇店"},
    {"id": "S003", "name": "广州天河店"},
    {"id": "S004", "name": "深圳南山店"},
    {"id": "S005", "name": "杭州西湖店"},
]

worker = Worker()


def fetch_stores():
    try:
        with urllib.request.urlopen(config.REPORT_BASE_URL + "/api/stores",
                                    timeout=2) as resp:
            return json.loads(resp.read().decode()), None
    except Exception as e:
        return FALLBACK_STORES, f"无法连接报表站点（{e}），使用内置门店列表"


def task_json(t):
    d = dict(t)
    d["status_zh"] = STATUS_ZH.get(t["status"], t["status"])
    d["step_zh"] = STEP_ZH.get(t["step"] or "", t["step"] or "-")
    return d


def create_app():
    app = Flask(__name__)
    db.init_db()
    if not worker.is_alive():
        worker.start()

    @app.route("/")
    def index():
        stores, warn = fetch_stores()
        return render_template(
            "index.html", stores=stores, warn=warn,
            default_from=date.today().replace(day=1).isoformat(),
            default_to=date.today().isoformat(),
            report_url=config.REPORT_BASE_URL)

    @app.route("/batch", methods=["POST"])
    def batch():
        store_ids = request.form.getlist("stores")
        date_from = request.form.get("date_from", "")
        date_to = request.form.get("date_to", "")
        stores, _ = fetch_stores()
        by_id = {s["id"]: s["name"] for s in stores}
        chosen = [(sid, by_id[sid]) for sid in store_ids if sid in by_id]
        error = None
        try:
            if not chosen:
                raise ValueError("请至少选择一个门店")
            d0, d1 = date.fromisoformat(date_from), date.fromisoformat(date_to)
            if d0 > d1:
                raise ValueError("开始日期不能晚于结束日期")
            if (d1 - d0).days > 61:
                raise ValueError("日期范围不能超过 62 天")
        except ValueError as e:
            error = str(e)
        if error:
            stores2, warn = fetch_stores()
            return render_template("index.html", stores=stores2, warn=warn,
                                   error=error, default_from=date_from,
                                   default_to=date_to,
                                   report_url=config.REPORT_BASE_URL), 400
        summary = db.create_batch(chosen, date_from, date_to)
        db.add_event(None, f"创建批次 {date_from} ~ {date_to}："
                           f"新建 {summary['created']}，复用 {summary['reused']}，"
                           f"重新下载 {summary['redownload']}，"
                           f"重试 {summary['retry']}，跳过 {summary['skipped']}")
        return redirect(url_for("index"))

    @app.route("/control/<action>", methods=["POST"])
    def control(action):
        if action == "start":
            worker.resume()
        elif action == "pause":
            worker.pause()
        elif action == "retry_failed":
            db.reset_failed()
        return redirect(url_for("index"))

    @app.route("/api/status")
    def api_status():
        return jsonify({
            "worker": {**worker.state, "paused": worker.is_paused()},
            "tasks": [task_json(t) for t in db.list_tasks()],
        })

    @app.route("/task/<int:tid>")
    def task_detail(tid):
        t = db.get_task(tid)
        if t is None:
            return "任务不存在", 404
        return render_template("task.html", t=task_json(t),
                               events=db.list_events(tid))

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=config.WEB_PORT)

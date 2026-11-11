"""端到端测试：真实启动模拟站点 + 任务页面 + Playwright worker。

覆盖：
  * 正常下载（S001/S002）
  * 导出生成失败（S003）——失败步骤应为 wait_ready
  * 下载内容门店不匹配（S004）/ 日期范围不匹配（S005）——失败步骤 verify_content
  * 会话过期：下载到"扩展名为 .csv 的登录页"——重新登录并重试，不得标记成功
  * 重复执行同一批：已核验文件复用；文件损坏后重新下载，错误原因保留
  * 暂停/继续：接着处理未完成项，不重复创建仍在排队的导出任务
"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

TMP = tempfile.mkdtemp(prefix="rd-e2e-")
os.environ.update({
    "RD_DATA_DIR": f"{TMP}/data",
    "RD_DOWNLOAD_DIR": f"{TMP}/downloads",
    "RD_DB_PATH": f"{TMP}/data/tasks.db",
    "REPORT_BASE_URL": "http://127.0.0.1:5111",
    "RD_POLL_INTERVAL": "0.5",
    "RD_MAX_DOWNLOAD_ATTEMPTS": "5",
    "MOCK_QUEUE_SECS": "1",
    "MOCK_PROC_SECS": "1",
    "MOCK_SESSION_TTL": "8",      # 短会话，运行中必然过期，考验重新登录
    "MOCK_TRAP_DOWNLOADS": "1",   # 每次登录后的首次下载返回"登录页.csv"
})

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from werkzeug.serving import make_server  # noqa: E402

from app import config, db  # noqa: E402
from app.verify import verify_csv  # noqa: E402
from app.web import create_app as create_web  # noqa: E402
from mock_site.app import create_app as create_mock  # noqa: E402

MOCK = "http://127.0.0.1:5111"
WEB = "http://127.0.0.1:5112"
ALL_STORES = ["S001", "S002", "S003", "S004", "S005"]
D0, D1 = "2026-10-01", "2026-10-07"


def post(path, data=None):
    body = urllib.parse.urlencode(data or {}, doseq=True).encode()
    req = urllib.request.Request(WEB + path, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status


def api(path):
    with urllib.request.urlopen(WEB + path, timeout=15) as r:
        return json.loads(r.read().decode())


def wait_until(cond, timeout=60, interval=0.2, desc="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(interval)
    raise AssertionError(f"超时等待：{desc}")


def tasks_by_store(date_from):
    return {t["store_id"]: t for t in api("/api/status")["tasks"]
            if t["date_from"] == date_from}


def wait_all_terminal(date_from, timeout=180):
    def done():
        ts = tasks_by_store(date_from)
        return ts and all(t["status"] in ("done", "failed")
                          for t in ts.values())
    wait_until(done, timeout=timeout, desc="全部任务到达终态")


def all_events(tid):
    return [e["message"] for e in db.list_events(tid)]


@pytest.fixture(scope="module", autouse=True)
def servers():
    mock = make_server("127.0.0.1", 5111, create_mock())
    web = make_server("127.0.0.1", 5112, create_web())
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    threading.Thread(target=web.serve_forever, daemon=True).start()
    wait_until(lambda: urllib.request.urlopen(MOCK + "/healthz").status == 200,
               desc="模拟站点启动")
    wait_until(lambda: api("/api/status") is not None, desc="任务页面启动")
    yield
    mock.shutdown()
    web.shutdown()


def test_full_run_with_all_scenarios():
    post("/batch", {"stores": ALL_STORES, "date_from": D0, "date_to": D1})
    post("/control/start")
    wait_all_terminal(D0)
    ts = tasks_by_store(D0)

    # 正常门店：完成，文件内容通过核验
    for sid in ("S001", "S002"):
        assert ts[sid]["status"] == "done", ts[sid]
        f = Path(ts[sid]["file_path"])
        assert f.exists() and f.parent == config.DOWNLOAD_DIR
        verify_csv(f, store_id=sid, store_name=ts[sid]["store_name"],
                   date_from=D0, date_to=D1)

    # S003：生成失败，失败步骤是"等待生成"
    assert ts["S003"]["status"] == "failed"
    assert ts["S003"]["step"] == "wait_ready"
    assert "生成失败" in ts["S003"]["error"]

    # S004/S005：内容核验失败，失败步骤是"校验内容"，且坏文件被隔离
    assert ts["S004"]["status"] == "failed"
    assert ts["S004"]["step"] == "verify_content"
    assert "门店" in ts["S004"]["error"]
    assert ts["S005"]["status"] == "failed"
    assert ts["S005"]["step"] == "verify_content"
    assert "日期" in ts["S005"]["error"]
    quarantined = list(config.QUARANTINE_DIR.glob("*.csv"))
    assert len(quarantined) >= 2

    # 会话过期陷阱：至少一次"下载到登录页"，且重新登录后恢复
    messages = [m for t in ts.values() for m in all_events(t["id"])]
    assert any("下载到登录页" in m for m in messages), messages
    assert any("重新登录" in m for m in messages), messages
    # 陷阱文件没有被标记成成功
    assert ts["S001"]["status"] == "done"  # 重试后真实完成


def test_rerun_reuses_verified_files():
    # 先制造一个"已完成"的状态（依赖上一个测试）
    ts0 = tasks_by_store(D0)
    assert ts0["S001"]["status"] == "done"

    post("/batch", {"stores": ALL_STORES, "date_from": D0, "date_to": D1})
    wait_all_terminal(D0)
    ts = tasks_by_store(D0)
    s001_events = all_events(ts["S001"]["id"])
    assert any("复用已核验文件" in m for m in s001_events)
    # 失败任务被重新尝试，并保留了上次失败原因
    s003_events = all_events(ts["S003"]["id"])
    assert any("重新尝试失败任务" in m and "上次失败" in m for m in s003_events)
    assert ts["S003"]["status"] == "failed"  # 确定性失败，再次失败


def test_rerun_redownloads_corrupt_file():
    ts = tasks_by_store(D0)
    f = Path(ts["S001"]["file_path"])
    f.write_bytes(b"garbage,not,csv\n1,2,3\n")  # 弄坏已完成的文件

    post("/batch", {"stores": ALL_STORES, "date_from": D0, "date_to": D1})
    wait_all_terminal(D0)
    ts = tasks_by_store(D0)
    assert ts["S001"]["status"] == "done"
    # 重新下载后的文件内容有效
    verify_csv(ts["S001"]["file_path"], store_id="S001",
               store_name=ts["S001"]["store_name"],
               date_from=D0, date_to=D1)
    # 损坏原因保留在处理记录里
    assert any("已有文件未通过校验" in m for m in all_events(ts["S001"]["id"]))


def test_pause_and_resume():
    post("/control/pause")
    d0 = "2026-09-01"
    post("/batch", {"stores": ["S001", "S002", "S003"],
                    "date_from": d0, "date_to": "2026-09-07"})
    post("/control/start")

    # 等第一个任务在站点上创建出导出任务后立刻暂停
    wait_until(lambda: any(t["export_id"] for t in tasks_by_store(d0).values()),
               desc="第一个导出任务已创建")
    post("/control/pause")
    wait_until(lambda: api("/api/status")["worker"]["mode"] == "paused",
               desc="worker 进入暂停")

    # 暂停期间任务状态应冻结
    snap = {t["id"]: (t["status"], len(all_events(t["id"])))
            for t in tasks_by_store(d0).values()}
    time.sleep(3)
    snap2 = {t["id"]: (t["status"], len(all_events(t["id"])))
             for t in tasks_by_store(d0).values()}
    assert snap == snap2, "暂停期间仍有任务在推进"

    # 继续：应接着处理，不重复创建仍在排队的导出
    post("/control/start")
    wait_all_terminal(d0)
    ts = tasks_by_store(d0)
    assert ts["S001"]["status"] == "done"
    assert ts["S002"]["status"] == "done"
    assert ts["S003"]["status"] == "failed"

    reuse_found = False
    for t in ts.values():
        events = all_events(t["id"])
        created = [m for m in events if m.startswith("已创建导出任务")]
        assert len(created) <= 1, f"任务 #{t['id']} 重复创建了导出：{created}"
        if any("复用页面已有导出任务" in m for m in events):
            reuse_found = True
    assert reuse_found, "暂停后继续没有复用已在排队的导出任务"

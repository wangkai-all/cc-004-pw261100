#!/usr/bin/env python3
"""一键启动：模拟报表站点（5001）+ 下载工具任务页面（5000）。

用法：.venv/bin/python run_all.py
然后打开 http://127.0.0.1:5000 ，选择门店和日期范围，创建批次并点"开始"。
"""
import threading

from werkzeug.serving import make_server

from app import config
from app.web import create_app
from mock_site.app import create_app as create_mock

SCENARIOS = """\
模拟站点内置场景（按门店）：
  S001 北京朝阳店 / S002 上海徐汇店：正常导出
  S003 广州天河店：导出生成失败
  S004 深圳南山店：CSV 内容门店不匹配（内容为 S999）
  S005 杭州西湖店：CSV 内容日期范围不匹配（整体偏移到上月）
  另外：会话 45 秒过期（MOCK_SESSION_TTL 可调），过期后下载会拿到
       "扩展名为 .csv 的登录页"，工具会报错、重新登录并重试。
"""


def main():
    config.ensure_browser_libs()

    mock = make_server("127.0.0.1", 5001, create_mock())
    threading.Thread(target=mock.serve_forever, daemon=True).start()

    print(f"模拟报表站点：{config.REPORT_BASE_URL}  （账号 admin / admin123）")
    print(f"任务页面：    http://127.0.0.1:{config.WEB_PORT}")
    print()
    print(SCENARIOS)

    web = make_server("127.0.0.1", config.WEB_PORT, create_app())
    try:
        web.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

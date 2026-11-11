"""下载工具的配置。路径类配置可用环境变量覆盖（测试用）。"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

DATA_DIR = Path(os.environ.get("RD_DATA_DIR", BASE_DIR / "data"))
DOWNLOAD_DIR = Path(os.environ.get("RD_DOWNLOAD_DIR", BASE_DIR / "downloads"))
QUARANTINE_DIR = DOWNLOAD_DIR / "quarantine"
DB_PATH = Path(os.environ.get("RD_DB_PATH", DATA_DIR / "tasks.db"))

# 报表站点
REPORT_BASE_URL = os.environ.get("REPORT_BASE_URL", "http://127.0.0.1:5001")
REPORT_USER = os.environ.get("REPORT_USER", "admin")
REPORT_PASS = os.environ.get("REPORT_PASS", "admin123")

# 任务页面端口（仅 run_all / __main__ 使用）
WEB_PORT = int(os.environ.get("RD_WEB_PORT", "5000"))

# 轮询导出状态的间隔秒数
POLL_INTERVAL = float(os.environ.get("RD_POLL_INTERVAL", "2"))

# 下载到登录页（会话过期）时的最大重试次数
MAX_DOWNLOAD_ATTEMPTS = int(os.environ.get("RD_MAX_DOWNLOAD_ATTEMPTS", "3"))

# 本机无 root 时为浏览器准备的系统库目录（scripts/fetch_syslibs.py 生成）
_SYSLIBS = BASE_DIR / ".syslibs" / "root"

# 浏览器装在项目目录内（~/.cache 可能被环境清理）
_BROWSERS = BASE_DIR / ".ms-playwright"


def ensure_browser_libs():
    """设置浏览器运行环境：本地系统库 + 项目内浏览器路径 + 字体配置。

    对随后启动的浏览器子进程生效（需在 launch 前调用）。
    """
    if _BROWSERS.is_dir():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(_BROWSERS))
    if not _SYSLIBS.is_dir():
        return
    # fontconfig：没有可用字体时渲染进程会崩溃（fill/inner_text 等失效）
    fonts_conf = _SYSLIBS.parent / "fonts.conf"
    fonts_dir = _SYSLIBS / "usr/share/fonts"
    if fonts_dir.is_dir() and not fonts_conf.exists():
        cache = _SYSLIBS.parent / "fontcache"
        cache.mkdir(exist_ok=True)
        fonts_conf.write_text(
            '<?xml version="1.0"?>\n'
            '<!DOCTYPE fontconfig SYSTEM "fonts.dtd">\n'
            "<fontconfig>\n"
            f"  <dir>{fonts_dir}</dir>\n"
            f"  <cachedir>{cache}</cachedir>\n"
            "</fontconfig>\n",
            encoding="utf-8")
    if fonts_conf.exists():
        os.environ.setdefault("FONTCONFIG_FILE", str(fonts_conf))
    dirs = [str(_SYSLIBS / "usr/lib/x86_64-linux-gnu"),
            str(_SYSLIBS / "lib/x86_64-linux-gnu")]
    cur = os.environ.get("LD_LIBRARY_PATH", "")
    parts = [p for p in cur.split(":") if p]
    for d in reversed(dirs):
        if d not in parts:
            parts.insert(0, d)
    os.environ["LD_LIBRARY_PATH"] = ":".join(parts)

# 经营报表下载工具

配置门店和日期范围后，用真实浏览器（Playwright）进入报表页面，逐个发起导出、
等待生成、下载到本地，并对每一次下载做内容核验。任务页面实时展示哪些已完成、
哪些失败、失败发生在哪一步。

## 功能

- **真实页面操作**：登录、发起导出、轮询队列状态、点击下载，全部通过
  Playwright 在报表页面上完成，不直接读模拟站点的数据。
- **双重核对**：
  - 下载前核对**页面上**该导出任务显示的门店和日期范围；
  - 下载后核对 **CSV 内容**里的门店、日期范围（逐日一致）和必需字段
    （`date, store_id, store_name, sales_amount, order_count`），不看文件名。
- **会话过期处理**：会话过期后下载会拿到一个"扩展名为 .csv 的登录页"。
  工具识别 HTML 内容 → 报错 → 重新登录 → 重试下载，**不会**把它标记为成功。
- **断点续跑**：任务状态持久化在 SQLite。暂停后继续，从上次中断的地方接着来；
  站点上仍在排队/生成中的导出任务直接复用，不重复创建。
- **重复执行复用**：同一批（门店 + 日期范围）重复执行时，已完成且文件仍通过
  内容校验的直接复用；文件损坏或内容对应其他门店/日期的，记录原因后重新下载。
- **失败定位**：每个任务记录失败步骤（创建导出 / 等待生成 / 核对页面 /
  下载 / 校验内容）和原因；完整历史（含历次失败原因）在任务详情页可查。
  内容不符的坏文件移入 `downloads/quarantine/` 隔离。

## 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium

# 无 root 权限的环境（浏览器缺系统库时）：
python3 scripts/fetch_syslibs.py .syslibs

.venv/bin/python run_all.py
```

然后打开任务页面 <http://127.0.0.1:5000>：

1. 选择门店和日期范围，点「创建下载任务」；
2. 点「开始 / 继续」，worker 开始逐个处理；
3. 列表每 2 秒自动刷新，点「详情」看每个任务的完整处理记录。

模拟报表站点在 <http://127.0.0.1:5001>（账号 `admin` / `admin123`），
也可以直接用浏览器登录观察导出队列。

## 模拟站点内置场景

| 门店 | 场景 |
|---|---|
| S001 北京朝阳店 / S002 上海徐汇店 | 正常：排队 → 生成 → 可下载 |
| S003 广州天河店 | 导出生成失败（任务失败于「等待生成」） |
| S004 深圳南山店 | CSV 内容门店是 S999（失败于「校验内容」） |
| S005 杭州西湖店 | CSV 内容日期整体偏移到上月（失败于「校验内容」） |
| 任意 | 会话 45 秒过期；每个导出的首次下载返回"登录页.csv"陷阱 |

环境变量可调：`MOCK_SESSION_TTL`（会话秒数）、`MOCK_QUEUE_SECS`（排队秒数）、
`MOCK_PROC_SECS`（生成秒数）、`MOCK_TRAP_DOWNLOADS`（下载陷阱开关，默认 1）。

## 结构

```
mock_site/app.py     模拟报表网站（登录/会话过期/导出队列/CSV 下载/各类陷阱）
app/config.py        配置（路径、站点地址、浏览器运行环境）
app/db.py            SQLite：任务表 + 事件表（批次创建/复用/重下逻辑）
app/verify.py        CSV 内容核验（登录页识别、门店/日期/必需字段）
app/worker.py        Playwright 下载 worker（独立线程，状态机可暂停续跑）
app/web.py           任务页面（Flask）：批次创建、启停控制、状态/详情页
run_all.py           一键启动模拟站点 + 任务页面
tests/               单元测试（核验逻辑）+ 端到端测试（真实浏览器全流程）
scripts/fetch_syslibs.py  无 root 环境下载浏览器系统依赖到本地
```

## 任务状态机

```
pending → waiting（确保导出存在：优先复用页面已有导出，否则创建；
                   轮询直到 可下载 / 生成失败）
        → downloading → verifying → done
任意一步失败 → failed（记录 step + error，历史保留在事件表）
```

- 下载到登录页（`session_expired`）→ 重新登录后重试，最多
  `RD_MAX_DOWNLOAD_ATTEMPTS` 次（默认 3）；
- worker 重启时，下载/校验中途的任务回退为 waiting 重新下载；
- 重复创建同一批任务时：done 且文件通过校验 → 复用；done 但文件校验失败
  → 记录原因并重新下载；failed → 重新尝试（上次失败原因保留在事件表）。

## 测试

```bash
.venv/bin/python -m pytest tests/ -q
```

端到端测试（`tests/test_e2e.py`）真实启动模拟站点、任务页面和浏览器，
覆盖：正常下载、生成失败、内容门店/日期不匹配、会话过期陷阱与重新登录、
复用已核验文件、文件损坏后重下、暂停/继续不重复创建导出。

## 环境变量（下载工具）

| 变量 | 默认 | 说明 |
|---|---|---|
| `REPORT_BASE_URL` | `http://127.0.0.1:5001` | 报表站点地址 |
| `REPORT_USER` / `REPORT_PASS` | `admin` / `admin123` | 登录账号 |
| `RD_WEB_PORT` | `5000` | 任务页面端口 |
| `RD_POLL_INTERVAL` | `2` | 轮询导出状态的间隔秒数 |
| `RD_MAX_DOWNLOAD_ATTEMPTS` | `3` | 下载到登录页时的最大重试次数 |
| `RD_DATA_DIR` / `RD_DOWNLOAD_DIR` / `RD_DB_PATH` | `data/` `downloads/` | 存储位置 |

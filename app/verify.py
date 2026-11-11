"""下载文件的核验：不看文件名，只看内容。

校验项：
  * 文件存在、非空、是 UTF-8 文本；
  * 不是 HTML（会话过期时站点会返回"扩展名为 .csv 的登录页"）；
  * 包含必需字段；
  * 所有数据行的门店与任务一致；
  * 日期集合与任务的日期范围逐日一致（不多不少）；
  * 数值字段可解析。
"""
import csv
import hashlib
import io
from datetime import date, timedelta
from pathlib import Path

REQUIRED_COLUMNS = ["date", "store_id", "store_name", "sales_amount", "order_count"]


class VerifyError(Exception):
    """code: missing / empty / corrupt / session_expired / missing_fields /
             store_mismatch / date_mismatch"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def expected_dates(date_from: str, date_to: str) -> set:
    d0 = date.fromisoformat(date_from)
    d1 = date.fromisoformat(date_to)
    days = set()
    while d0 <= d1:
        days.add(d0.isoformat())
        d0 += timedelta(days=1)
    return days


def looks_like_html(head: bytes) -> bool:
    h = head.lower()
    return (b"<!doctype" in h or b"<html" in h or b"<head" in h
            or (b"<form" in h and b"password" in h))


def verify_csv(path, *, store_id: str, store_name: str,
               date_from: str, date_to: str) -> str:
    """校验通过返回文件 sha256；否则抛 VerifyError。"""
    p = Path(path)
    if not p.exists():
        raise VerifyError("missing", "文件不存在")
    data = p.read_bytes()
    if not data:
        raise VerifyError("empty", "文件为空")

    if looks_like_html(data[:4096]):
        raise VerifyError(
            "session_expired",
            "下载到的不是 CSV，而是 HTML 登录页（会话已过期）")

    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise VerifyError("corrupt", "文件不是有效的 UTF-8 文本（可能已损坏）")

    try:
        reader = csv.DictReader(io.StringIO(text))
        fields = reader.fieldnames or []
        rows = list(reader)
    except csv.Error as e:
        raise VerifyError("corrupt", f"CSV 解析失败：{e}")

    missing_cols = [c for c in REQUIRED_COLUMNS if c not in fields]
    if missing_cols:
        raise VerifyError("missing_fields",
                          f"缺少必需字段：{', '.join(missing_cols)}")
    if not rows:
        raise VerifyError("empty", "CSV 没有任何数据行")

    store_ids = {(r.get("store_id") or "").strip() for r in rows}
    if store_ids != {store_id}:
        raise VerifyError(
            "store_mismatch",
            f"内容门店为 {sorted(store_ids)}，应为 {store_id}")
    store_names = {(r.get("store_name") or "").strip() for r in rows}
    if store_names != {store_name}:
        raise VerifyError(
            "store_mismatch",
            f"内容门店名称为 {sorted(store_names)}，应为「{store_name}」")

    actual_dates = set()
    for i, r in enumerate(rows, start=2):  # 第 1 行是表头
        ds = (r.get("date") or "").strip()
        try:
            actual_dates.add(date.fromisoformat(ds).isoformat())
        except ValueError:
            raise VerifyError("corrupt", f"第 {i} 行日期格式错误：{ds!r}")
        for col in ("sales_amount", "order_count"):
            try:
                float((r.get(col) or "").strip())
            except ValueError:
                raise VerifyError("corrupt",
                                  f"第 {i} 行字段 {col} 不是数值：{r.get(col)!r}")

    expected = expected_dates(date_from, date_to)
    if actual_dates != expected:
        lack = sorted(expected - actual_dates)
        extra = sorted(actual_dates - expected)
        parts = []
        if lack:
            parts.append(f"缺少 {len(lack)} 天（如 {lack[0]}）")
        if extra:
            parts.append(f"多出 {len(extra)} 天（如 {extra[0]}）")
        got = f"{min(actual_dates)} ~ {max(actual_dates)}" if actual_dates else "无"
        raise VerifyError(
            "date_mismatch",
            f"内容日期范围不符（{'；'.join(parts)}；实际覆盖 {got}，"
            f"应为 {date_from} ~ {date_to}）")

    return hashlib.sha256(data).hexdigest()

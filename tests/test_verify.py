"""verify_csv 的单元测试。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.verify import VerifyError, verify_csv  # noqa: E402

KW = dict(store_id="S001", store_name="北京朝阳店",
          date_from="2026-10-01", date_to="2026-10-03")

GOOD = (
    "date,store_id,store_name,sales_amount,order_count\n"
    "2026-10-01,S001,北京朝阳店,1234.5,67\n"
    "2026-10-02,S001,北京朝阳店,2345.6,78\n"
    "2026-10-03,S001,北京朝阳店,3456.7,89\n"
)

LOGIN_HTML = (
    "<!doctype html><html><head><title>登录</title></head>"
    "<body><form><input type=password></form></body></html>"
)


def write(tmp_path, content, name="f.csv"):
    p = tmp_path / name
    if isinstance(content, bytes):
        p.write_bytes(content)
    else:
        p.write_text(content, encoding="utf-8")
    return p


def test_good_csv_passes(tmp_path):
    sha = verify_csv(write(tmp_path, GOOD), **KW)
    assert isinstance(sha, str) and len(sha) == 64


def test_good_csv_with_bom_passes(tmp_path):
    sha = verify_csv(write(tmp_path, GOOD.encode("utf-8-sig")), **KW)
    assert len(sha) == 64


def test_missing_file(tmp_path):
    with pytest.raises(VerifyError) as e:
        verify_csv(tmp_path / "nope.csv", **KW)
    assert e.value.code == "missing"


def test_empty_file(tmp_path):
    with pytest.raises(VerifyError) as e:
        verify_csv(write(tmp_path, ""), **KW)
    assert e.value.code == "empty"


def test_login_page_detected_as_session_expired(tmp_path):
    # 会话过期时下载到的"扩展名为 .csv 的登录页"
    with pytest.raises(VerifyError) as e:
        verify_csv(write(tmp_path, LOGIN_HTML, name="report_S001.csv"), **KW)
    assert e.value.code == "session_expired"


def test_non_utf8_is_corrupt(tmp_path):
    with pytest.raises(VerifyError) as e:
        verify_csv(write(tmp_path, b"\xff\xfe\x00\x01binary"), **KW)
    assert e.value.code == "corrupt"


def test_missing_required_column(tmp_path):
    bad = GOOD.replace("sales_amount,", "")
    with pytest.raises(VerifyError) as e:
        verify_csv(write(tmp_path, bad), **KW)
    assert e.value.code == "missing_fields"
    assert "sales_amount" in e.value.message


def test_no_data_rows(tmp_path):
    with pytest.raises(VerifyError) as e:
        verify_csv(write(tmp_path, GOOD.splitlines()[0] + "\n"), **KW)
    assert e.value.code == "empty"


def test_store_mismatch(tmp_path):
    bad = GOOD.replace("S001", "S999")
    with pytest.raises(VerifyError) as e:
        verify_csv(write(tmp_path, bad), **KW)
    assert e.value.code == "store_mismatch"


def test_store_name_mismatch(tmp_path):
    bad = GOOD.replace("北京朝阳店", "未知门店")
    with pytest.raises(VerifyError) as e:
        verify_csv(write(tmp_path, bad), **KW)
    assert e.value.code == "store_mismatch"


def test_date_missing_day(tmp_path):
    bad = "\n".join(GOOD.splitlines()[:-1]) + "\n"  # 去掉最后一天
    with pytest.raises(VerifyError) as e:
        verify_csv(write(tmp_path, bad), **KW)
    assert e.value.code == "date_mismatch"
    assert "缺少" in e.value.message


def test_date_wrong_month(tmp_path):
    bad = GOOD.replace("2026-10", "2026-09")
    with pytest.raises(VerifyError) as e:
        verify_csv(write(tmp_path, bad), **KW)
    assert e.value.code == "date_mismatch"
    assert "多出" in e.value.message


def test_bad_number_is_corrupt(tmp_path):
    bad = GOOD.replace("1234.5", "not-a-number")
    with pytest.raises(VerifyError) as e:
        verify_csv(write(tmp_path, bad), **KW)
    assert e.value.code == "corrupt"

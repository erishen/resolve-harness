"""Tests for scripts/fetch_shfe_futures.py: parsing + 降级 + markdown 渲染。

网络部分用 monkeypatch 替身，保证离线可跑。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# scripts/ 不是包，手动加入 path 后导入
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fetch_shfe_futures as m  # noqa: E402


SHFE_FIXTURE = {
    "o_curinstrument": [
        {"PRODUCTCLASS": "1", "PRODUCTGROUPID": "cu", "DELIVERYMONTH": "2609",
         "PRODUCTNAME": "铜", "CLOSEPRICE": 107750, "ZD1_CHG": 570, "PRESETTLEMENTPRICE": 107180},
        {"PRODUCTCLASS": "1", "PRODUCTGROUPID": "au", "DELIVERYMONTH": "2610",
         "PRODUCTNAME": "黄金", "CLOSEPRICE": 560, "ZD1_CHG": 11, "PRESETTLEMENTPRICE": 550},
        {"PRODUCTCLASS": "2", "PRODUCTGROUPID": "cu", "DELIVERYMONTH": "2609",  # 期权，应被过滤
         "PRODUCTNAME": "铜期权", "CLOSEPRICE": 0, "ZD1_CHG": 0, "PRESETTLEMENTPRICE": 0},
        {"PRODUCTCLASS": "1", "PRODUCTGROUPID": "rb", "DELIVERYMONTH": "2610",
         "PRODUCTNAME": "螺纹钢", "CLOSEPRICE": 3200, "ZD1_CHG": -32, "PRESETTLEMENTPRICE": 3232},
    ]
}


@pytest.fixture()
def patch_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(m, "_http_get", lambda url, **kw: __import__("json").dumps(SHFE_FIXTURE))


def test_fetch_shfe_normalizes_and_filters_options(patch_http: None) -> None:
    rows = m.fetch_shfe("20260821")
    # 期权(PRODUCTCLASS=2)被过滤，仅剩 3 条期货
    assert len(rows) == 3
    codes = {r["code"] for r in rows}
    assert "cu2609" in codes and "au2610" in codes and "rb2610" in codes
    au = next(r for r in rows if r["code"] == "au2610")
    # 涨跌幅 = 11 / 550 * 100
    assert au["change_pct"] == pytest.approx(2.0, abs=1e-6)
    assert au["name"] == "黄金2610"


def test_top_sorts_by_change_desc(patch_http: None) -> None:
    rows = m.fetch_shfe("20260821")
    top = m._top(rows, 2)
    assert [r["code"] for r in top] == ["au2610", "cu2609"]  # 2.0% > 0.53% > -0.99%


def test_markdown_shape() -> None:
    md = m.to_markdown(
        [{"code": "au2610", "name": "黄金2610", "price": 560, "change_pct": 2.0}],
        source="shfe",
        trade_date="20260821",
    )
    assert md.startswith("# 上期所期货行情")
    assert "| 黄金2610 | 560 | +2.00% |" in md
    assert "交易日：20260821" in md


def test_fallback_eastmoney_to_shfe(monkeypatch: pytest.MonkeyPatch) -> None:
    """EastMoney 抛 502 -> 降级到 SHFE 仍可取数。"""
    import urllib.error as _ue

    def fake_get(url: str, **kw):
        if "eastmoney" in url:
            raise _ue.HTTPError(url, 502, "Bad Gateway", None, None)
        return __import__("json").dumps(SHFE_FIXTURE)

    monkeypatch.setattr(m, "_http_get", fake_get)

    # 复刻 main() 的降级决策
    source = None
    rows: list[dict] = []
    try:
        rows = m.fetch_eastmoney()
        source = "eastmoney"
    except Exception:
        pass
    if source is None:
        rows = m.fetch_shfe("20260821")
        source = "shfe"

    assert source == "shfe"
    assert len(rows) == 3
    assert any(r["code"] == "au2610" for r in rows)

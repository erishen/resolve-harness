"""抓取上期所期货行情，解析前 N 大涨幅合约，写入沙箱 futures.md。

数据源策略（自动降级）：
  1. 东方财富实时接口 push2.eastmoney.com/api/qt/clist/get（fs=m:113+t:2，上期所）
     —— 字段全、含实时涨跌幅，首选。
  2. 若东方财富不可达（本环境会被网络拦截，返回 502），降级到
     上期所官网权威每日行情 /data/tradedata/future/dailydata/kx{YYYYMMDD}.dat
     —— 口径：最新价=收盘价，涨跌幅=涨跌1÷前结算价。

用法：
  python scripts/fetch_shfe_futures.py                 # 默认：仅上期所官网公开数据，取前 5，写 data/sandbox/futures.md
  python scripts/fetch_shfe_futures.py --top 10 --out /tmp/f.md
  python scripts/fetch_shfe_futures.py --date 20260821
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import sys
import urllib.error
import urllib.request
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("fetch_shfe_futures")

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
EM_URL = (
    "https://push2.eastmoney.com/api/qt/clist/get"
    "?pn=1&pz=50&po=1&np=1"
    "&ut=bd1d9ddb04089700cf9c27f6f7426281&fltt=2&invt=2&fid=f3"
    "&fs=m:113+t:2"
    "&fields=f1,f2,f3,f4,f12,f14"
)
SHFE_DAT = "https://www.shfe.com.cn/data/tradedata/future/dailydata/kx{date}.dat?params={ts}"


def _http_get(url: str, *, referer: str | None = None, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    if referer:
        req.add_header("Referer", referer)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _norm(code: str, name: str, price, change_pct) -> dict:
    return {"code": code, "name": name, "price": price, "change_pct": float(change_pct)}


def fetch_eastmoney() -> list[dict]:
    """东方财富实时快照 → 归一化行；失败时抛异常。"""
    body = _http_get(EM_URL, referer="https://quote.eastmoney.com/")
    data = json.loads(body)
    diff = (data.get("data") or {}).get("diff") or []
    if not diff:
        raise ValueError("eastmoney: empty diff")
    rows = []
    for it in diff:
        rows.append(_norm(it["f12"], it["f14"], it["f2"], it["f3"]))
    return rows


def fetch_shfe(date: str) -> list[dict]:
    """上期所官网每日行情 → 归一化行；失败抛异常。"""
    url = SHFE_DAT.format(date=date, ts=_dt.datetime.now().strftime("%s%f"))
    body = _http_get(url, referer="https://www.shfe.com.cn/reports/tradedata/dailyandweeklydata/?query_params=kx")
    data = json.loads(body)
    insts = data.get("o_curinstrument") or []
    if not insts:
        raise ValueError("shfe: empty o_curinstrument")
    rows = []
    for it in insts:
        # PRODUCTCLASS "1" 为期货（"2" 为期权的并行合约，跳过）
        if it.get("PRODUCTCLASS") != "1":
            continue
        pre = it.get("PRESETTLEMENTPRICE") or 0
        if not pre:
            continue
        pct = (it.get("ZD1_CHG") or 0) / pre * 100
        code = f"{it['PRODUCTGROUPID']}{it['DELIVERYMONTH']}"
        name = f"{it['PRODUCTNAME']}{it['DELIVERYMONTH']}"
        rows.append(_norm(code, name, it.get("CLOSEPRICE"), pct))
    return rows


def _top(rows: list[dict], n: int) -> list[dict]:
    return sorted(rows, key=lambda r: r["change_pct"], reverse=True)[:n]


def to_markdown(rows: list[dict], source: str, trade_date: str) -> str:
    lines = ["# 上期所期货行情（前 %d 大涨幅合约）" % len(rows), ""]
    if source == "eastmoney":
        lines.append("> 数据来源：东方财富实时行情（push2.eastmoney.com，fs=m:113+t:2）")
    else:
        lines.append("> 数据来源：上海期货交易所官网每日行情（/data/tradedata/future/dailydata/kxYYYYMMDD.dat）")
        lines.append("> 口径：「最新价」取当日收盘价；「涨跌幅」= 涨跌1 ÷ 前结算价。")
    lines.append(f"> 交易日：{trade_date}")
    lines.append("")
    lines.append("| 排名 | 合约代码 | 合约名称 | 最新价 | 涨跌幅 |")
    lines.append("|------|----------|----------|--------|--------|")
    for i, r in enumerate(rows, 1):
        sign = "+" if r["change_pct"] >= 0 else ""
        lines.append(f"| {i} | {r['code']} | {r['name']} | {r['price']} | {sign}{r['change_pct']:.2f}% |")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="抓取上期所期货行情并解析前 N 大涨幅合约")
    ap.add_argument("--top", type=int, default=5, help="取前 N 大涨幅合约（默认 5）")
    ap.add_argument("--out", type=str, default=None, help="输出 md 路径（默认 <repo>/data/sandbox/futures.md）")
    ap.add_argument("--date", type=str, default=_dt.datetime.now().strftime("%Y%m%d"), help="交易日 YYYYMMDD（仅官网源使用）")
    ap.add_argument("--source", choices=["auto", "eastmoney", "shfe"], default="auto",
                    help="auto=仅上期所官网公开数据；eastmoney=第三方实时接口（显式指定，仅供研究）")
    args = ap.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    out_path = Path(args.out) if args.out else repo_root / "data" / "sandbox" / "futures.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    source = None
    rows: list[dict] = []
    # 合规口径：auto 仅使用上期所官网主动公开披露的每日行情；
    # 东方财富为第三方实时接口，仅在显式 --source eastmoney 时可用。
    if args.source == "eastmoney":
        # 第三方实时接口：仅在显式指定时使用，失败不降级（避免静默换源）
        log.warning("--source eastmoney 为第三方实时接口，仅供本地研究，勿用于生产/分发")
        try:
            rows = fetch_eastmoney()
            source = "eastmoney"
        except Exception as exc:  # noqa: BLE001
            log.error("东方财富接口失败：%s", exc)
            return 1
    else:  # auto / shfe：仅上期所官网公开披露数据
        try:
            rows = fetch_shfe(args.date)
            source = "shfe"
            log.info("上期所官网接口成功，拿到 %d 条合约", len(rows))
        except Exception as exc:  # noqa: BLE001
            log.error("上期所官网接口失败：%s", exc)
            return 1

    top = _top(rows, args.top)
    md = to_markdown(top, source, args.date if source == "shfe" else "实时")
    out_path.write_text(md, encoding="utf-8")
    log.info("已写入 %s（来源=%s，前 %d 合约）", out_path, source, len(top))
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())

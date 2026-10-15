"""
免费公开行情数据（无需 API Key）。支持 USDT 本位永续合约：
  binance / bybit / okx / gate
exchange = "auto" 时按顺序尝试，哪个能连上用哪个。
"""
from __future__ import annotations

import time
from typing import Optional

import pandas as pd
import requests

UA = {"User-Agent": "vcp-scanner/1.0"}

# 各交易所对周期的写法
INTERVALS = {
    "binance": {"15m": "15m", "30m": "30m", "1h": "1h", "4h": "4h", "1d": "1d", "1w": "1w"},
    "bybit": {"15m": "15", "30m": "30", "1h": "60", "4h": "240", "1d": "D", "1w": "W"},
    "okx": {"15m": "15m", "30m": "30m", "1h": "1H", "4h": "4H", "1d": "1Dutc", "1w": "1Wutc"},
    "gate": {"15m": "15m", "30m": "30m", "1h": "1h", "4h": "4h", "1d": "1d", "1w": "7d"},
}
INTERVAL_MS = {"15m": 900_000, "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000,
               "1d": 86_400_000, "1w": 604_800_000}


class ExchangeError(Exception):
    pass


def _get(url: str, params: Optional[dict] = None, retries: int = 3, timeout: int = 15):
    last = None
    for i in range(retries):
        try:
            resp = requests.get(url, params=params, headers=UA, timeout=timeout)
            if resp.status_code == 429:
                time.sleep(2 * (i + 1))
                continue
            if resp.status_code in (403, 451):
                raise ExchangeError(f"{url} 拒绝访问(HTTP {resp.status_code})，可能所在地区被限制")
            resp.raise_for_status()
            return resp.json()
        except ExchangeError:
            raise
        except Exception as e:  # 网络抖动重试
            last = e
            time.sleep(1.5 * (i + 1))
    raise ExchangeError(f"请求失败 {url}: {last}")


def _frame(rows, cols=("time", "open", "high", "low", "close", "volume")) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=list(cols))
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    df["time"] = pd.to_datetime(df["time"].astype("int64"), unit="ms", utc=True)
    return df.sort_values("time").drop_duplicates("time").reset_index(drop=True)


# ----------------------------------------------------------------------------
class Binance:
    name = "binance"
    base = "https://fapi.binance.com"

    def symbols(self) -> dict[str, float]:
        info = _get(self.base + "/fapi/v1/exchangeInfo")
        ok = {s["symbol"] for s in info["symbols"]
              if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT"
              and s.get("status") == "TRADING"}
        tick = _get(self.base + "/fapi/v1/ticker/24hr")
        return {t["symbol"]: float(t["quoteVolume"]) for t in tick if t["symbol"] in ok}

    def klines(self, symbol: str, interval: str, limit: int) -> pd.DataFrame:
        rows = _get(self.base + "/fapi/v1/klines",
                    {"symbol": symbol, "interval": INTERVALS["binance"][interval], "limit": min(limit, 1500)})
        # 用成交额（USDT）作为成交量，跨币种可比
        return _frame([(r[0], r[1], r[2], r[3], r[4], r[7]) for r in rows])


class Bybit:
    name = "bybit"
    base = "https://api.bybit.com"

    def symbols(self) -> dict[str, float]:
        ok, cursor = set(), ""
        while True:
            d = _get(self.base + "/v5/market/instruments-info",
                     {"category": "linear", "limit": 1000, "cursor": cursor})["result"]
            for s in d["list"]:
                if s.get("contractType") == "LinearPerpetual" and s.get("quoteCoin") == "USDT" \
                        and s.get("status") == "Trading":
                    ok.add(s["symbol"])
            cursor = d.get("nextPageCursor") or ""
            if not cursor:
                break
        tick = _get(self.base + "/v5/market/tickers", {"category": "linear"})["result"]["list"]
        return {t["symbol"]: float(t["turnover24h"]) for t in tick if t["symbol"] in ok}

    def klines(self, symbol: str, interval: str, limit: int) -> pd.DataFrame:
        rows = _get(self.base + "/v5/market/kline",
                    {"category": "linear", "symbol": symbol,
                     "interval": INTERVALS["bybit"][interval], "limit": min(limit, 1000)})["result"]["list"]
        return _frame([(r[0], r[1], r[2], r[3], r[4], r[6]) for r in rows])


class OKX:
    name = "okx"
    base = "https://www.okx.com"

    def symbols(self) -> dict[str, float]:
        inst = _get(self.base + "/api/v5/public/instruments", {"instType": "SWAP"})["data"]
        ok = {s["instId"] for s in inst if s.get("settleCcy") == "USDT" and s.get("state") == "live"}
        tick = _get(self.base + "/api/v5/market/tickers", {"instType": "SWAP"})["data"]
        # 永续 volCcy24h 单位是币，乘以最新价≈USDT 成交额
        return {t["instId"]: float(t["volCcy24h"] or 0) * float(t["last"] or 0) for t in tick if t["instId"] in ok}

    def klines(self, symbol: str, interval: str, limit: int) -> pd.DataFrame:
        bar = INTERVALS["okx"][interval]
        rows = _get(self.base + "/api/v5/market/candles", {"instId": symbol, "bar": bar, "limit": 300})["data"]
        # 不够时用 history-candles 往前翻页
        while len(rows) < limit and rows:
            older = _get(self.base + "/api/v5/market/history-candles",
                         {"instId": symbol, "bar": bar, "after": rows[-1][0], "limit": 100})["data"]
            if not older:
                break
            rows += older
            time.sleep(0.12)
        # r[7] = volCcyQuote（USDT 成交额）
        return _frame([(r[0], r[1], r[2], r[3], r[4], r[7] if len(r) > 7 else r[6]) for r in rows])


class Gate:
    name = "gate"
    base = "https://api.gateio.ws/api/v4"

    def symbols(self) -> dict[str, float]:
        cons = _get(self.base + "/futures/usdt/contracts")
        ok = {c["name"] for c in cons if not c.get("in_delisting")}
        tick = _get(self.base + "/futures/usdt/tickers")
        return {t["contract"]: float(t.get("volume_24h_quote") or 0) for t in tick if t["contract"] in ok}

    def klines(self, symbol: str, interval: str, limit: int) -> pd.DataFrame:
        rows = _get(self.base + "/futures/usdt/candlesticks",
                    {"contract": symbol, "interval": INTERVALS["gate"][interval], "limit": min(limit, 1999)})
        return _frame([(int(r["t"]) * 1000, r["o"], r["h"], r["l"], r["c"], r.get("sum", r["v"])) for r in rows])


EXCHANGES = {"binance": Binance, "bybit": Bybit, "okx": OKX, "gate": Gate}


def get_exchange(name: str):
    """name = binance/bybit/okx/gate/auto。auto 时返回第一个可用的交易所及其币种列表。"""
    order = list(EXCHANGES) if name == "auto" else [name]
    errors = []
    for n in order:
        ex = EXCHANGES[n]()
        try:
            syms = ex.symbols()
            if syms:
                return ex, syms
        except Exception as e:
            errors.append(f"{n}: {e}")
    raise ExchangeError("所有交易所都连接失败:\n  " + "\n  ".join(errors))


def drop_unclosed(df: pd.DataFrame, interval: str) -> pd.DataFrame:
    """去掉还没收盘的最后一根K线，避免信号“重绘”。"""
    if df.empty:
        return df
    now = pd.Timestamp.now(tz="UTC")
    if df["time"].iloc[-1] + pd.Timedelta(milliseconds=INTERVAL_MS[interval]) > now:
        return df.iloc[:-1].reset_index(drop=True)
    return df

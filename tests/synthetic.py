"""生成模拟K线，用于离线测试 / 演示。"""
import numpy as np
import pandas as pd


def _path(points, rng, noise=0.006):
    """points: [(bars, target_price), ...] 线性插值 + 少量噪声"""
    closes = []
    price = points[0][1]
    for bars, target in points[1:]:
        seg = np.linspace(price, target, bars + 1)[1:]
        closes.extend(seg * (1 + rng.normal(0, noise, bars)))
        price = target
    return np.array(closes)


def make_vcp(breakout=False, seed=7, follow=0):
    rng = np.random.default_rng(seed)
    pts = [(0, 10), (200, 16), (70, 30),  # 上升趋势
           (12, 21), (14, 29),            # T1 -30%
           (8, 24.6), (10, 28.6),         # T2 -14%
           (5, 26.7), (6, 28.3),          # T3 -6.6%
           (3, 27.6), (2, 28.1)]          # 收紧
    close = _path(pts, rng)
    n = len(close)
    vol = np.full(n, 1e6) * (1 + rng.uniform(-0.15, 0.15, n))
    base_start = 270
    vol[base_start:] *= np.linspace(1.3, 0.35, n - base_start)  # 成交量萎缩
    if breakout:
        close = np.append(close, 29.4)
        vol = np.append(vol, 3e6)
        n += 1
    if follow:  # 突破后继续上涨 follow 根K线
        up = np.linspace(close[-1], close[-1] * 1.35, follow + 1)[1:]
        close = np.append(close, up)
        vol = np.append(vol, np.full(follow, 1.4e6))
        n += follow
    openp = np.r_[close[0], close[:-1]]
    high = np.maximum(openp, close) * (1 + rng.uniform(0.001, 0.008, n))
    low = np.minimum(openp, close) * (1 - rng.uniform(0.001, 0.008, n))
    t = pd.date_range("2025-01-01", periods=n, freq="D", tz="UTC")
    return pd.DataFrame({"time": t, "open": openp, "high": high, "low": low, "close": close, "volume": vol})


def make_downtrend(seed=3):
    rng = np.random.default_rng(seed)
    close = _path([(0, 30), (150, 12), (60, 15), (60, 9)], rng, 0.01)
    n = len(close)
    openp = np.r_[close[0], close[:-1]]
    return pd.DataFrame({
        "time": pd.date_range("2025-01-01", periods=n, freq="D", tz="UTC"),
        "open": openp, "high": np.maximum(openp, close) * 1.01, "low": np.minimum(openp, close) * 0.99,
        "close": close, "volume": np.full(n, 1e6)})


def make_two_base_4h(seed=11, breakout=False, follow=0):
    """模拟师父截图那种走势：起涨 → B1(13%,3%) → 突破上涨 → B2(16.8%,5.8%) → 接近 pivot。返回 (4h, 日线)。"""
    rng = np.random.default_rng(seed)
    pts = [(0, 0.0065), (90, 0.0105),                       # 起涨（到 B2 高点涨幅 ≈ 126%）
           (12, 0.00915), (14, 0.0102), (6, 0.00990), (6, 0.0104),   # B1: C1≈13%  C2≈3%
           (60, 0.0147),                                    # 突破后上涨到 B2 高点
           (25, 0.01225), (30, 0.01335), (10, 0.01262), (12, 0.01330)]  # B2: C1≈16.8% C2≈5.5%
    close = _path(pts, rng, noise=0.004)
    n = len(close)
    vol = np.full(n, 4e4) * (1 + rng.uniform(-0.2, 0.2, n))
    vol[-40:] *= 0.6                                        # 末段缩量
    if breakout:
        close = np.append(close, 0.01350)
        vol = np.append(vol, 1.2e5)
        n += 1
    if follow:  # 突破后继续上涨
        close = np.append(close, np.linspace(close[-1], close[-1] * 1.45, follow + 1)[1:])
        vol = np.append(vol, np.full(follow, 6e4))
        n += follow
    openp = np.r_[close[0], close[:-1]]
    high = np.maximum(openp, close) * (1 + rng.uniform(0.0005, 0.003, n))
    low = np.minimum(openp, close) * (1 - rng.uniform(0.0005, 0.003, n))
    t = pd.date_range("2026-06-01", periods=n, freq="4h", tz="UTC")
    df = pd.DataFrame({"time": t, "open": openp, "high": high, "low": low, "close": close, "volume": vol})
    d1 = (df.set_index("time").resample("1D")
          .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
          .dropna().reset_index())
    return df, d1

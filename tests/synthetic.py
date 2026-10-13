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


def make_vcp(breakout=False, seed=7):
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

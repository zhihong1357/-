"""
VCP（波动收缩形态 / Volatility Contraction Pattern）客观判定逻辑。

全部规则都是可量化的，参数在 config.yaml 的 `vcp:` 段里调整。
判定流程：
  1. 趋势模板（Minervini Trend Template，第二阶段上升趋势）
  2. 找底部（base）：最近 N 根K线里的最高点作为底部起点
  3. 用 ZigZag 找摆动高低点，再合并成一段段“收缩”（回撤）
  4. 收缩必须：次数 2~6 次、幅度逐次变小、最后一次足够紧
  5. 成交量萎缩（最后一次收缩的量 < 第一次收缩的量，最近几根量 < 均量）
  6. 枢轴点（pivot）= 最后一次收缩的高点；收盘带量突破 pivot = 突破信号
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

import numpy as np
import pandas as pd


# ----------------------------------------------------------------------------
# 数据结构
# ----------------------------------------------------------------------------
@dataclass
class Contraction:
    peak_idx: int          # 收缩起点（高点）在 df 中的位置
    peak: float
    trough_idx: int        # 收缩最低点位置
    trough: float
    depth: float           # 回撤幅度 0~1
    avg_volume: float      # 这段收缩期间的平均成交量


@dataclass
class VCPResult:
    symbol: str
    valid: bool = False                    # 是否完整符合 VCP
    status: str = "NONE"                   # NONE / SETUP / BREAKOUT
    reasons: list = field(default_factory=list)   # 不符合的原因
    checks: dict = field(default_factory=dict)    # 每条规则是否通过
    contractions: list = field(default_factory=list)
    pivot: Optional[float] = None
    pivot_idx: Optional[int] = None
    base_start_idx: Optional[int] = None
    base_bars: int = 0
    last_close: Optional[float] = None
    distance_to_pivot: Optional[float] = None    # (pivot - close)/close
    breakout_volume_ratio: Optional[float] = None
    score: float = 0.0

    def depths_text(self) -> str:
        return " → ".join(f"{c.depth * 100:.1f}%" for c in self.contractions)

    def to_row(self) -> dict:
        d = asdict(self)
        d.pop("contractions")
        d["contractions"] = self.depths_text()
        d["n_contractions"] = len(self.contractions)
        d["reasons"] = "; ".join(self.reasons)
        d.pop("checks")
        return d


DEFAULTS = {
    # 趋势模板
    "require_trend_template": True,
    "ma_fast": 50,
    "ma_mid": 150,
    "ma_slow": 200,
    "ma_slow_rising_bars": 20,      # MA200 至少比 N 根前更高
    "min_above_low_pct": 0.30,      # 价格至少高于 52 周低点 30%
    "max_below_high_pct": 0.25,     # 价格距 52 周高点不超过 25%
    "year_bars": 365,               # 加密货币 24/7，日线一年=365 根
    "min_history_bars": 120,        # 历史太短的币不扫
    # 底部与收缩
    "base_min_bars": 15,
    "base_max_bars": 180,
    "zigzag_pct": 0.04,             # 摆动点最小幅度
    "recovery_ratio": 0.5,          # 反弹收复上一段回撤的比例 >= 此值才算新一段收缩
    "min_contractions": 2,
    "max_contractions": 6,
    "max_first_depth": 0.35,        # 第一段回撤不超过 35%（Minervini 原版）
    "max_last_depth": 0.15,         # 最后一段回撤不超过 15%
    "min_last_depth": 0.0,
    "contraction_tolerance": 0.10,  # 后一段可比前一段大 10%（相对值）的容忍度
    "require_higher_lows": False,   # 是否要求每段低点逐步抬高
    "prior_uptrend_pct": 0.30,      # 底部前需有 >=30% 的上涨
    "prior_uptrend_bars": 120,
    # 成交量
    "require_volume_dryup": True,
    "volume_ma": 50,
    "dryup_recent_bars": 5,
    "dryup_ratio": 1.0,             # 最近 N 根平均量 < 均量 * 此值
    # 信号
    "pivot_proximity_pct": 0.06,    # 现价在 pivot 下方 6% 以内 => SETUP（准备突破）
    "breakout_volume_ratio": 1.5,   # 突破日成交量 >= 均量 1.5 倍
    "breakout_max_extension": 0.05, # 收盘超过 pivot 5% 以上视为追高，不再提示
}


# ----------------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------------
def zigzag(high: np.ndarray, low: np.ndarray, pct: float):
    """
    返回交替的摆动点列表 [(idx, price, 'H'|'L'), ...]，从第一个点开始。
    当价格从上一个极值反向移动超过 pct 时才确认该极值。
    最后一个未确认的极值也会附加在末尾（标记为暂定）。
    """
    n = len(high)
    if n == 0:
        return []
    pivots = []
    # 起点视为高点（底部从最高点开始）
    trend = "down"
    ext_idx, ext_price = 0, high[0]
    pivots.append((0, high[0], "H"))
    cand_idx, cand_price = 0, low[0]
    for i in range(1, n):
        if trend == "down":
            if low[i] < cand_price:
                cand_idx, cand_price = i, low[i]
            elif high[i] >= cand_price * (1 + pct):
                pivots.append((cand_idx, cand_price, "L"))
                trend = "up"
                cand_idx, cand_price = i, high[i]
        else:
            if high[i] > cand_price:
                cand_idx, cand_price = i, high[i]
            elif low[i] <= cand_price * (1 - pct):
                pivots.append((cand_idx, cand_price, "H"))
                trend = "down"
                cand_idx, cand_price = i, low[i]
    # 附加暂定极值
    last_type = "L" if trend == "down" else "H"
    if cand_idx != pivots[-1][0]:
        pivots.append((cand_idx, cand_price, last_type))
    return pivots


def build_contractions(df: pd.DataFrame, start: int, cfg: dict) -> list[Contraction]:
    """从底部起点 start 开始，把摆动点合并成一段段收缩。"""
    high = df["high"].to_numpy()[start:]
    low = df["low"].to_numpy()[start:]
    vol = df["volume"].to_numpy()[start:]
    piv = zigzag(high, low, cfg["zigzag_pct"])

    segs = []  # [peak_i, peak, trough_i, trough]
    for idx, price, typ in piv:
        if typ == "H":
            if not segs:
                segs.append([idx, price, None, None])
                continue
            cur = segs[-1]
            if cur[2] is None:          # 高点后还没出现低点：抬高高点
                if price >= cur[1]:
                    cur[0], cur[1] = idx, price
                continue
            drop = cur[1] - cur[3]
            recovery = (price - cur[3]) / drop if drop > 0 else 1.0
            if recovery >= cfg["recovery_ratio"]:
                segs.append([idx, price, None, None])
            # 否则只是下跌途中的小反弹，并入当前收缩
        else:  # 'L'
            if not segs:
                continue
            cur = segs[-1]
            if cur[3] is None or price < cur[3]:
                cur[2], cur[3] = idx, price

    # 最后一段：高点之后若尚未出现确认低点，用之后的最低价
    out = []
    for k, (pi, p, ti, t) in enumerate(segs):
        end = segs[k + 1][0] if k + 1 < len(segs) else len(high)
        if ti is None:
            # 高点出现后不足 3 根K线：还看不出是否回撤，不算一段收缩
            if end - pi < 4:
                continue
            rel = int(np.argmin(low[pi + 1:end])) + 1
            ti, t = pi + rel, low[pi + rel]
        depth = (p - t) / p if p > 0 else 0.0
        v = float(np.mean(vol[pi:max(ti, pi) + 1])) if ti >= pi else float(vol[pi])
        out.append(Contraction(pi + start, float(p), ti + start, float(t), float(depth), v))
    return out


def trend_template(df: pd.DataFrame, cfg: dict) -> tuple[bool, dict]:
    c = df["close"]
    n = len(df)
    checks = {}
    fast = c.rolling(cfg["ma_fast"]).mean()
    mid = c.rolling(cfg["ma_mid"]).mean()
    slow = c.rolling(cfg["ma_slow"]).mean()
    last = c.iloc[-1]

    have_slow = n >= cfg["ma_slow"] + cfg["ma_slow_rising_bars"]
    have_mid = n >= cfg["ma_mid"]
    checks["price>MA50"] = bool(last > fast.iloc[-1]) if n >= cfg["ma_fast"] else False
    if have_mid:
        checks["price>MA150"] = bool(last > mid.iloc[-1])
        checks["MA50>MA150"] = bool(fast.iloc[-1] > mid.iloc[-1])
    if have_slow:
        checks["price>MA200"] = bool(last > slow.iloc[-1])
        checks["MA150>MA200"] = bool(mid.iloc[-1] > slow.iloc[-1])
        checks["MA200 rising"] = bool(slow.iloc[-1] > slow.iloc[-1 - cfg["ma_slow_rising_bars"]])

    yr = df.iloc[-cfg["year_bars"]:]
    lo, hi = yr["low"].min(), yr["high"].max()
    checks[f">= {cfg['min_above_low_pct']*100:.0f}% above 52w low"] = bool(last >= lo * (1 + cfg["min_above_low_pct"]))
    checks[f"within {cfg['max_below_high_pct']*100:.0f}% of 52w high"] = bool(last >= hi * (1 - cfg["max_below_high_pct"]))
    return all(checks.values()), checks


# ----------------------------------------------------------------------------
# 主判定
# ----------------------------------------------------------------------------
def _detect_base(df: pd.DataFrame, symbol: str, cfg: dict) -> VCPResult:
    """只判定形态本身（不含突破），df 的最后一根是最新已收盘K线。"""
    r = VCPResult(symbol=symbol)
    n = len(df)
    r.last_close = float(df["close"].iloc[-1])

    if n < cfg["min_history_bars"]:
        r.reasons.append(f"历史K线不足 {cfg['min_history_bars']} 根")
        return r

    # 1. 趋势模板
    tt_ok, tt_checks = trend_template(df, cfg)
    r.checks.update({f"趋势:{k}": v for k, v in tt_checks.items()})
    if cfg["require_trend_template"] and not tt_ok:
        failed = [k for k, v in tt_checks.items() if not v]
        r.reasons.append("趋势模板不符: " + ", ".join(failed))

    # 2. 底部起点 = 最近 base_max_bars 根内最高点
    win_start = max(0, n - cfg["base_max_bars"])
    highs = df["high"].to_numpy()
    start = win_start + int(np.argmax(highs[win_start:]))
    r.base_start_idx = start
    r.base_bars = n - start
    if r.base_bars < cfg["base_min_bars"]:
        r.checks["底部长度"] = False
        r.reasons.append(f"底部太短({r.base_bars}根 < {cfg['base_min_bars']})")
        return r
    r.checks["底部长度"] = True

    # 底部前的上涨
    pre = df.iloc[max(0, start - cfg["prior_uptrend_bars"]):start + 1]
    if len(pre) > 5:
        gain = df["high"].iloc[start] / pre["low"].min() - 1
        ok = gain >= cfg["prior_uptrend_pct"]
        r.checks[f"底部前涨幅>={cfg['prior_uptrend_pct']*100:.0f}%"] = bool(ok)
        if not ok:
            r.reasons.append(f"底部前涨幅不足({gain*100:.0f}%)")

    # 3. 收缩
    cons = build_contractions(df, start, cfg)
    # 若最后一段几乎没有回撤（价格就在高点），合并时会出现 depth≈0 的段：不算一段收缩
    if len(cons) > 1 and cons[-1].depth < 0.005:
        cons = cons[:-1]
    r.contractions = cons
    if not cons:
        r.reasons.append("找不到收缩")
        return r

    r.pivot = cons[-1].peak
    r.pivot_idx = cons[-1].peak_idx
    r.distance_to_pivot = (r.pivot - r.last_close) / r.last_close

    k = len(cons)
    ok_count = cfg["min_contractions"] <= k <= cfg["max_contractions"]
    r.checks[f"收缩次数{cfg['min_contractions']}~{cfg['max_contractions']}"] = ok_count
    if not ok_count:
        r.reasons.append(f"收缩次数={k}")

    depths = [c.depth for c in cons]
    ok_shrink = all(depths[i] <= depths[i - 1] * (1 + cfg["contraction_tolerance"]) for i in range(1, k))
    ok_shrink = ok_shrink and (k < 2 or depths[-1] < depths[0])
    r.checks["回撤幅度逐次缩小"] = ok_shrink
    if not ok_shrink:
        r.reasons.append("回撤没有逐次缩小: " + r.depths_text())

    ok_first = depths[0] <= cfg["max_first_depth"]
    r.checks[f"首次回撤<={cfg['max_first_depth']*100:.0f}%"] = ok_first
    if not ok_first:
        r.reasons.append(f"首次回撤过深({depths[0]*100:.1f}%)")

    ok_last = cfg["min_last_depth"] <= depths[-1] <= cfg["max_last_depth"]
    r.checks[f"末次回撤<={cfg['max_last_depth']*100:.0f}%"] = ok_last
    if not ok_last:
        r.reasons.append(f"末次回撤不够紧({depths[-1]*100:.1f}%)")

    if cfg["require_higher_lows"]:
        ok_hl = all(cons[i].trough >= cons[i - 1].trough for i in range(1, k))
        r.checks["低点逐步抬高"] = ok_hl
        if not ok_hl:
            r.reasons.append("低点没有逐步抬高")

    # 价格不能跌破最后一段的低点（形态被破坏）
    ok_hold = r.last_close >= cons[-1].trough
    r.checks["未跌破末段低点"] = ok_hold
    if not ok_hold:
        r.reasons.append("已跌破末段低点")

    # 4. 成交量萎缩
    vol = df["volume"]
    vma = vol.rolling(cfg["volume_ma"], min_periods=10).mean()
    if cfg["require_volume_dryup"]:
        ok_v1 = k < 2 or cons[-1].avg_volume < cons[0].avg_volume
        recent = vol.iloc[-cfg["dryup_recent_bars"]:].mean()
        ok_v2 = bool(recent < vma.iloc[-1] * cfg["dryup_ratio"])
        r.checks["末段量<首段量"] = ok_v1
        r.checks[f"近{cfg['dryup_recent_bars']}根量<均量"] = ok_v2
        if not ok_v1:
            r.reasons.append("末段成交量没有小于首段")
        if not ok_v2:
            r.reasons.append("最近成交量没有萎缩")

    r.valid = all(r.checks.values()) if cfg["require_trend_template"] else all(
        v for kk, v in r.checks.items() if not kk.startswith("趋势:"))

    # 评分（越高越好，仅用于排序）
    if cons:
        tight = 1 - depths[-1] / max(cfg["max_last_depth"], 1e-9)
        shrink = 1 - depths[-1] / max(depths[0], 1e-9)
        prox = max(0.0, 1 - max(r.distance_to_pivot, 0) / max(cfg["pivot_proximity_pct"], 1e-9))
        r.score = round(100 * (0.35 * max(tight, 0) + 0.35 * max(shrink, 0) + 0.30 * prox), 1)
    return r


def detect_vcp(df: pd.DataFrame, symbol: str = "", config: Optional[dict] = None) -> VCPResult:
    """
    df: 已收盘的K线，列 open/high/low/close/volume，按时间升序。
    返回 VCPResult。status:
      BREAKOUT  前一根K线时形态成立，最新一根收盘带量突破 pivot
      SETUP     形态成立，现价位于 pivot 下方 pivot_proximity_pct 以内
      NONE      其他
    """
    cfg = {**DEFAULTS, **(config or {})}
    df = df.reset_index(drop=True)

    # 先看：截止到上一根K线，形态是否成立 → 最新一根是否突破
    if len(df) > cfg["min_history_bars"] + 1:
        prev = _detect_base(df.iloc[:-1], symbol, cfg)
        if prev.valid and prev.pivot is not None:
            last = df.iloc[-1]
            vma = df["volume"].iloc[:-1].rolling(cfg["volume_ma"], min_periods=10).mean().iloc[-1]
            vr = float(last["volume"] / vma) if vma and vma > 0 else 0.0
            ext = last["close"] / prev.pivot - 1
            if last["close"] > prev.pivot and vr >= cfg["breakout_volume_ratio"] and ext <= cfg["breakout_max_extension"]:
                prev.status = "BREAKOUT"
                prev.last_close = float(last["close"])
                prev.distance_to_pivot = (prev.pivot - prev.last_close) / prev.last_close
                prev.breakout_volume_ratio = round(vr, 2)
                prev.checks[f"突破量>={cfg['breakout_volume_ratio']}x均量"] = True
                return prev

    r = _detect_base(df, symbol, cfg)
    if r.valid and r.distance_to_pivot is not None and 0 <= r.distance_to_pivot <= cfg["pivot_proximity_pct"]:
        r.status = "SETUP"
    elif r.valid:
        r.reasons.append(f"距 pivot {r.distance_to_pivot*100:.1f}%，尚未接近")
    return r

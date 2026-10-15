"""
VCP 基础工具：ZigZag 摆动点 + 把一个 Base 拆成一段段收缩（C1、C2…）。
完整的多周期判定逻辑（阶段、Base 编号、SCV 评分、交易计划、历史回放）在 engine.py。
"""
from __future__ import annotations

from dataclasses import dataclass

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

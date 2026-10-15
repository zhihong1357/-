"""
交易计划 + 历史验证（回测）。

交易计划（全部按规则计算，不含主观判断）：
  进场   SETUP：在 pivot 上方 entry_buffer 挂突破单（buy stop）
         BREAKOUT：按突破K线收盘价
  止损   最后一次收缩的最低点下方 stop_buffer（形态失败的位置）
  风险R  = 进场价 - 止损价
  止盈1  进场 + tp1_r × R，平掉 tp1_close_pct 仓位，剩余仓位止损移到进场价（保本）
  止盈2  进场 + tp2_r × R，全部平仓
  时间   持仓超过 max_hold_bars 根K线仍未走完，按收盘价离场
  仓位   每笔只亏账户的 risk_per_trade_pct：名义仓位 = 账户 × 风险% ÷ 止损距离%

历史验证：在过去 lookback_bars 根K线里，逐根重放“当时能看到的数据”，
找出当时触发的 BREAKOUT，按上面同一套规则模拟交易，统计结果。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from .detector import VCPResult, detect_vcp

TRADE_DEFAULTS = {
    "account_usdt": 1000,          # 账户资金
    "risk_per_trade_pct": 0.01,    # 每笔最多亏账户 1%
    "max_leverage": 5,             # 仓位最多用到 5 倍杠杆
    "entry_buffer": 0.002,         # 突破单挂在 pivot 上方 0.2%
    "stop_buffer": 0.005,          # 止损放在末段低点下方 0.5%
    "max_stop_pct": 0.08,          # 止损距离超过 8% 时提示风险偏大（Minervini 7~8% 原则）
    "tp1_r": 2.0,
    "tp1_close_pct": 0.5,
    "tp2_r": 3.0,
    "max_hold_bars": 60,
}

VALIDATION_DEFAULTS = {
    "enabled": True,
    "lookback_bars": 120,          # 回看最近 120 根K线（日线≈4个月）
    "show_cases": 3,               # 汇总里展示最近几个成功案例
}


@dataclass
class Plan:
    entry: float
    stop: float
    tp1: float
    tp2: float
    risk_pct: float                # 止损距离 %
    rr: float                      # 止盈2 的盈亏比
    notional: float                # 名义仓位 USDT
    margin: float                  # 需要的保证金 USDT
    leverage: float
    qty: float                     # 币数量
    risk_usdt: float               # 打到止损实际亏损
    warn: str = ""


def make_plan(r: VCPResult, cfg: dict, entry: Optional[float] = None) -> Optional[Plan]:
    t = {**TRADE_DEFAULTS, **(cfg or {})}
    if not r.pivot or not r.contractions:
        return None
    if entry is None:
        entry = r.last_close if r.status == "BREAKOUT" else r.pivot * (1 + t["entry_buffer"])
    stop = r.contractions[-1].trough * (1 - t["stop_buffer"])
    risk = entry - stop
    if risk <= 0:
        return None
    risk_pct = risk / entry
    acct = float(t["account_usdt"])
    notional = acct * t["risk_per_trade_pct"] / risk_pct
    warn = ""
    if notional > acct * t["max_leverage"]:
        notional = acct * t["max_leverage"]
        warn = f"仓位已按最大 {t['max_leverage']} 倍杠杆封顶"
    if risk_pct > t["max_stop_pct"]:
        warn = (warn + "；" if warn else "") + f"止损距离 {risk_pct*100:.1f}% 超过 {t['max_stop_pct']*100:.0f}%，风险偏大"
    lev = max(1.0, np.ceil(notional / acct))
    return Plan(
        entry=entry, stop=stop,
        tp1=entry + t["tp1_r"] * risk, tp2=entry + t["tp2_r"] * risk,
        risk_pct=risk_pct, rr=t["tp2_r"],
        notional=notional, margin=notional / lev, leverage=lev,
        qty=notional / entry, risk_usdt=notional * risk_pct, warn=warn,
    )


def simulate(df: pd.DataFrame, start: int, plan: Plan, cfg: dict) -> dict:
    """从第 start+1 根K线开始按计划走：同一根K线既碰止损又碰止盈时，保守地算止损。"""
    t = {**TRADE_DEFAULTS, **(cfg or {})}
    hi, lo, cl = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    stop, part, result_r = plan.stop, 1.0, 0.0
    tp1_hit, peak = False, plan.entry
    end = min(len(df) - 1, start + t["max_hold_bars"])
    for i in range(start + 1, end + 1):
        peak = max(peak, hi[i])
        if lo[i] <= stop:
            result_r += part * ((stop - plan.entry) / (plan.entry - plan.stop))
            return _res("止损" if not tp1_hit else "止盈1后保本离场", i, result_r, tp1_hit, peak, plan)
        if not tp1_hit and hi[i] >= plan.tp1:
            tp1_hit = True
            result_r += t["tp1_close_pct"] * t["tp1_r"]
            part -= t["tp1_close_pct"]
            stop = plan.entry
        if tp1_hit and hi[i] >= plan.tp2:
            result_r += part * t["tp2_r"]
            return _res("止盈2 全部达成", i, result_r, True, peak, plan)
    if end >= len(df) - 1 and end - start < t["max_hold_bars"]:
        open_r = part * ((cl[end] - plan.entry) / (plan.entry - plan.stop))
        return _res("持仓中", end, result_r + open_r, tp1_hit, peak, plan, open_=True)
    result_r += part * ((cl[end] - plan.entry) / (plan.entry - plan.stop))
    return _res("到期按收盘离场", end, result_r, tp1_hit, peak, plan)


def _res(outcome, idx, r, tp1, peak, plan, open_=False):
    return {"outcome": outcome, "exit_idx": idx, "r": round(r, 2), "tp1_hit": tp1,
            "max_gain_pct": round((peak / plan.entry - 1) * 100, 1), "open": open_}


def validate_symbol(df: pd.DataFrame, symbol: str, vcp_cfg: dict, trade_cfg: dict, val_cfg: dict) -> list[dict]:
    """在最近 lookback_bars 根K线里，找出当时触发的 BREAKOUT 并模拟交易。"""
    v = {**VALIDATION_DEFAULTS, **(val_cfg or {})}
    vc = {**vcp_cfg}
    n = len(df)
    first = max(vc.get("min_history_bars", 120) + 2, n - v["lookback_bars"])
    vol = df["volume"].to_numpy()
    vma = pd.Series(vol).rolling(vc.get("volume_ma", 50), min_periods=10).mean().shift(1).to_numpy()
    need = vc.get("breakout_volume_ratio", 1.5)
    trades, i = [], first
    while i < n:
        # 先用成交量快速筛选，没放量的K线不可能是突破
        if not (vma[i] > 0 and vol[i] >= vma[i] * need):
            i += 1
            continue
        sub = df.iloc[: i + 1]
        r = detect_vcp(sub, symbol, vc)
        if r.status != "BREAKOUT":
            i += 1
            continue
        plan = make_plan(r, trade_cfg, entry=float(df["close"].iloc[i]))
        if plan is None:
            i += 1
            continue
        sim = simulate(df, i, plan, trade_cfg)
        trades.append({
            "symbol": symbol, "idx": i, "time": df["time"].iloc[i] if "time" in df else i,
            "pivot": r.pivot, "depths": r.depths_text(), "plan": plan, "result": r, **sim,
        })
        i = sim["exit_idx"] + 1        # 一笔交易结束前不重复开仓
    return trades

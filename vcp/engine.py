"""
多周期 VCP 引擎（日线判断阶段 + 4 小时找收缩和进场），输出格式参照“师父”的信号报告。

流程：
  1. Stage（日线）：先有一段 ≥100% 的大涨（对数K线上翻倍），且收盘在日线 MA150 上方 → Stage 2；
     上市不足 200 根日线标记为 NEW
  2. Base（4h）：从那段大涨的起涨低点开始，按“回撤 ≥ base_min_depth 开始一个 Base，收盘创新高结束”
     切成 B1、B2…；超过 2 个 Base 判定失效（LATE）
  3. 每个 Base：C1 = 最高点 → 回调最低点；之后每段收缩的低点必须更高（没抬高就并入前一段）；
     合格（2~6 次收缩、逐次变小、C1 ≤ 35%、末段 ≤ 15%）→ CONFIRMED
  4. Pivot = 最后一次收缩的高点；Entry = Pivot × (1 + 挂单缓冲)；Stop = 末段低点 × (1 - 缓冲)
  5. SCV 打分：S 结构 / C 收缩 / V 成交量，合计 → 评级
  6. 价格接近 Entry → SETUP；4h 收盘站上 Entry → BREAKOUT

全部规则都是可量化的，参数在 config.yaml 的 strategy: 段调整。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from .detector import build_contractions

DEFAULTS = {
    # 周期
    "exec_tf": "4h",
    "stage_tf": "1d",
    "exec_bars": 900,               # 4h 拉 900 根 ≈ 150 天（要覆盖整段大涨才能正确数 Base）
    "stage_bars": 400,              # 日线拉 400 根
    # 阶段（日线）：先有 ≥100% 的大涨，且收盘在 MA150 上方 = Stage 2
    "new_listing_bars": 200,        # 日线不足 200 根 = 新币 NEW
    "min_advance_pct": 1.00,        # 起涨低点 → 之后最高点 涨幅 ≥ 100%（对数K线上即翻倍）
    "advance_lookback": 365,        # 在最近 365 根日线里找这段大涨
    "stage_ma": 150,                # 收盘必须在日线 MA150 上方（不足 150 根用 MA50，再不足用 MA20）
    # Base / 收缩（4h）
    "base_min_depth": 0.08,         # 从高点回撤 ≥ 8% 才算开始一个 Base
    "base_min_bars": 10,            # Base 至少 10 根 4h K线
    "zigzag_pct": 0.03,
    "recovery_ratio": 0.35,         # 反弹收回上段跌幅 ≥ 35% 才算新的一段收缩
    "min_contractions": 2,
    "max_contractions": 6,
    "max_bases": 2,                 # 超过 2 个 Base 判定失效（后期形态）
    "max_first_depth": 0.35,        # C1 ≤ 35%（Minervini 原版）
    "max_last_depth": 0.15,
    "contraction_tolerance": 0.10,
    "small_c1_pct": 0.20,           # C1 ≤ 20% 视为“C1偏小”（加分）
    # 进场 / 计划
    "entry_buffer": 0.005,          # 挂单缓冲 0.5%
    "stop_buffer": 0.005,
    "be_r": 0.8,                    # 涨到 Entry + 0.8R 时止损移到保本
    "plan_r": 5.0,                  # 计划止盈 = Entry + 5R
    "chart_tp_lookback": 180,       # 结构止盈：最近 180 根 4h 内的最高点
    "near_entry_pct": 0.03,         # 现价在 Entry 下方 3% 内 = 接近入场
    "max_extension_pct": 0.03,      # 已高出 Entry 3% 以上不再提示（防追高）
    "breakout_lag_bars": 3,         # 最近 3 根 4h 内的突破都算
    # 成交量
    "volume_ma": 50,
    "vol_expansion": 1.2,
    "vol_quiet": 0.8,
    "vol_high": 1.5,
    "vol_low": 0.7,
    "liquidity_cap_pct": 0.10,      # 单笔上限 = 最新 4h 成交额的 10%
    # 评级
    "min_rating": "FAIR",           # 低于这个评级不提醒：WEAK < FAIR < GOOD < EXCELLENT
    # 仓位
    "account_usdt": 1000,
    "risk_per_trade_pct": 0.01,
    "max_leverage": 5,
    "max_risk_pct": 0.10,           # 入场风险 > 10% → 半仓
    # 历史回放
    "max_hold_bars": 180,           # 最多持有 180 根 4h（30 天）
}

RATINGS = ["WEAK", "FAIR", "GOOD", "EXCELLENT"]


def rating_of(total: float) -> str:
    if total >= 4:
        return "EXCELLENT"
    if total >= 2:
        return "GOOD"
    if total >= 0.5:
        return "FAIR"
    return "WEAK"


# ----------------------------------------------------------------------------
@dataclass
class Base:
    no: int
    start: int
    end: int                 # 不含；== len(df) 表示仍在进行中
    high: float
    low: float
    cons: list
    ok: bool
    status: str              # OK / FAIL / WIDE / CONFIRMED / FORMING
    current: bool

    def label(self) -> str:
        return f"B{self.no} {self.status if self.status != 'CONFIRMED' else 'OK'}({len(self.cons)}C)"


@dataclass
class Signal:
    symbol: str
    status: str = "NONE"                     # NONE / SETUP / BREAKOUT
    reasons: list = field(default_factory=list)
    stage: dict = field(default_factory=dict)
    bases: list = field(default_factory=list)
    base: Optional[Base] = None
    pivot: Optional[float] = None
    entry: Optional[float] = None
    stop: Optional[float] = None
    be: Optional[float] = None
    plan_tp: Optional[float] = None
    chart_tp: Optional[float] = None
    risk_pct: Optional[float] = None
    plan_rr: Optional[float] = None
    struct_rr: Optional[float] = None
    s: tuple = (0.0, [])
    c: tuple = (0.0, [])
    v: tuple = (0.0, [])
    total: float = 0.0
    rating: str = "WEAK"
    vol: dict = field(default_factory=dict)
    size: dict = field(default_factory=dict)
    last_close: Optional[float] = None
    near_entry: bool = False
    valid: bool = False

    def to_row(self) -> dict:
        b = self.base
        return {
            "symbol": self.symbol, "status": self.status, "valid": self.valid, "rating": self.rating,
            "score": self.total, "stage": self.stage.get("label"), "base": f"B{b.no}" if b else "",
            "pattern": pattern_text(b), "entry": self.entry, "stop": self.stop, "plan_tp": self.plan_tp,
            "chart_tp": self.chart_tp, "risk_pct": self.risk_pct, "last_close": self.last_close,
            "reasons": "; ".join(self.reasons),
        }


def pattern_text(b: Optional[Base]) -> str:
    return "-".join(f"C{i + 1}" for i in range(len(b.cons))) if b and b.cons else ""


# ----------------------------------------------------------------------------
# 1. 阶段（日线）
# ----------------------------------------------------------------------------
def stage_info(d1: pd.DataFrame, cfg: dict) -> dict:
    """Stage 2 = 最近一段 ≥100% 的大涨（低点在前、高点在后）+ 日线收盘在 MA150 上方。"""
    n = len(d1)
    if n < 20:
        return {"label": "NEW/S?", "s2": False, "new": True, "bars": n, "why": "日线不足 20 根"}
    c = d1["close"]
    last = float(c.iloc[-1])
    new = n < cfg["new_listing_bars"]
    k = cfg["stage_ma"] if n >= cfg["stage_ma"] else (50 if n >= 50 else 20)
    ma = float(c.rolling(k).mean().iloc[-1])
    w = d1.iloc[-cfg["advance_lookback"]:].reset_index(drop=True)
    hi_i = int(w["high"].to_numpy().argmax())
    lo_i = int(w["low"].to_numpy()[: hi_i + 1].argmin())
    lo, hi = float(w["low"].iloc[lo_i]), float(w["high"].iloc[hi_i])
    adv = hi / lo - 1 if lo > 0 else 0.0
    above = last > ma
    big = adv >= cfg["min_advance_pct"]
    s2 = bool(above and big)
    if s2:
        st = "S2"
    elif not above:
        st = "S4" if last < lo + (hi - lo) * 0.5 else "S3"
    else:
        st = "S1"
    why = []
    if not big:
        why.append(f"涨幅 {adv * 100:.0f}% < {cfg['min_advance_pct'] * 100:.0f}%")
    if not above:
        why.append(f"收盘在日线 MA{k} 下方")
    return {"label": f"NEW/{st}" if new else st, "s2": s2, "new": new, "bars": n, "ma": k, "ma_val": ma,
            "adv": adv, "adv_low": lo, "adv_high": hi, "above_ma": above, "adv_low_time": w["time"].iloc[lo_i],
            "why": "，".join(why)}


# ----------------------------------------------------------------------------
# 2. Base 切分（4h）
# ----------------------------------------------------------------------------
def merge_higher_lows(cons: list, vol: np.ndarray) -> list:
    """VCP 要求每段收缩的低点比上一段高。某段低点没抬高 → 说明还是同一次回调，并入前一段。
    这样 C1 永远是“从最高点到回调最低点”，之后的每个 C 都是更高的低点。"""
    out = []
    for c in cons:
        while out and c.trough <= out[-1].trough:
            p = out.pop()
            peak_idx, peak = (p.peak_idx, p.peak) if p.peak >= c.peak else (c.peak_idx, c.peak)
            v = float(vol[peak_idx:c.trough_idx + 1].mean())
            c = type(c)(peak_idx, peak, c.trough_idx, c.trough, (peak - c.trough) / peak, v)
        out.append(c)
    return out


def _eval_cons(cons: list, cfg: dict) -> tuple[bool, str]:
    k = len(cons)
    if k == 0:
        return False, "FORMING"
    d = [x.depth for x in cons]
    if d[0] > cfg["max_first_depth"]:
        return False, "WIDE"
    if k < cfg["min_contractions"]:
        return False, "FORMING"
    if k > cfg["max_contractions"]:
        return False, "FAIL"
    shrink = all(d[i] <= d[i - 1] * (1 + cfg["contraction_tolerance"]) for i in range(1, k)) and d[-1] < d[0]
    if not shrink or d[-1] > cfg["max_last_depth"]:
        return False, "FAIL"
    return True, "OK"


def segment_bases(df: pd.DataFrame, cfg: dict, stage: dict | None = None) -> list[Base]:
    h, l, c = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    n = len(df)
    if n < 20:
        return []
    # 从日线那段 ≥100% 大涨的起涨低点开始数 Base（4h 数据不够早时用 4h 窗口内的最低点）
    first = 0
    if stage and stage.get("adv_low_time") is not None:
        first = min(int((df["time"] < stage["adv_low_time"]).sum()), n - 1)
    start = first + int(np.argmin(l[first:]))
    spans, rh, i = [], start, start
    while i < n:
        if h[i] >= h[rh]:
            rh = i
        elif l[i] <= h[rh] * (1 - cfg["base_min_depth"]):
            b0, j = rh, i
            while j < n and c[j] <= h[b0]:
                j += 1
            if j - b0 >= cfg["base_min_bars"] or j == n:
                spans.append((b0, j))
            if j >= n:
                break
            rh = i = j
            continue
        i += 1
    bases = []
    for no, (b0, end) in enumerate(spans, 1):
        cons = build_contractions(df.iloc[:end], b0, cfg)
        if len(cons) > 1 and cons[-1].depth < 0.005:
            cons = cons[:-1]
        cons = merge_higher_lows(cons, df["volume"].to_numpy())
        ok, st = _eval_cons(cons, cfg)
        current = end >= n
        if no > cfg["max_bases"]:
            ok, st = False, "LATE"
        if current and ok:
            st = "CONFIRMED"
        bases.append(Base(no, b0, end, float(h[b0]), float(l[b0:end].min()), cons, ok, st, current))
    return bases


# ----------------------------------------------------------------------------
# 3. 成交量
# ----------------------------------------------------------------------------
def volume_info(df: pd.DataFrame, cfg: dict) -> dict:
    v = df["volume"]
    vma = float(v.rolling(cfg["volume_ma"], min_periods=10).mean().iloc[-1] or 0)
    last = df.iloc[-1]
    lv = float(last["volume"])
    ratio = lv / vma if vma > 0 else 0.0
    level = "HIGH" if ratio >= cfg["vol_high"] else ("LOW" if ratio < cfg["vol_low"] else "NORMAL")
    bull = last["close"] >= last["open"]
    kind = "EXPANSION" if ratio >= cfg["vol_expansion"] else ("QUIET" if ratio < cfg["vol_quiet"] else "NORMAL")
    recent = float(v.iloc[-5:].mean())
    trend = "EXPANDING" if recent > vma * 1.1 else ("CONTRACTING" if recent < vma * 0.9 else "FLAT")
    return {
        "last": lv, "ma": vma, "ratio": ratio, "level": level,
        "raw": lv / float(last["close"]) if last["close"] else 0.0,
        "est24h": vma * 6, "trend": trend, "candle": f"{'BULL' if bull else 'BEAR'}_{kind}",
        "liq_cap": lv * cfg["liquidity_cap_pct"],
    }


# ----------------------------------------------------------------------------
# 4. 核心：对“当前这一刻”做完整分析（不含突破判断）
# ----------------------------------------------------------------------------
def _core(symbol: str, df: pd.DataFrame, d1: pd.DataFrame, cfg: dict) -> Signal:
    sig = Signal(symbol=symbol)
    sig.last_close = float(df["close"].iloc[-1])
    sig.stage = stage_info(d1, cfg)
    if not sig.stage["s2"]:
        sig.reasons.append(f"日线阶段 {sig.stage['label']}，不是 Stage 2（{sig.stage.get('why', '')}）")
    sig.bases = segment_bases(df, cfg, sig.stage)
    cur = sig.bases[-1] if sig.bases and sig.bases[-1].current else None
    sig.base = cur
    if cur is None:
        sig.reasons.append("价格在创新高，没有正在形成的 Base")
        return sig
    if not cur.cons:
        sig.reasons.append("当前 Base 还没有收缩")
        return sig
    last_c = cur.cons[-1]
    sig.pivot = last_c.peak
    sig.entry = sig.pivot * (1 + cfg["entry_buffer"])
    sig.stop = last_c.trough * (1 - cfg["stop_buffer"])
    R = sig.entry - sig.stop
    if R <= 0:
        sig.reasons.append("止损高于进场价")
        return sig
    sig.risk_pct = R / sig.entry
    sig.be = sig.entry + cfg["be_r"] * R
    sig.plan_tp = sig.entry + cfg["plan_r"] * R
    sig.plan_rr = cfg["plan_r"]
    hh = float(df["high"].iloc[-cfg["chart_tp_lookback"]:].max())
    if hh > sig.entry * 1.01:
        sig.chart_tp = hh
        sig.struct_rr = (hh - sig.entry) / R
    if cur.status == "LATE":
        sig.reasons.append(f"已是第 {cur.no} 个 Base，超过 {cfg['max_bases']} 个判定失效")
    elif not cur.ok:
        sig.reasons.append(f"当前 Base {cur.label()}：收缩不合格 {depths(cur)}")
    if sig.last_close < last_c.trough:
        sig.reasons.append("已跌破末段收缩低点")
    sig.valid = sig.stage["s2"] and cur.ok and sig.last_close >= last_c.trough
    return sig


def depths(b: Base) -> str:
    return " ".join(f"C{i + 1}:{c.depth * 100:.1f}%" for i, c in enumerate(b.cons))


def _score(sig: Signal, df: pd.DataFrame, cfg: dict, breakout: bool) -> None:
    # S 结构
    s, sr = 0.0, []
    if sig.stage.get("s2"):
        s += 1.0
        sr.append("Stage2")
    else:
        s -= 2.0
        sr.append(f"非Stage2({sig.stage.get('label')})")
    c = df["close"]
    mas = [c.rolling(k).mean().iloc[-1] for k in (50, 150, 200) if len(df) >= k]
    seq = [float(c.iloc[-1])] + [float(m) for m in mas]
    if len(seq) >= 3 and all(a > b for a, b in zip(seq, seq[1:])):
        s += 0.5
        sr.append("Close确认排列")
    elif mas and seq[0] < seq[1]:
        s -= 0.5
        sr.append("收盘在MA50下")
    # C 收缩
    b = sig.base
    cc, cr = 0.0, []
    if b and b.cons:
        if b.cons[0].depth <= cfg["small_c1_pct"]:
            cc += 0.5
            cr.append("C1偏小")
        if b.ok:
            cc += 1.0
            cr.append("Base确认")
        if b.no <= cfg["max_bases"]:
            cc += 0.5
            cr.append(f"第{b.no}Base")
        else:
            cc -= 2.0
            cr.append(f"第{b.no}Base(超过{cfg['max_bases']}个，失效)")
        if breakout:
            cc += 1.0
            cr.append("Pivot突破")
    # V 成交量
    vi = volume_info(df, cfg)
    sig.vol = vi
    vv, vr = 0.0, []
    if vi["candle"] == "BEAR_EXPANSION":
        vv -= 2.0
        vr.append("阴足放量")
    elif vi["candle"] == "BULL_EXPANSION":
        if breakout and vi["ratio"] >= cfg["vol_high"]:
            vv += 2.0
            vr.append("突破放量")
        else:
            vv += 1.0
            vr.append("阳足放量")
    elif vi["candle"] == "BEAR_QUIET":
        vv += 0.5
        vr.append("阴足缩量")
    if b and len(b.cons) >= 2 and b.cons[-1].avg_volume < b.cons[0].avg_volume:
        vv += 1.0
        vr.append("末段量缩")
    sig.s, sig.c, sig.v = (s, sr), (cc, cr), (vv, vr)
    sig.total = round(s + cc + vv, 2)
    sig.rating = rating_of(sig.total)


def _size(sig: Signal, cfg: dict) -> None:
    if sig.entry is None or not sig.risk_pct:
        return
    tag = "NEW_LISTING" if sig.stage.get("new") else "MATURE"
    factor, rule = 1.0, "NORMAL_SIZE"
    if sig.rating == "WEAK":
        factor, rule = 0.0, "SKIP"
    elif sig.risk_pct > cfg["max_risk_pct"]:
        factor, rule = 0.5, "HALF_SIZE"
    acct = float(cfg["account_usdt"])
    notional = acct * cfg["risk_per_trade_pct"] / sig.risk_pct * factor
    notes = []
    if notional > acct * cfg["max_leverage"]:
        notional = acct * cfg["max_leverage"]
        notes.append(f"按 {cfg['max_leverage']}x 杠杆封顶")
    cap = sig.vol.get("liq_cap", 0)
    cap_ok = notional <= cap
    if not cap_ok and cap > 0:
        notional = cap
        notes.append("按流动性上限封顶")
    lev = max(1.0, float(np.ceil(notional / acct))) if notional > 0 else 0
    sig.size = {
        "rule": f"{tag}_{rule}", "notional": notional, "qty": notional / sig.entry if sig.entry else 0,
        "leverage": lev, "margin": notional / lev if lev else 0, "loss": notional * sig.risk_pct,
        "cap_ok": cap_ok, "notes": notes,
    }


def analyze(symbol: str, df: pd.DataFrame, d1: pd.DataFrame, config: Optional[dict] = None) -> Signal:
    """df = 已收盘的 4h K线；d1 = 已收盘的日线。"""
    cfg = {**DEFAULTS, **(config or {})}
    df = df.reset_index(drop=True)
    c = df["close"].to_numpy()
    # 最近几根 4h 内收盘突破 Entry？（形态用突破前的K线判断，避免突破K线改变结构）
    for lag in range(1, cfg["breakout_lag_bars"] + 1):
        if len(df) <= lag + 50:
            break
        prev = _core(symbol, df.iloc[:-lag], d1, cfg)
        if not (prev.valid and prev.entry):
            continue
        if np.any(c[-lag:] > prev.entry) and c[-1] >= prev.entry and c[-1] <= prev.entry * (1 + cfg["max_extension_pct"]):
            prev.last_close = float(c[-1])
            prev.status = "BREAKOUT"
            prev.near_entry = True
            _score(prev, df, cfg, breakout=True)
            _size(prev, cfg)
            return prev
    sig = _core(symbol, df, d1, cfg)
    _score(sig, df, cfg, breakout=False)
    _size(sig, cfg)
    if sig.entry:
        gap = (sig.entry - sig.last_close) / sig.entry
        sig.near_entry = -cfg["max_extension_pct"] <= gap <= cfg["near_entry_pct"]
        if sig.valid and 0 <= gap <= cfg["near_entry_pct"]:
            sig.status = "SETUP"
        elif sig.valid and gap > cfg["near_entry_pct"]:
            sig.reasons.append(f"距入场还有 {gap * 100:.1f}%")
        elif sig.valid and gap < 0:
            sig.reasons.append("已在入场价上方但没有收盘突破信号")
    if sig.status != "NONE" and RATINGS.index(sig.rating) < RATINGS.index(cfg["min_rating"]):
        sig.reasons.append(f"评级 {sig.rating} 低于 {cfg['min_rating']}")
        sig.status = "NONE"
    return sig


# ----------------------------------------------------------------------------
# 5. 历史回放：用同一套规则，逐根 4h 重演“当时看得到的数据”
# ----------------------------------------------------------------------------
def replay(symbol: str, df: pd.DataFrame, d1: pd.DataFrame, lookback: int, config: Optional[dict] = None) -> list:
    cfg = {**DEFAULTS, **(config or {})}
    df = df.reset_index(drop=True)
    n = len(df)
    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    t = df["time"]
    d1_close_time = d1["time"] + pd.Timedelta(days=1)
    i = max(120, n - lookback)
    trades = []
    while i < n:
        if h[i] <= h[max(0, i - 6):i].max():          # 没创近期新高，不可能突破
            i += 1
            continue
        sub = df.iloc[:i]
        d1s = d1[d1_close_time <= t.iloc[i]]
        sig = _core(symbol, sub, d1s, cfg)
        if not (sig.valid and sig.entry) or c[i - 1] >= sig.entry or h[i] < sig.entry:
            i += 1
            continue
        _score(sig, sub, cfg, breakout=False)
        if RATINGS.index(sig.rating) < RATINGS.index(cfg["min_rating"]):
            i += 1
            continue
        _size(sig, cfg)
        fill = max(sig.entry, o[i])
        res = simulate(df, i, fill, sig, cfg)
        trades.append({"symbol": symbol, "idx": i, "time": t.iloc[i], "fill": fill, "sig": sig, **res})
        i = res["exit_idx"] + 1
    return trades


def simulate(df: pd.DataFrame, i: int, fill: float, sig: Signal, cfg: dict) -> dict:
    h, l, c = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    R = fill - sig.stop
    stop, be_done, peak = sig.stop, False, fill
    end = min(len(df) - 1, i + cfg["max_hold_bars"])
    hit_chart = False
    for j in range(i, end + 1):
        peak = max(peak, h[j])
        if l[j] <= stop:
            r = (stop - fill) / R
            return _r("保本离场" if be_done else "止损", j, r, peak, fill, be_done, hit_chart)
        if sig.chart_tp and h[j] >= sig.chart_tp:
            hit_chart = True
        if not be_done and h[j] >= sig.be:
            be_done, stop = True, fill
        if h[j] >= sig.plan_tp:
            return _r("计划止盈达成", j, (sig.plan_tp - fill) / R, peak, fill, True, hit_chart)
    open_ = end == len(df) - 1 and end - i < cfg["max_hold_bars"]
    return _r("持仓中" if open_ else "到期离场", end, (c[end] - fill) / R, peak, fill, be_done, hit_chart, open_)


def _r(outcome, j, r, peak, fill, be, chart, open_=False):
    return {"outcome": outcome, "exit_idx": j, "r": round(float(r), 2), "open": open_, "be_hit": be,
            "chart_tp_hit": chart, "max_gain_pct": round((peak / fill - 1) * 100, 1),
            "success": outcome == "计划止盈达成" or (not open_ and r >= 1)}

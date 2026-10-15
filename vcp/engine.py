"""
多周期 VCP 引擎（日线判断阶段 + 4 小时找收缩和进场），输出格式参照“师父”的信号报告。

流程：
  1. Stage（日线）：Minervini 趋势模板 8 条（含 RS 相对强度 ≥ 70）→ Stage 2；
     上市不足 200 根日线标记为 NEW（用现有均线判断）；前期翻倍（≥100%）作为加分项
  2. Base（4h）：从那段大涨的起涨低点开始，按“回撤 ≥ base_min_depth 开始一个 Base，收盘创新高结束”
     切成 B1、B2…；超过 2 个 Base 判定失效（LATE）
  3. 每个 Base：C1 = 最高点 → 回调最低点；之后每段收缩的低点必须更高（没抬高就并入前一段）；
     合格（2~6 次收缩、逐次变小、C1 ≤ 35%、末段 ≤ 15%）→ CONFIRMED
  4. Pivot = 最后一次收缩的高点；Entry = Pivot × (1 + 挂单缓冲)；Stop = 末段低点 × (1 - 缓冲)；
     入场风险 > 10% 不做（Minervini：单笔止损不超过 10%）
  4b. 出场方案（exit_profile）：minervini / master / qullamaggie，历史回放会把三种都算一遍做对比
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
    # 阶段（日线）：Minervini 趋势模板
    "new_listing_bars": 200,        # 日线不足 200 根 = 新币 NEW（只用算得出来的均线）
    "ma_slow_rising_bars": 20,      # MA200 至少比 1 个月（20 根）前高；Minervini 更偏好 4~5 个月
    "min_above_low_pct": 0.30,      # 现价 ≥ 52 周低点 × 1.30
    "max_below_high_pct": 0.25,     # 现价 ≥ 52 周高点 × 0.75
    "min_rs": 70,                   # RS 相对强度（在本次扫描的所有币里排名百分位）≥ 70
    "strong_rs": 85,                # RS ≥ 85 加分
    "advance_lookback": 365,        # 在最近 365 根日线里找起涨低点（也是数 Base 的起点）
    "advance_bonus_pct": 1.00,      # 前期涨幅 ≥ 100%（翻倍）→ S 加分
    "require_advance": False,       # true = 把“前期翻倍”变成硬性条件
    # Base / 收缩（4h）
    "base_min_depth": 0.08,         # 从高点回撤 ≥ 8% 才算开始一个 Base
    "base_min_bars": 10,            # Base 至少 10 根 4h K线
    "zigzag_pct": 0.03,
    "recovery_ratio": 0.35,         # 反弹收回上段跌幅 ≥ 35% 才算新的一段收缩
    "min_contractions": 2,
    "max_contractions": 6,
    "max_bases": 2,                 # 超过 2 个 Base 判定失效（后期形态）
    "max_first_depth": 0.35,        # C1 ≤ 35%（Minervini 原版）
    "max_last_depth": 0.10,         # 最后一段 ≤ 10%（Minervini：末段通常是个位数的紧密收缩）
    "contraction_tolerance": 0.10,
    "small_c1_pct": 0.20,           # C1 ≤ 20% 视为“C1偏小”（加分）
    # 进场 / 计划
    "entry_buffer": 0.005,          # 挂单缓冲 0.5%
    "stop_buffer": 0.005,
    "exit_profile": "minervini",    # 出场方案：minervini / master / qullamaggie（见 PROFILES）
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
    # 扣分项（师父的做法：走满分，慢慢扣分）
    "min_daily_volume": 1_000_000,  # 最近 24h 成交额 < 100 万 USDT → 流动性差，扣分
    "wick_lookback": 3,             # 检查最近 3 根 4h（含突破K线）有没有长上影线
    "wick_ratio": 0.5,              # 上影线 ≥ 整根K线长度的 50% 视为长上影线
    "wick_min_range": 0.5,          # K线长度至少是近 20 根平均长度的 0.5 倍才算（忽略很小的K线）
    # 评级（Score = S、C、V 三项各 +1/-1 的合计，满分 3）
    "min_rating": "GOOD",           # 低于这个评级不提醒：WEAK < FAIR < GOOD < STRONG < EXCELLENT
    # 仓位
    "account_usdt": 1000,
    "risk_per_trade_pct": 0.01,
    "max_leverage": 5,
    "max_risk_pct": 0.10,           # 入场风险 > 10% → 不做（Minervini：单笔止损不超过 10%）
    # 历史回放
    "max_hold_bars": 180,           # 最多持有 180 根 4h（30 天）
}

RATINGS = ["WEAK", "FAIR", "GOOD", "STRONG", "EXCELLENT"]
SCORE_RATING = {3: "EXCELLENT", 2: "STRONG", 1: "GOOD", 0: "FAIR"}

# 出场方案（R = 入场价 - 止损价）。历史回放对同一批入场三种都算，方便比较哪种在加密货币上更好。
PROFILES = {
    # Minervini：涨到 2R 把止损移到保本；3R 卖一半（逢强卖出）；剩下的收盘跌破 MA120(4h ≈ 日线 20MA) 离场
    "minervini": {"name": "Minervini", "be_r": 2.0, "partial_r": 3.0, "partial_pct": 0.5,
                  "partial_after_bars": None, "tp_r": None, "trail_ma": 120},
    # 师父报告里的参数：0.8R 保本，5R 全部止盈
    "master": {"name": "师父(0.8R保本/5R止盈)", "be_r": 0.8, "partial_r": None, "partial_pct": 0,
               "partial_after_bars": None, "tp_r": 5.0, "trail_ma": None},
    # Qullamaggie 风格（公开资料整理，非官方）：3 天后若盈利先卖一半并保本，剩下跌破 MA60(4h ≈ 日线 10MA) 离场
    "qullamaggie": {"name": "Qullamaggie风格", "be_r": None, "partial_r": None, "partial_pct": 0.5,
                    "partial_after_bars": 18, "tp_r": None, "trail_ma": 60},
}


def rating_of(score: int) -> str:
    """Score = S/C/V 三项的正负号合计：三项全过 3 分 EXCELLENT，扣到 1 分 GOOD（师父例子），负分 WEAK。"""
    return SCORE_RATING.get(score, "WEAK" if score < 0 else "EXCELLENT")


def _sign(x: float) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)


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
    total: float = 0.0                       # S+C+V 细分合计（参考用）
    score: int = 0                           # 满分 3：S、C、V 每项 +1/0/-1
    rating: str = "WEAK"
    vol: dict = field(default_factory=dict)
    size: dict = field(default_factory=dict)
    last_close: Optional[float] = None
    near_entry: bool = False
    valid: bool = False
    rs: Optional[int] = None                 # 相对强度百分位（扫描完所有币后才算得出）
    rs_raw: float = 0.0

    def to_row(self) -> dict:
        b = self.base
        return {
            "symbol": self.symbol, "status": self.status, "valid": self.valid, "rating": self.rating,
            "score": self.score, "detail_total": self.total, "rs": self.rs, "stage": self.stage.get("label"), "base": f"B{b.no}" if b else "",
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
    """Minervini 趋势模板（RS 那一条在扫描完所有币后由 apply_rs 补上）。"""
    n = len(d1)
    if n < 20:
        return {"label": "NEW/S?", "s2": False, "new": True, "bars": n, "why": "日线不足 20 根", "checks": {}}
    c = d1["close"]
    last = float(c.iloc[-1])
    new = n < cfg["new_listing_bars"]
    rb = cfg["ma_slow_rising_bars"]
    ma = {k: c.rolling(k).mean() for k in (20, 50, 150, 200) if n >= k}
    lv = {k: float(m.iloc[-1]) for k, m in ma.items()}
    yr = d1.iloc[-365:]
    hi52, lo52 = float(yr["high"].max()), float(yr["low"].min())
    chk = {}
    if 200 in lv and n >= 200 + rb:
        chk["价格>MA150且>MA200"] = last > lv[150] and last > lv[200]
        chk["MA150>MA200"] = lv[150] > lv[200]
        chk["MA200向上(1个月)"] = lv[200] > float(ma[200].iloc[-1 - rb])
        chk["MA50>MA150且>MA200"] = lv[50] > lv[150] and lv[50] > lv[200]
    elif 150 in lv:
        chk["价格>MA150"] = last > lv[150]
        chk["MA50>MA150"] = lv[50] > lv[150]
        chk["MA150向上"] = lv[150] > float(ma[150].iloc[-6])
    else:
        k = 50 if 50 in lv else 20
        chk[f"MA{k}向上"] = lv[k] > float(ma[k].iloc[-6])
    k_fast = 50 if 50 in lv else 20
    chk[f"价格>MA{k_fast}"] = last > lv[k_fast]
    chk[f"距52周低点≥{cfg['min_above_low_pct'] * 100:.0f}%"] = last >= lo52 * (1 + cfg["min_above_low_pct"])
    chk[f"距52周高点≤{cfg['max_below_high_pct'] * 100:.0f}%"] = last >= hi52 * (1 - cfg["max_below_high_pct"])

    # 前期涨幅：最近 advance_lookback 根日线里，最高点之前的最低点 → 最高点
    w = d1.iloc[-cfg["advance_lookback"]:].reset_index(drop=True)
    hi_i = int(w["high"].to_numpy().argmax())
    lo_i = int(w["low"].to_numpy()[: hi_i + 1].argmin())
    lo, hi = float(w["low"].iloc[lo_i]), float(w["high"].iloc[hi_i])
    adv = hi / lo - 1 if lo > 0 else 0.0
    if cfg["require_advance"]:
        chk[f"前期涨幅≥{cfg['advance_bonus_pct'] * 100:.0f}%"] = adv >= cfg["advance_bonus_pct"]

    chk = {k: bool(v) for k, v in chk.items()}
    s2 = all(chk.values())
    ma150 = lv.get(150, lv.get(50, lv.get(20)))
    if s2:
        st = "S2"
    elif ma150 is not None and last < ma150:
        st = "S4" if 150 in ma and lv[150] < float(ma[150].iloc[-6]) else "S3"
    else:
        st = "S1"
    failed = [k for k, v in chk.items() if not v]
    return {"label": f"NEW/{st}" if new else st, "s2": s2, "new": new, "bars": n, "checks": chk,
            "why": "，".join(failed), "ma": 150 if 150 in lv else k_fast, "ma_val": ma150,
            "above_ma": ma150 is not None and last > ma150,
            "adv": adv, "adv_low": lo, "adv_high": hi, "adv_low_time": w["time"].iloc[lo_i]}


def rs_raw(d1: pd.DataFrame) -> float:
    """IBD/Minervini 风格的相对强度原始分：最近 3 个月涨幅权重 40%，前面三个季度各 20%。"""
    c = d1["close"].to_numpy()
    if len(c) < 30:
        return float("nan")

    def ret(a, b):
        a, b = min(a, len(c) - 1), min(b, len(c) - 1)
        return c[-1 - a] / c[-1 - b] - 1 if b > a else 0.0
    return 0.4 * ret(0, 63) + 0.2 * ret(63, 126) + 0.2 * ret(126, 189) + 0.2 * ret(189, 252)


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
    prof = PROFILES[cfg["exit_profile"]]
    sig.be = sig.entry + prof["be_r"] * R if prof["be_r"] else None
    target = prof["tp_r"] or prof["partial_r"]
    sig.plan_tp = sig.entry + target * R if target else None
    sig.plan_rr = target
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
    risk_ok = sig.risk_pct <= cfg["max_risk_pct"]
    if not risk_ok:
        sig.reasons.append(f"入场风险 {sig.risk_pct * 100:.1f}% > {cfg['max_risk_pct'] * 100:.0f}%（Minervini：止损不超过 10%）")
    sig.valid = sig.stage["s2"] and cur.ok and sig.last_close >= last_c.trough and risk_ok
    return sig


def depths(b: Base) -> str:
    return " ".join(f"C{i + 1}:{c.depth * 100:.1f}%" for i, c in enumerate(b.cons))


def upper_wick(df: pd.DataFrame, cfg: dict) -> Optional[int]:
    """最近 wick_lookback 根里有长上影线就返回它距今几根（0 = 最新一根），没有返回 None。"""
    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    rng = h - l
    avg = pd.Series(rng).rolling(20, min_periods=5).mean().to_numpy()
    for k in range(min(cfg["wick_lookback"], len(df))):
        j = len(df) - 1 - k
        if rng[j] <= 0 or rng[j] < avg[j] * cfg["wick_min_range"]:
            continue
        if (h[j] - max(o[j], c[j])) / rng[j] >= cfg["wick_ratio"]:
            return k
    return None


def _score(sig: Signal, df: pd.DataFrame, cfg: dict, breakout: bool) -> None:
    """
    扣分制：S / C / V 三项各自加减分，最后每项按正负记 +1 / -1，合计就是 Score（满分 3）。
    师父的四条扣分：不在 Stage 2、无量（成交额没超过 MA50）、长上影线、日成交额不到 100 万。
    """
    # S 结构
    s, sr = 0.0, []
    if sig.stage.get("s2"):
        s += 1.0
        sr.append("Stage2")
    else:
        s -= 2.0
        sr.append(f"非Stage2({sig.stage.get('label')}) -2")
    c = df["close"]
    mas = [c.rolling(k).mean().iloc[-1] for k in (50, 150, 200) if len(df) >= k]
    seq = [float(c.iloc[-1])] + [float(m) for m in mas]
    if len(seq) >= 3 and all(a > b for a, b in zip(seq, seq[1:])):
        s += 0.5
        sr.append("Close确认排列")
    elif mas and seq[0] < seq[1]:
        s -= 0.5
        sr.append("收盘在MA50下 -0.5")
    if sig.stage.get("adv", 0) >= cfg["advance_bonus_pct"]:
        s += 0.5
        sr.append(f"前期涨幅{sig.stage['adv'] * 100:.0f}%")
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
        else:
            cc -= 1.0
            cr.append(f"Base未确认({b.status}) -1")
        if b.no <= cfg["max_bases"]:
            cc += 0.5
            cr.append(f"第{b.no}Base")
        else:
            cc -= 2.0
            cr.append(f"第{b.no}Base(超过{cfg['max_bases']}个) -2")
        if breakout:
            cc += 1.0
            cr.append("Pivot突破")
    wick = upper_wick(df, cfg)
    if wick is not None:
        cc -= 2.0
        cr.append(f"长上影线({'最新K线' if wick == 0 else f'{wick}根前'}) -2")
    # V 成交量
    vi = volume_info(df, cfg)
    vi["day"] = float(df["volume"].iloc[-6:].sum())          # 最近 24h（6 根 4h）实际成交额
    sig.vol = vi
    vv, vr = 0.0, []
    if vi["candle"] == "BEAR_EXPANSION":
        vv -= 2.0
        vr.append("阴足放量 -2")
    if breakout:
        if vi["ratio"] <= 1.0:
            vv -= 2.0
            vr.append(f"无量突破(成交额{vi['ratio']:.2f}x 未超MA50) -2")
        elif vi["ratio"] >= cfg["vol_high"]:
            vv += 2.0
            vr.append("突破放量")
        else:
            vv += 1.0
            vr.append("突破量超MA50")
    elif vi["candle"] == "BULL_EXPANSION":
        vv += 0.5
        vr.append("阳足放量")
    elif vi["candle"] == "BEAR_QUIET":
        vv += 0.5
        vr.append("阴足缩量")
    if b and len(b.cons) >= 2 and b.cons[-1].avg_volume < b.cons[0].avg_volume:
        vv += 1.0
        vr.append("末段量缩")
    if vi["day"] < cfg["min_daily_volume"]:
        vv -= 2.0
        vr.append(f"日成交额{vi['day'] / 1e6:.2f}M<{cfg['min_daily_volume'] / 1e6:g}M 流动性差 -2")
    sig.s, sig.c, sig.v = (s, sr), (cc, cr), (vv, vr)
    _finalize(sig)


def _finalize(sig: Signal) -> None:
    sig.total = round(sig.s[0] + sig.c[0] + sig.v[0], 2)
    sig.score = _sign(sig.s[0]) + _sign(sig.c[0]) + _sign(sig.v[0])
    sig.rating = rating_of(sig.score)


def _size(sig: Signal, cfg: dict) -> None:
    if sig.entry is None or not sig.risk_pct:
        return
    tag = "NEW_LISTING" if sig.stage.get("new") else "MATURE"
    factor, rule = 1.0, "NORMAL_SIZE"
    if RATINGS.index(sig.rating) < RATINGS.index(cfg["min_rating"]) or sig.risk_pct > cfg["max_risk_pct"]:
        factor, rule = 0.0, "SKIP"
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
            prev.rs_raw = rs_raw(d1)
            _score(prev, df, cfg, breakout=True)
            _size(prev, cfg)
            return prev
    sig = _core(symbol, df, d1, cfg)
    sig.rs_raw = rs_raw(d1)
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


def apply_rs(sig: Signal, rs: int, cfg: Optional[dict] = None) -> None:
    """扫描完所有币后，把 RS 排名补进趋势模板和 S 分。"""
    cfg = {**DEFAULTS, **(cfg or {})}
    sig.rs = rs
    ok = rs >= cfg["min_rs"]
    sig.stage.setdefault("checks", {})[f"RS≥{cfg['min_rs']}"] = ok
    s, sr = sig.s
    if not ok:
        s -= 1.0
        sr = sr + [f"RS{rs}偏弱 -1"]
        if sig.stage.get("s2"):
            sig.stage["s2"] = False
            sig.stage["label"] = sig.stage["label"].replace("S2", "S2-")
            sig.stage["why"] = (sig.stage.get("why", "") + f"，RS {rs} < {cfg['min_rs']}").lstrip("，")
        sig.valid = False
        if sig.status != "NONE":
            sig.status = "NONE"
            sig.reasons.append(f"RS 相对强度 {rs} < {cfg['min_rs']}")
    elif rs >= cfg["strong_rs"]:
        s += 0.5
        sr = sr + [f"RS{rs}强"]
    sig.s = (s, sr)
    _finalize(sig)
    _size(sig, cfg)
    if sig.status != "NONE" and RATINGS.index(sig.rating) < RATINGS.index(cfg["min_rating"]):
        sig.reasons.append(f"评级 {sig.rating} 低于 {cfg['min_rating']}")
        sig.status = "NONE"


def rs_percentiles(raw: dict) -> dict:
    """{symbol: 原始分} → {symbol: 1~99 百分位}"""
    vals = {k: v for k, v in raw.items() if v == v}
    if not vals:
        return {}
    ser = pd.Series(vals).rank(pct=True)
    return {k: int(max(1, min(99, round(p * 99)))) for k, p in ser.items()}


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
        # 分数按“突破K线收盘时”计算（那时才看得到突破量和上影线）；不在这里按分数过滤，
        # 这样回放能比较高分和低分的真实结果，检验扣分制是否有效
        _score(sig, df.iloc[: i + 1], cfg, breakout=True)
        _size(sig, cfg)
        fill = max(sig.entry, o[i])
        by = {name: simulate(df, i, fill, sig, cfg, prof) for name, prof in PROFILES.items()}
        res = by[cfg["exit_profile"]]
        trades.append({"symbol": symbol, "idx": i, "time": t.iloc[i], "fill": fill, "sig": sig,
                       "score": sig.score, "rating": sig.rating,
                       "taken": RATINGS.index(sig.rating) >= RATINGS.index(cfg["min_rating"]),
                       "by_profile": by, **res})
        i = res["exit_idx"] + 1
    return trades


def simulate(df: pd.DataFrame, i: int, fill: float, sig: Signal, cfg: dict, prof: Optional[dict] = None) -> dict:
    """按出场方案模拟。同一根K线既碰止损又碰目标时，保守地算先止损。"""
    prof = prof or PROFILES[cfg["exit_profile"]]
    h, l, c = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    ma = df["close"].rolling(prof["trail_ma"]).mean().to_numpy() if prof["trail_ma"] else None
    R = fill - sig.stop
    stop, part, realized, peak = sig.stop, 1.0, 0.0, fill
    be_done = partial_done = False
    end = min(len(df) - 1, i + cfg["max_hold_bars"])
    hit_chart = False
    for j in range(i, end + 1):
        peak = max(peak, h[j])
        if l[j] <= stop:
            realized += part * (stop - fill) / R
            out = "止损" if not (be_done or partial_done) else ("保本离场" if realized < 0.5 else "部分止盈后离场")
            return _r(out, j, realized, peak, fill, be_done, hit_chart)
        if sig.chart_tp and h[j] >= sig.chart_tp:
            hit_chart = True
        if prof["partial_r"] and not partial_done and h[j] >= fill + prof["partial_r"] * R:
            realized += prof["partial_pct"] * prof["partial_r"]
            part -= prof["partial_pct"]
            partial_done, be_done, stop = True, True, max(stop, fill)
        if prof["partial_after_bars"] and not partial_done and j - i >= prof["partial_after_bars"] and c[j] > fill:
            realized += prof["partial_pct"] * (c[j] - fill) / R
            part -= prof["partial_pct"]
            partial_done, be_done, stop = True, True, max(stop, fill)
        if prof["be_r"] and not be_done and h[j] >= fill + prof["be_r"] * R:
            be_done, stop = True, max(stop, fill)
        if prof["tp_r"] and h[j] >= fill + prof["tp_r"] * R:
            realized += part * prof["tp_r"]
            return _r("计划止盈达成", j, realized, peak, fill, True, hit_chart)
        if ma is not None and (be_done or partial_done) and np.isfinite(ma[j]) and c[j] < ma[j]:
            realized += part * (c[j] - fill) / R
            return _r("跟踪止盈离场", j, realized, peak, fill, be_done, hit_chart)
    open_ = end == len(df) - 1 and end - i < cfg["max_hold_bars"]
    realized += part * (c[end] - fill) / R
    return _r("持仓中" if open_ else "到期离场", end, realized, peak, fill, be_done, hit_chart, open_)


def _r(outcome, j, r, peak, fill, be, chart, open_=False):
    return {"outcome": outcome, "exit_idx": j, "r": round(float(r), 2), "open": open_, "be_hit": be,
            "chart_tp_hit": chart, "max_gain_pct": round((peak / fill - 1) * 100, 1),
            "success": outcome == "计划止盈达成" or (not open_ and r >= 1)}

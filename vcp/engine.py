"""
多周期 VCP 引擎：日线判断阶段 + 执行周期（4h / 1h / 30m）找 Base、收缩和进场。
打分方式从师父的 9 个信号报告反推（见 README「SCV 扣分制」）。

流程：
  1. Stage（日线）：Minervini 趋势模板 → S2；上市不足 200 根日线标记 NEW
  2. Base（执行周期）：从起涨低点开始，回撤 ≥ base_min_depth 开始一个 Base，收盘创新高结束 → B1、B2、B3；
     B4 起失效（LATE）
  3. 每个 Base：C1 = 最高点 → 回调最低点；之后每段收缩低点必须更高（没抬高就并入前一段）；
     结构合格 = 至少 2 段、逐段变小、C1 ≤ 35%；当前 Base 末段低点守住一段时间并反弹过半 → CONFIRMED，否则 UNCONFIRMED
  4. Entry = 末段高点 × (1 + 挂单缓冲)；Stop = 末段低点 × (1 - 缓冲)；末段 > 10% 或入场风险 > 10% → 半仓
  5. SCV：S / C / V 各自加减分，每项正数 +1、0 记 0、负数 -1，合计 = Score（满分 3）
  6. 接近 Entry → SETUP；收盘站上 Entry → BREAKOUT

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
    "exec_bars": 900,               # 执行周期拉多少根K线（各周期见 TF_DEFAULTS）
    "stage_bars": 400,              # 日线拉 400 根
    # 阶段（日线）：Minervini 趋势模板
    "new_listing_bars": 200,        # 日线不足 200 根 = 新币 NEW（只用算得出来的均线）
    "ma_slow_rising_bars": 20,      # MA200 至少比 1 个月（20 根）前高；Minervini 更偏好 4~5 个月
    "min_above_low_pct": 0.30,      # 现价 ≥ 52 周低点 × 1.30
    "max_below_high_pct": 0.25,     # 现价 ≥ 52 周高点 × 0.75
    "min_rs": 70,                   # RS 相对强度（在本次扫描的所有币里排名百分位）< 70 → S 扣 1 分
    "advance_lookback": 365,        # 在最近 365 根日线里找起涨低点（也是数 Base 的起点）
    "advance_bonus_pct": 1.00,      # 前期涨幅门槛（报告里显示是否翻倍；require_advance 时变成硬性条件）
    "require_advance": False,       # true = 把“前期翻倍”变成硬性条件
    # Base / 收缩（4h）
    "base_min_depth": 0.08,         # 从高点回撤 ≥ 8% 才算开始一个 Base
    "base_min_bars": 10,            # Base 至少 10 根 4h K线
    "zigzag_pct": 0.03,
    "recovery_ratio": 0.35,         # 反弹收回上段跌幅 ≥ 35% 才算新的一段收缩
    "min_contractions": 2,
    "max_contractions": 6,
    "max_bases": 3,                 # B4 起判定失效（师父：B3 还能做，只是得分低）
    "max_first_depth": 0.35,        # C1 ≤ 35%（Minervini 原版）
    "half_size_last_c": 0.10,       # 最后一段 > 10% → 风险大，仓位减半（师父）
    "confirm_bars": 5,              # 末段低点之后至少守住 5 根K线、且反弹过末段一半 → Base CONFIRMED
    "contraction_tolerance": 0.10,
    "small_c1_pct": 0.20,           # C1 < 20% 视为“C1偏小”（洗盘不够深，-0.5）
    # 进场 / 计划
    "entry_buffer": 0.005,          # 挂单缓冲 0.5%
    "stop_buffer": 0.005,
    "exit_profile": "minervini",    # 出场方案：minervini / master / qullamaggie（见 PROFILES）
    "chart_tp_lookback": 180,       # 结构止盈：最近 N 根K线内的最高点（各周期见 TF_DEFAULTS）
    "near_entry_pct": 0.03,         # 现价在 Entry 下方 3% 内 = 接近入场
    "max_extension_pct": 0.03,      # 已高出 Entry 3% 以上不再提示（防追高）
    "breakout_lag_bars": 3,         # 最近 3 根 4h 内的突破都算
    # 成交量
    "volume_ma": 50,
    "vol_spike": 1.3,               # 成交额 ≥ MA50 × 1.3 = 放量K线
    "recent_vol_bars": 10,          # “近期”= 最近 10 根K线
    "vol_strong": 2.0,              # 最新成交额 ≥ 2 倍均线 = STRONG；< 1 倍 = WEAK
    "liquidity_cap_pct": 0.10,      # 单笔上限 = 最新 4h 成交额的 10%
    # 扣分项（师父的做法：走满分，慢慢扣分）
    "min_daily_volume": 1_000_000,  # 最近 24h 成交额 < 100 万 USDT → 流动性差，扣分
    "wick_lookback": 3,             # 检查最近 3 根 4h（含突破K线）有没有长上影线
    "wick_ratio": 0.5,              # 上影线 ≥ 整根K线长度的 50% 视为长上影线
    "wick_min_range": 0.5,          # K线长度至少是近 20 根平均长度的 0.5 倍才算（忽略很小的K线）
    # 评级（Score = S、C、V 三项各 +1/-1 的合计，满分 3）
    "min_rating": "GOOD",           # 低于这个评级不提醒：WEAK < FAIR < GOOD < STRONG < EXCELLENT
    "min_score": 1,                 # Score 低于这个分数不提醒
    "require_ma_stack": True,       # 执行周期必须 MA50 > MA150 > MA200（多头排列），否则不提醒
    "ma_stack_min_bars": 20,        # 多头排列要连续保持 ≥ 20 根K线，否则“刚排好/混乱”，S 扣分
    "ma_min_spread": 0.01,          # MA50 至少比 MA200 高 1%，否则“均线缠绕”，S 扣分
    "ma_messy_penalty": 1.0,        # 上面两项各扣 1 分
    "ma_slope_bars": 10,            # MA50 要比 10 根前高（向上）
    "ma_slope_penalty": 0.5,        # MA50 走平/向下扣 0.5
    # 仓位
    "account_usdt": 1000,
    "risk_per_trade_pct": 0.01,
    "max_leverage": 5,
    "max_risk_pct": 0.10,           # 入场风险 > 10% → 半仓（HALF_SIZE）
    # 历史回放
    "max_hold_bars": 180,           # 回放最多持有 N 根K线（各周期见 TF_DEFAULTS）
}

RATINGS = ["WEAK", "FAIR", "GOOD", "STRONG", "EXCELLENT"]
SCORE_RATING = {3: "STRONG", 2: "GOOD", 1: "GOOD", 0: "FAIR"}   # 师父：3 分 STRONG，1~2 分 GOOD

BARS_PER_DAY = {"15m": 96, "30m": 48, "1h": 24, "4h": 6, "1d": 1}
# 各执行周期的默认参数（师父：15m/30m 挂单缓冲 0.25%，1h/4h 0.5%）
TF_DEFAULTS = {
    "4h": {"exec_bars": 900, "entry_buffer": 0.005, "chart_tp_lookback": 180, "max_hold_bars": 180},
    "1h": {"exec_bars": 600, "entry_buffer": 0.005, "chart_tp_lookback": 240, "max_hold_bars": 240},
    "30m": {"exec_bars": 600, "entry_buffer": 0.0025, "chart_tp_lookback": 240, "max_hold_bars": 240},
    "15m": {"exec_bars": 600, "entry_buffer": 0.0025, "chart_tp_lookback": 240, "max_hold_bars": 240},
}


def tf_config(cfg: dict, tf: str) -> dict:
    """某个执行周期的完整参数：通用参数 + 周期默认值 + config.yaml 里 tf_overrides 的覆盖。"""
    out = {**cfg, **TF_DEFAULTS.get(tf, {}), **((cfg.get("tf_overrides") or {}).get(tf) or {})}
    out["exec_tf"] = tf
    return out

# 出场方案（R = 入场价 - 止损价）。历史回放对同一批入场三种都算，方便比较哪种在加密货币上更好。
PROFILES = {
    # Minervini：涨到 2R 把止损移到保本；3R 卖一半（逢强卖出）；剩下的收盘跌破“20 日均线”离场
    "minervini": {"name": "Minervini", "be_r": 2.0, "partial_r": 3.0, "partial_pct": 0.5,
                  "partial_after_days": None, "tp_r": None, "trail_days": 20},
    # 师父报告里的参数：0.8R 保本，5R 全部止盈
    "master": {"name": "师父(0.8R保本/5R止盈)", "be_r": 0.8, "partial_r": None, "partial_pct": 0,
               "partial_after_days": None, "tp_r": 5.0, "trail_days": None},
    # Qullamaggie 风格（公开资料整理，非官方）：3 天后若盈利先卖一半并保本，剩下跌破“10 日均线”离场
    "qullamaggie": {"name": "Qullamaggie风格", "be_r": None, "partial_r": None, "partial_pct": 0.5,
                    "partial_after_days": 3, "tp_r": None, "trail_days": 10},
}


def rating_of(score: int) -> str:
    """Score = S/C/V 三项的正负号合计（满分 3）：3 STRONG，1~2 GOOD，0 FAIR，负分 WEAK。"""
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
    ok: bool                 # 结构合格（≥2 段、逐段变小、C1 ≤ 35%、没超过最多 Base 数）
    status: str              # 已结束：OK / FAIL / WIDE / FORMING / LATE；当前：CONFIRMED / UNCONFIRMED / ...
    current: bool
    confirmed: bool = False

    def label(self) -> str:
        st = {"CONFIRMED": "OK", "UNCONFIRMED": "WAIT"}.get(self.status, self.status)
        return f"B{self.no} {st}({len(self.cons)}C)"


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
    tf: str = "4h"
    ma_stack: dict = field(default_factory=dict)

    def to_row(self) -> dict:
        b = self.base
        return {
            "symbol": self.symbol, "tf": self.tf, "status": self.status, "valid": self.valid, "rating": self.rating,
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
    if not shrink:
        return False, "FAIL"
    return True, "OK"


def _confirmed(df: pd.DataFrame, last_c, cfg: dict) -> bool:
    """末段低点之后已守住 confirm_bars 根，且收盘反弹过末段的一半 → 这个 Base 确认。"""
    n = len(df)
    if n - 1 - last_c.trough_idx < cfg["confirm_bars"]:
        return False
    after = df["close"].to_numpy()[last_c.trough_idx + 1:]
    if after.size == 0 or after.min() < last_c.trough:
        return False
    return bool(after.max() >= last_c.trough + (last_c.peak - last_c.trough) * 0.5)


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
        conf = False
        if current and ok:
            conf = _confirmed(df, cons[-1], cfg)
            st = "CONFIRMED" if conf else "UNCONFIRMED"
        bases.append(Base(no, b0, end, float(h[b0]), float(l[b0:end].min()), cons, ok, st, current, conf))
    return bases


# ----------------------------------------------------------------------------
# 3. 成交量
# ----------------------------------------------------------------------------
def volume_info(df: pd.DataFrame, cfg: dict) -> dict:
    v = df["volume"].to_numpy()
    vma_s = pd.Series(v).rolling(cfg["volume_ma"], min_periods=10).mean().to_numpy()
    vma = float(vma_s[-1]) if np.isfinite(vma_s[-1]) else 0.0
    o, c = df["open"].to_numpy(), df["close"].to_numpy()
    lv = float(v[-1])
    ratio = lv / vma if vma > 0 else 0.0
    level = "STRONG" if ratio >= cfg["vol_strong"] else ("NORMAL" if ratio >= 1.0 else "WEAK")
    spike = ratio >= cfg["vol_spike"]
    candle = ("BULL_EXPANSION" if c[-1] >= o[-1] else "BEAR_EXPANSION") if spike else "NEUTRAL"
    recent = float(v[-5:].mean())
    trend = "EXPANDING" if recent > vma * 1.1 else ("CONTRACTING" if recent < vma * 0.9 else "STABLE")
    # 近期（最近 recent_vol_bars 根）有没有放量阳线 / 放量阴线
    k = min(cfg["recent_vol_bars"], len(v))
    rv, rm = v[-k:], vma_s[-k:]
    big = np.where(np.isfinite(rm) & (rm > 0), rv / np.where(rm > 0, rm, 1), 0) >= cfg["vol_spike"]
    bull_spike = bool(np.any(big & (c[-k:] >= o[-k:])))
    bear_spike = bool(np.any(big & (c[-k:] < o[-k:])))
    per_day = BARS_PER_DAY.get(cfg.get("exec_tf", "4h"), 6)
    return {
        "last": lv, "ma": vma, "ratio": ratio, "level": level,
        "raw": lv / float(c[-1]) if c[-1] else 0.0,
        "est24h": vma * per_day, "day": float(v[-per_day:].sum()),
        "trend": trend, "candle": candle, "bull_spike": bull_spike, "bear_spike": bear_spike,
        "liq_cap": lv * cfg["liquidity_cap_pct"],
    }


# ----------------------------------------------------------------------------
# 4. 核心：对“当前这一刻”做完整分析（不含突破判断）
# ----------------------------------------------------------------------------
def _core(symbol: str, df: pd.DataFrame, d1: pd.DataFrame, cfg: dict) -> Signal:
    sig = Signal(symbol=symbol, tf=cfg.get("exec_tf", "4h"))
    sig.last_close = float(df["close"].iloc[-1])
    sig.stage = stage_info(d1, cfg)
    sig.ma_stack = ma_stack(df, cfg)
    if cfg["require_ma_stack"] and not sig.ma_stack["ok"]:
        sig.reasons.append(f"{sig.tf} 均线没有多头排列（{sig.ma_stack['text']}）")
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
    sig.valid = (sig.stage["s2"] and cur.ok and sig.last_close >= last_c.trough
                 and (sig.ma_stack["ok"] or not cfg["require_ma_stack"]))
    return sig


def ma_stack(df: pd.DataFrame, cfg: dict) -> dict:
    """执行周期的均线多头排列：MA50 > MA150 > MA200（师父：小周期的 VCP 必须在多头排列里）。
    价格不一定要在均线上方（师父 BTW/UAI/NMR 的例子价格都在 MA50 下），但三条均线的顺序必须是多头。
    另外检查“均线混乱”：多头排列刚形成 / 三条均线缠在一起 / MA50 走平向下（用于 S 扣分）。"""
    c = df["close"]
    ks = [k for k in (50, 150, 200) if len(df) >= k]
    if len(ks) < 2:
        return {"ok": True, "text": "K线太少，未检查", "vals": {}, "held": None, "spread": None,
                "fast_up": True, "messy": []}
    ma = {k: c.rolling(k).mean().to_numpy() for k in ks}
    vals = {k: float(ma[k][-1]) for k in ks}
    stacked = np.ones(len(df), dtype=bool)
    for a, b in zip(ks, ks[1:]):
        stacked &= ma[a] > ma[b]
    ok = bool(stacked[-1])
    # 多头排列已经连续保持了几根K线
    held = 0
    for x in stacked[::-1]:
        if not x:
            break
        held += 1
    spread = vals[ks[0]] / vals[ks[-1]] - 1                       # MA50 和最慢那条均线拉开多少
    k_slope = cfg["ma_slope_bars"]
    fast_up = bool(len(df) > ks[0] + k_slope and ma[ks[0]][-1] > ma[ks[0]][-1 - k_slope])
    messy = []
    if ok and held < cfg["ma_stack_min_bars"]:
        messy.append(f"多头排列才{held}根(需≥{cfg['ma_stack_min_bars']})")
    if ok and spread < cfg["ma_min_spread"]:
        messy.append(f"均线缠绕(MA{ks[0]}只比MA{ks[-1]}高{spread * 100:.1f}%)")
    if ok and not fast_up:
        messy.append(f"MA{ks[0]}走平/向下")
    if ok:
        text = " > ".join(f"MA{k}" for k in ks) + f"，已保持{held}根，MA{ks[0]}高于MA{ks[-1]} {spread * 100:.1f}%"
    else:
        text = " / ".join(f"MA{k} {vals[k]:.6g}" for k in ks)
    return {"ok": ok, "text": text, "vals": vals, "held": held, "spread": spread, "fast_up": fast_up,
            "messy": messy}


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


def c_points(b: Base, breakout: bool, cfg: dict) -> tuple[float, list]:
    """C 收缩分（不含长上影线）。"""
    cc, cr = 0.0, []
    c1 = b.cons[0].depth
    if c1 < cfg["small_c1_pct"]:
        cc -= 0.5
        cr.append("C1偏小")
    else:
        cr.append(f"C1={c1 * 100:.1f}%")
    if b.confirmed:
        cc += 1.0
        cr.append("Base确认")
    else:
        cc += 0.5
        cr.append("Base未确认")
    rank_pts = {1: 0.5, 2: 1.5, 3: 0.0}
    if b.no in rank_pts and b.no <= cfg["max_bases"]:
        cc += rank_pts[b.no]
        cr.append(f"第{b.no}Base")
    else:
        cc -= 2.0
        cr.append(f"第{b.no}Base(太晚) -2")
    if breakout:
        cc += 1.0
        cr.append("Pivot突破")
    return cc, cr


def v_points(bull_spike: bool, bear_spike: bool, day_volume: float, cfg: dict) -> tuple[float, list]:
    """V 成交量分：近期阳放 +1；阴足放量 -2；两个都没有 = 0（无量，拿不到这 1 分）；日成交额太小 -2。"""
    vv, vr = 0.0, []
    if bull_spike:
        vv += 1.0
        vr.append("近期阳放")
    if bear_spike:
        vv -= 2.0
        vr.append("阴足放量")
    if day_volume < cfg["min_daily_volume"]:
        vv -= 2.0
        vr.append(f"日成交额{day_volume / 1e6:.2f}M<{cfg['min_daily_volume'] / 1e6:g}M 流动性差 -2")
    return vv, vr


def _score(sig: Signal, df: pd.DataFrame, cfg: dict, breakout: bool) -> None:
    """
    SCV（从师父 9 个报告反推，每个例子都对得上）：
      S 结构：Stage2 +1.0（不在 Stage2 -2.0）；执行周期收盘 > MA50 > MA150 > MA200（Close确认排列）+0.5
              （均线本身必须多头排列，否则直接不提醒，见 ma_stack）
      C 收缩：C1 在 20%~35% 记 0，C1偏小（<20%）-0.5；Base确认 +1.0 / 未确认 +0.5；
              第1Base +0.5、第2Base +1.5、第3Base 0；Pivot突破 +1.0；长上影线 -1.0
      V 成交量：近期阳放 +1.0；阴足放量 -2.0；都没有 = 0（“无量”就拿不到这 1 分）；日成交额 < 100 万 -2.0
    每项正数 +1、0 记 0、负数 -1，合计 = Score（满分 3）
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
    # 均线混乱（多头排列刚形成 / 缠绕 / MA50 走平）：每一项都扣分
    stack = sig.ma_stack or ma_stack(df, cfg)
    for m in stack.get("messy", []):
        pts = cfg["ma_slope_penalty"] if m.endswith("走平/向下") else cfg["ma_messy_penalty"]
        s -= pts
        sr.append(f"{m} -{pts:g}")
    # C 收缩
    b = sig.base
    cc, cr = c_points(b, breakout, cfg) if b and b.cons else (0.0, [])
    wick = upper_wick(df, cfg)
    if wick is not None:
        cc -= 1.0
        cr.append(f"长上影线({'最新K线' if wick == 0 else f'{wick}根前'}) -1")
    # V 成交量
    vi = volume_info(df, cfg)
    sig.vol = vi
    vv, vr = v_points(vi["bull_spike"], vi["bear_spike"], vi["day"], cfg)
    sig.s, sig.c, sig.v = (s, sr), (cc, cr), (vv, vr)
    _finalize(sig)


def passes(sig: Signal, cfg: dict) -> bool:
    """评级和分数都达标才做（纪律：低分不做）。"""
    return RATINGS.index(sig.rating) >= RATINGS.index(cfg["min_rating"]) and sig.score >= cfg["min_score"]


def _finalize(sig: Signal) -> None:
    sig.total = round(sig.s[0] + sig.c[0] + sig.v[0], 2)
    sig.score = _sign(sig.s[0]) + _sign(sig.c[0]) + _sign(sig.v[0])
    sig.rating = rating_of(sig.score)


def _size(sig: Signal, cfg: dict) -> None:
    if sig.entry is None or not sig.risk_pct:
        return
    tag = "NEW_LISTING" if sig.stage.get("new") else "MATURE"
    factor, rule = 1.0, "NORMAL_SIZE"
    last_c = sig.base.cons[-1].depth if sig.base and sig.base.cons else 0
    if not passes(sig, cfg):
        factor, rule = 0.0, "SKIP"
    elif sig.risk_pct > cfg["max_risk_pct"] or last_c > cfg["half_size_last_c"]:
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
    if sig.status != "NONE" and not passes(sig, cfg):
        sig.reasons.append(f"Score {sig.score}（{sig.rating}）未达标")
        sig.status = "NONE"
    return sig


def apply_rs(sig: Signal, rs: int, cfg: Optional[dict] = None) -> None:
    """扫描完所有币后补上 RS 排名：RS < min_rs（弱于大部分币）→ S 扣 1 分。"""
    cfg = {**DEFAULTS, **(cfg or {})}
    sig.rs = rs
    ok = rs >= cfg["min_rs"]
    sig.stage.setdefault("checks", {})[f"RS≥{cfg['min_rs']}"] = ok
    if not ok:
        s, sr = sig.s
        sig.s = (s - 1.0, sr + [f"RS{rs}偏弱 -1"])
        _finalize(sig)
    _size(sig, cfg)
    if sig.status != "NONE" and not passes(sig, cfg):
        sig.reasons.append(f"Score {sig.score}（{sig.rating}）未达标")
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
                       "taken": passes(sig, cfg),
                       "by_profile": by, **res})
        i = res["exit_idx"] + 1
    return trades


def simulate(df: pd.DataFrame, i: int, fill: float, sig: Signal, cfg: dict, prof: Optional[dict] = None) -> dict:
    """按出场方案模拟。同一根K线既碰止损又碰目标时，保守地算先止损。"""
    prof = prof or PROFILES[cfg["exit_profile"]]
    h, l, c = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    per_day = BARS_PER_DAY.get(cfg.get("exec_tf", "4h"), 6)
    trail = prof["trail_days"] * per_day if prof["trail_days"] else None
    after = prof["partial_after_days"] * per_day if prof["partial_after_days"] else None
    ma = df["close"].rolling(trail).mean().to_numpy() if trail else None
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
        if after and not partial_done and j - i >= after and c[j] > fill:
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

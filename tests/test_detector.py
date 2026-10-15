import numpy as np

from tests.synthetic import make_downtrend, make_two_base_4h
from vcp.detector import zigzag
from vcp.engine import DEFAULTS, PROFILES, analyze, apply_rs, replay, simulate


def test_zigzag_alternates():
    h = np.array([10, 9, 8, 9.5, 10.5, 9, 8.8, 10])
    p = zigzag(h, h, 0.05)
    types = [t for _, _, t in p]
    assert all(a != b for a, b in zip(types, types[1:]))


def test_two_bases_like_master_chart():
    df, d1 = make_two_base_4h()
    s = analyze("X", df, d1)
    assert [b.no for b in s.bases] == [1, 2]
    b1, b2 = s.bases
    assert b1.ok and len(b1.cons) == 2 and 0.10 < b1.cons[0].depth < 0.16
    assert b2.current and b2.status == "CONFIRMED"
    assert 0.15 < b2.cons[0].depth < 0.19 and b2.cons[-1].depth < 0.08
    assert s.stage["label"] == "NEW/S2"
    assert s.status == "SETUP"
    assert s.stop < b2.cons[-1].trough < b2.cons[-1].peak < s.entry < s.be < s.plan_tp
    R = s.entry - s.stop
    assert abs((s.be - s.entry) / R - 2) < 1e-9          # Minervini：+2R 保本
    assert abs((s.plan_tp - s.entry) / R - 3) < 1e-9     # Minervini：+3R 卖一半
    m = analyze("X", df, d1, {"exit_profile": "master"})
    assert abs((m.plan_tp - m.entry) / (m.entry - m.stop) - 5) < 1e-9


def test_breakout_scores_pivot_and_volume():
    df, d1 = make_two_base_4h(breakout=True)
    s = analyze("X", df, d1)
    assert s.status == "BREAKOUT"
    assert "Pivot突破" in s.c[1]
    assert s.c[0] == 3.0                      # C1偏小 + Base确认 + 第2Base + Pivot突破，和师父的例子一致
    assert s.rating in ("GOOD", "EXCELLENT")


def test_position_size_risks_one_percent():
    df, d1 = make_two_base_4h()
    s = analyze("X", df, d1, {"account_usdt": 1000, "risk_per_trade_pct": 0.01, "liquidity_cap_pct": 100})
    assert abs(s.size["loss"] - 10) < 0.01


def test_downtrend_rejected():
    df = make_downtrend()
    s = analyze("X", df, df)
    assert not s.valid and s.status == "NONE"


def test_replay_and_simulate():
    df, d1 = make_two_base_4h(breakout=True)
    trades = replay("X", df, d1, 300)
    assert any(t["idx"] == len(df) - 1 for t in trades), "最后一根突破应在回放中触发"
    # 手工验证模拟：入场后直接涨到 5R → 计划止盈
    s = analyze("X", df.iloc[:-1], d1, {"exit_profile": "master"})
    fake = df.copy()
    fake.loc[len(fake) - 1, "high"] = s.plan_tp * 1.01
    res = simulate(fake, len(fake) - 1, s.entry, s, DEFAULTS, PROFILES["master"])
    assert res["outcome"] == "计划止盈达成" and abs(res["r"] - 5) < 1e-6
    assert set(trades[0]["by_profile"]) == set(PROFILES)


def test_advance_rule_optional():
    df, d1 = make_two_base_4h()
    s = analyze("X", df, d1)
    assert s.stage["s2"] and s.stage["adv"] >= 1.0
    s = analyze("X", df, d1, {"require_advance": True, "advance_bonus_pct": 2.0})   # 硬性要求涨 200%
    assert not s.stage["s2"] and s.status == "NONE"


def test_lower_low_merges_into_previous_contraction():
    from vcp.detector import Contraction
    from vcp.engine import merge_higher_lows
    vol = np.ones(50)
    cons = [Contraction(0, 100, 5, 80, 0.20, 1), Contraction(10, 95, 15, 85, 0.105, 1),
            Contraction(20, 92, 25, 78, 0.152, 1), Contraction(30, 90, 35, 86, 0.044, 1)]
    out = merge_higher_lows(cons, vol)
    # 第 3 段低点 78 < 80 → 前三段合并成 C1：100 → 78；第 4 段低点 86 更高 → C2
    assert [(c.peak, c.trough) for c in out] == [(100, 78), (90, 86)]
    assert abs(out[0].depth - 0.22) < 1e-9


def test_more_than_max_bases_is_invalid():
    df, d1 = make_two_base_4h()
    s = analyze("X", df, d1, {"max_bases": 1})
    assert s.base.no == 2 and s.base.status == "LATE"
    assert not s.valid and s.status == "NONE"


def test_minervini_partial_then_trailing_exit():
    df, d1 = make_two_base_4h()
    s = analyze("X", df, d1)
    R = s.entry - s.stop
    i = len(df)
    up = [s.entry + k * R for k in (0.5, 1.5, 2.5, 3.2, 3.6, 3.4)]
    rows = [{"time": df["time"].iloc[-1], "open": p, "high": p * 1.002, "low": p * 0.998, "close": p,
             "volume": 1e5} for p in up]
    rows.append({"time": df["time"].iloc[-1], "open": s.entry + 3.4 * R, "high": s.entry + 3.4 * R,
                 "low": s.entry - 0.5 * R, "close": s.entry - 0.3 * R, "volume": 1e5})
    import pandas as pd
    fake = pd.concat([df, pd.DataFrame(rows)], ignore_index=True)
    fake["close"] = fake["close"].astype(float)
    res = simulate(fake, i, s.entry, s, DEFAULTS, PROFILES["minervini"])
    # +2R 止损移到保本，+3R 卖一半（+1.5R），之后跌回入场价 → 剩下一半保本离场，合计 +1.5R
    assert res["outcome"] == "部分止盈后离场"
    assert abs(res["r"] - 1.5) < 1e-6


def test_risk_over_10pct_is_skipped():
    df, d1 = make_two_base_4h()
    s = analyze("X", df, d1, {"max_risk_pct": 0.03})
    assert not s.valid and s.status == "NONE" and s.size["rule"].endswith("SKIP")


def test_low_rs_demotes_signal():
    df, d1 = make_two_base_4h()
    s = analyze("X", df, d1)
    assert s.status == "SETUP"
    apply_rs(s, 40)
    assert s.status == "NONE" and not s.stage["s2"] and s.rs == 40

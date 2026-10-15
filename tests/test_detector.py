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
    assert "Pivot突破" in s.c[1] and "近期阳放" in s.v[1]
    # C1偏小(-0.5) + Base确认(+1) + 第2Base(+1.5) + Pivot突破(+1) = 3.0，和师父 CTRUSDT 一样
    assert s.c[0] == 3.0


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


def test_risk_over_10pct_is_half_size():
    df, d1 = make_two_base_4h()
    s = analyze("X", df, d1, {"max_risk_pct": 0.03})
    assert s.valid and s.size["rule"].endswith("HALF_SIZE")


def test_low_rs_is_deducted():
    df, d1 = make_two_base_4h()
    s = analyze("X", df, d1)
    before = s.s[0]
    apply_rs(s, 40)
    assert s.rs == 40 and s.s[0] == before - 1.0 and any("RS40" in x for x in s.s[1])


def test_score_is_sign_sum_like_master_example():
    from vcp.engine import Signal, _finalize
    s = Signal("X")
    s.s, s.c, s.v = (1.5, []), (3.0, []), (-2.0, [])          # 师父的 CTRUSDT：S+ C+ V-
    _finalize(s)
    assert s.score == 1 and s.rating == "GOOD"


def test_full_marks_breakout():
    df, d1 = make_two_base_4h(breakout=True)
    s = analyze("X", df, d1)
    assert s.status == "BREAKOUT" and s.score == 3 and s.rating == "STRONG"


def test_breakout_without_volume_gets_no_v_point():
    df, d1 = make_two_base_4h(breakout=True, breakout_vol=1e5)    # 突破K线成交额远低于 MA50
    s = analyze("X", df, d1)
    assert s.v[0] <= 0 and s.score <= 2                             # 无量 → V 拿不到 +1


def test_long_upper_wick_is_deducted():
    df, d1 = make_two_base_4h(breakout=True, wick=True)
    s = analyze("X", df, d1)
    assert any("长上影线" in x for x in s.c[1])


def test_low_daily_volume_is_deducted():
    df, d1 = make_two_base_4h()
    df = df.copy()
    df["volume"] = df["volume"] / 20                               # 日成交额 ≈ 12 万
    s = analyze("X", df, d1)
    assert any("流动性差" in x for x in s.v[1])


def test_replay_records_score_buckets():
    df, d1 = make_two_base_4h(breakout=True, follow=40)
    trades = replay("X", df, d1, 300)
    assert trades and all("score" in t and "taken" in t for t in trades)


# 师父的 7 个做多信号（MUUUSDT 是做空，暂不做）：(C1, Base确认?, 第几个Base, Pivot突破?, 近期阳放?, 阴足放量?, 收盘排列?) → (S, C, V, Score)
MASTER = {
    "AERO": (0.207, True, 1, True, True, False, True, (1.5, 2.5, 1.0, 3)),
    "SOON": (0.214, False, 3, True, False, False, True, (1.5, 1.5, 0.0, 2)),
    "PHA": (0.256, False, 1, False, False, False, True, (1.5, 1.0, 0.0, 2)),
    "BTW": (0.327, False, 3, False, False, True, False, (1.0, 0.5, -2.0, 1)),
    "CTR": (0.168, True, 2, True, False, True, True, (1.5, 3.0, -2.0, 1)),
    "UAI": (0.220, False, 1, True, False, True, False, (1.0, 2.0, -2.0, 1)),
    "NMR": (0.216, False, 1, False, False, False, False, (1.0, 1.0, 0.0, 2)),
}


def test_scoring_matches_all_master_examples():
    from vcp.detector import Contraction
    from vcp.engine import Base, Signal, _finalize, c_points, v_points
    cfg = DEFAULTS
    for name, (c1, conf, no, pivot, bull, bear, aligned, (S, C, V, score)) in MASTER.items():
        b = Base(no, 0, 10, 1, 0.5, [Contraction(0, 1, 5, 1 - c1, c1, 1)], True,
                 "CONFIRMED" if conf else "UNCONFIRMED", True, conf)
        cc, _ = c_points(b, pivot, cfg)
        vv, _ = v_points(bull, bear, 1e8, cfg)
        sig = Signal(name)
        sig.s, sig.c, sig.v = (1.0 + (0.5 if aligned else 0), []), (cc, []), (vv, [])
        _finalize(sig)
        assert (sig.s[0], cc, vv, sig.score) == (S, C, V, score), name
        assert sig.rating == ("STRONG" if score == 3 else "GOOD"), name


def test_multi_timeframe_config():
    from vcp.engine import tf_config
    c30 = tf_config(DEFAULTS, "30m")
    assert c30["exec_tf"] == "30m" and c30["entry_buffer"] == 0.0025
    assert tf_config(DEFAULTS, "4h")["entry_buffer"] == 0.005
    df, d1 = make_two_base_4h()
    s = analyze("X", df, d1, c30)
    assert s.tf == "30m" and abs(s.vol["est24h"] - s.vol["ma"] * 48) < 1e-6


def test_every_exchange_supports_every_timeframe():
    # 回归测试：曾经因为数据接口没有 30m 导致整次扫描失败
    from vcp.data import INTERVAL_MS, INTERVALS
    from vcp.engine import TF_DEFAULTS
    for tf in list(TF_DEFAULTS) + ["1d"]:
        assert tf in INTERVAL_MS, tf
        for ex, m in INTERVALS.items():
            assert tf in m, (ex, tf)


def test_one_failing_timeframe_keeps_the_others(monkeypatch):
    import scanner
    from tests.synthetic import make_two_base_4h
    df, d1 = make_two_base_4h()

    class Ex:
        def klines(self, s, tf, n):
            if tf == "30m":
                raise KeyError("30m")
            return d1 if tf == "1d" else df
    monkeypatch.setattr(scanner, "drop_unclosed", lambda d, tf: d)
    cfg = scanner.load_config("config.yaml")
    out = scanner.analyse(Ex(), "X", cfg)
    assert [tf for tf, *_ in out] == ["4h", "1h"]


def test_exec_ma_stack_required():
    # PLTRUSDT 30m：日线是 Stage 2，但 30m 的 MA50 < MA150 < MA200 → 不能出信号（师父：均线没有多头排列）
    import pandas as pd
    from vcp.engine import ma_stack
    df, d1 = make_two_base_4h()
    assert ma_stack(df, DEFAULTS)["ok"]
    bear = df.copy()
    for k in ("open", "high", "low", "close"):
        bear[k] = df[k].to_numpy()[::-1]                         # 价格倒过来 = 一路下跌，均线空头排列
    bear["high"], bear["low"] = bear[["high", "low"]].max(axis=1), bear[["high", "low"]].min(axis=1)
    assert not ma_stack(bear, DEFAULTS)["ok"]
    s = analyze("X", bear, d1)
    assert s.status == "NONE" and not s.valid
    assert any("均线没有多头排列" in r for r in s.reasons)
    s = analyze("X", df, d1)
    assert s.ma_stack["ok"] and s.status == "SETUP"

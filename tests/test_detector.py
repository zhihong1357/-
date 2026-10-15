from tests.synthetic import make_downtrend, make_vcp
from vcp.detector import detect_vcp, zigzag


def test_vcp_setup_detected():
    r = detect_vcp(make_vcp(), "X")
    assert r.valid, r.reasons
    assert r.status == "SETUP"
    d = [c.depth for c in r.contractions]
    assert len(d) >= 3
    assert d[0] > d[-1]
    assert 0.25 < d[0] < 0.35


def test_vcp_breakout_detected():
    r = detect_vcp(make_vcp(breakout=True), "X")
    assert r.status == "BREAKOUT", r.reasons
    assert r.breakout_volume_ratio >= 1.5


def test_downtrend_rejected():
    r = detect_vcp(make_downtrend(), "X")
    assert not r.valid
    assert r.status == "NONE"


def test_zigzag_alternates():
    import numpy as np
    h = np.array([10, 9, 8, 9.5, 10.5, 9, 8.8, 10])
    p = zigzag(h, h, 0.05)
    types = [t for _, _, t in p]
    assert all(a != b for a, b in zip(types, types[1:]))


def test_trade_plan_levels():
    from vcp.trade import make_plan
    r = detect_vcp(make_vcp(), "X")
    p = make_plan(r, {"account_usdt": 1000, "risk_per_trade_pct": 0.01})
    assert p.stop < r.contractions[-1].trough < r.pivot < p.entry < p.tp1 < p.tp2
    assert abs(p.risk_usdt - 10) < 0.01 or "封顶" in p.warn
    assert abs((p.tp2 - p.entry) / (p.entry - p.stop) - 3) < 1e-9


def test_validation_finds_successful_breakout():
    from vcp.detector import DEFAULTS
    from vcp.trade import validate_symbol
    df = make_vcp(breakout=True, follow=25)
    trades = validate_symbol(df, "X", DEFAULTS, {}, {"lookback_bars": 60})
    assert trades, "should replay the historical breakout"
    t = trades[0]
    assert t["tp1_hit"] and t["r"] > 0

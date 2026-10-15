"""把分析结果写成和师父一样的信号报告（中英对照）。"""
from __future__ import annotations

import pandas as pd

from .engine import BARS_PER_DAY, PROFILES
from .chart import display_symbol, fmoney, fp  # noqa: F401  (display_symbol 供 scanner 使用)

STATUS_HEAD = {
    "BREAKOUT": "🚀 {kind} · 突破",
    "SETUP": "👀 {kind} · 接近入场",
    "NONE": "SIGNAL CHECK / 信号检查",
}


def signal_text(sig, cfg: dict, ex_name: str, head: str | None = None, new: bool = True) -> str:
    b = sig.base
    st = sig.stage
    hist = " -> ".join(x.label() for x in sig.bases) or "-"
    # 和师父一样：已结束的 Base 写 B1C1，当前还没确认的 Base 直接写 C1、C2
    cdet = " | ".join(" ".join(f"{'' if (x.current and not x.confirmed) else f'B{x.no}'}C{i + 1}:{c.depth * 100:.1f}%"
                               for i, c in enumerate(x.cons)) for x in sig.bases if x.cons) or "-"
    kind = "NEW SIGNAL / 新信号" if new else "SIGNAL UPDATE / 信号更新"
    v = sig.vol or {}
    sz = sig.size or {}
    prof = PROFILES[cfg["exit_profile"]]
    tp_note = "" if prof["tp_r"] else f"，卖 {prof['partial_pct'] * 100:.0f}%"
    lines = [
        head or STATUS_HEAD.get(sig.status, STATUS_HEAD["NONE"]).format(kind=kind),
        "",
        f"{display_symbol(sig.symbol)}  ({ex_name})",
        f"Exec TF 执行周期: {cfg['exec_tf']}",
        f"Stage TF 阶段周期: {cfg['stage_tf']}",
        "Direction 方向: LONG",
        f"Rating 评级: {sig.rating}",
        f"Score 分数: {sig.score}  (S{_sign_txt(sig.s[0])} C{_sign_txt(sig.c[0])} V{_sign_txt(sig.v[0])}；"
        f"满分 3；需 ≥ {cfg['min_score']})",
        "",
        f"Stage 阶段: {st.get('label', '-')} ({st.get('bars', 0)} D1 bars)",
        "Trend Template 趋势模板: " + (" ".join(f"{'✅' if v else '❌'}{k}" for k, v in st.get("checks", {}).items()) or "-"),
        f"RS 相对强度: {sig.rs if sig.rs is not None else '-'} (< {cfg['min_rs']} 扣分，在本次扫描的币中排名百分位)",
        f"Advance 前期涨幅: {st.get('adv', 0) * 100:+.0f}% (${fp(st.get('adv_low'))} → ${fp(st.get('adv_high'))})"
        f"{' 翻倍 ✅' if st.get('adv', 0) >= cfg['advance_bonus_pct'] else ''}",
        f"Exec MA 执行周期均线: {'✅ 多头排列 ' if sig.ma_stack.get('ok') else '❌ 没有多头排列 '}"
        f"({sig.ma_stack.get('text', '-')})",
        f"Pattern 收缩: {'-'.join(f'C{i + 1}' for i in range(len(b.cons))) if b else '-'}",
        f"Base Rank 基地编号: B{b.no} ({b.status})" if b else "Base Rank 基地编号: -",
        (f"Last C 末段收缩: {b.cons[-1].depth * 100:.1f}%"
         f"{'（> ' + format(cfg['half_size_last_c'] * 100, '.0f') + '% 风险大，仓位减半）' if b.cons[-1].depth > cfg['half_size_last_c'] else ''}")
        if b and b.cons else "Last C 末段收缩: -",
        f"Base History 基地历史: {hist}",
        f"Base C 明细: {cdet}",
        f"Base 基地: ${fp(b.high)} - ${fp(b.low)} ({b.status})" if b else "Base 基地: -",
        "",
        "SCV 拆解",
        _scv("S 结构", sig.s),
        _scv("C 收缩", sig.c),
        _scv("V 成交量", sig.v),
    ]
    if sig.entry:
        lines += [
            "",
            f"Entry 入场: ${fp(sig.entry)}",
            f"Stop 止损: ${fp(sig.stop)}",
            f"Exit Plan 出场方案: {prof['name']}",
            f"BE 保本: ${fp(sig.be)} (+{prof['be_r']:g}R)" if sig.be else "BE 保本: 减仓后止损移到入场价",
            f"Plan TP 计划止盈: ${fp(sig.plan_tp)} (+{sig.plan_rr:g}R{tp_note})" if sig.plan_tp
            else "Plan TP 计划止盈: 无固定目标（跟踪均线离场）",
            f"Chart TP 结构止盈: ${fp(sig.chart_tp)}" if sig.chart_tp else "Chart TP 结构止盈: - (已在高位，上方无前高)",
            f"Buffer 挂单缓冲: {cfg['entry_buffer'] * 100:.2f}%",
            f"Risk 入场风险: {sig.risk_pct * 100:.2f}%",
            f"Plan R:R 计划盈亏比: {sig.plan_rr:.2f}" if sig.plan_rr else "Plan R:R 计划盈亏比: - (跟踪离场)",
            f"Structure R:R 结构盈亏比: {sig.struct_rr:.2f}" if sig.struct_rr else "Structure R:R 结构盈亏比: -",
            "",
            f"Size Rule 仓位规则: {sz.get('rule', '-')}",
            f"Position 仓位: 名义 {fmoney(sz.get('notional'))} ≈ {fqty(sz.get('qty', 0))} 个 | "
            f"{sz.get('leverage', 0):.0f}x | 保证金 {fmoney(sz.get('margin'))}",
            f"Loss at Stop 止损亏损: {fmoney(sz.get('loss'))} "
            f"(账户 {cfg['account_usdt']:g}U × {cfg['risk_per_trade_pct'] * 100:g}%)",
        ]
        if sz.get("notes"):
            lines.append("  ⚠️ " + "；".join(sz["notes"]))
    lines += [
        f"Last {cfg['exec_tf']} Volume 最新成交额: {fmoney(v.get('last'))} ({v.get('level', '-')})",
        f"Vol MA50 成交额均线: {fmoney(v.get('ma'))} | Ratio 倍率: {v.get('ratio', 0):.2f}x",
        f"Raw Volume 原始成交量: {v.get('raw', 0):,.0f}",
        f"Est 24h Volume 估算24h: {fmoney(v.get('est24h'))}",
        f"Vol Trend 量能趋势: {v.get('trend', '-')} | Candle K线量: {v.get('candle', '-')}",
        f"24h Volume 最近24h成交额: {fmoney(v.get('day'))} (需 ≥ {fmoney(cfg['min_daily_volume'])})",
        f"Liquidity Cap 单笔上限: {fmoney(v.get('liq_cap'))} ({'OK' if sz.get('cap_ok', True) else 'CAPPED'})",
        f"Near Entry 接近入场: {'YES' if sig.near_entry else 'NO'}",
    ]
    if sig.entry:
        lines += [
            "",
            "进场逻辑: " + (f"{cfg['exec_tf']} 收盘已站上 Entry，可按计划进场（价格已超出 Entry "
                         f"{cfg['max_extension_pct'] * 100:.0f}% 以上则不追）" if sig.status == "BREAKOUT" else
                         f"在 Entry 挂突破单（buy stop）；{cfg['exec_tf']} 收盘没站上 Entry 或跌破 Stop 则撤单"),
            "出场逻辑: " + exit_logic(prof, cfg),
        ]
    lines.append("⚠️ 规则化计算结果，非投资建议")
    return "\n".join(lines)


def exit_logic(prof: dict, cfg: dict) -> str:
    tf = cfg["exec_tf"]
    parts = ["跌破 Stop 全部离场"]
    if prof["partial_after_days"]:
        parts.append(f"持有 {prof['partial_after_days']} 天后若盈利，先卖 {prof['partial_pct'] * 100:.0f}% 并把止损移到入场价")
    if prof["be_r"]:
        parts.append(f"涨到 +{prof['be_r']:g}R 把止损移到入场价（保本）")
    if prof["partial_r"]:
        parts.append(f"涨到 +{prof['partial_r']:g}R 卖 {prof['partial_pct'] * 100:.0f}%（逢强卖出）")
    if prof["tp_r"]:
        parts.append(f"涨到 +{prof['tp_r']:g}R 全部止盈")
    if prof["trail_days"]:
        n = prof["trail_days"] * BARS_PER_DAY.get(tf, 6)
        parts.append(f"剩余仓位 {tf} 收盘跌破 MA{n}（≈ {prof['trail_days']} 日均线）离场")
    parts.append(f"持仓满 {cfg['max_hold_bars']} 根 {tf} 未结束按收盘离场；Chart TP 是上方前高阻力，可考虑减仓")
    return "；".join(parts)


def fqty(q: float) -> str:
    return f"{q:,.0f}" if q >= 100 else f"{q:.4g}"


def _sign_txt(x: float) -> str:
    return "+1" if x > 0 else ("-1" if x < 0 else "0")


def _scv(name: str, part) -> str:
    val, why = part
    sign = "+" if val > 0 else ("-" if val < 0 else "0")
    return f"{name}: {sign} ({val:+.1f}) | {', '.join(why) if why else '-'}"


def case_text(t: dict, df: pd.DataFrame, cfg: dict, ex_name: str) -> str:
    sig = t["sig"]
    tf = cfg["exec_tf"]
    d0 = pd.Timestamp(t["time"]).strftime("%Y-%m-%d %H:%M")
    d1 = pd.Timestamp(df["time"].iloc[t["exit_idx"]]).strftime("%Y-%m-%d %H:%M")
    bars = t["exit_idx"] - t["idx"]
    if t["open"]:
        head = "⏳ HISTORY 历史案例 · 仍在持仓中"
        res = [f"现价: ${fp(float(df['close'].iloc[-1]))}   浮动结果: {t['r']:+.2f}R   已持有 {bars} 根 {tf}",
               f"期间最大涨幅: {t['max_gain_pct']}%   BE {'已到达(止损已移到入场价)' if t['be_hit'] else '未到达'}"]
    else:
        head = "✅ HISTORY 历史案例 · 已验证成功" if t["success"] else "❌ HISTORY 历史案例 · 失败/保本"
        res = [f"结果: {t['outcome']}  {t['r']:+.2f}R   离场 {d1}（持有 {bars} 根 {tf}）",
               f"期间最大涨幅: {t['max_gain_pct']}%   Chart TP {'已到达' if t['chart_tp_hit'] else '未到达'}"]
    top = [head, f"触发时间: {d0} UTC   实际成交价: ${fp(t['fill'])}", *res, "",
           "—— 当时的信号报告 ——"]
    body = signal_text(sig, cfg, ex_name, head="SIGNAL (当时) / 历史信号")
    return "\n".join(top) + "\n" + body.replace("⚠️ 规则化计算结果，非投资建议", "⚠️ 历史案例用于检验规则，不代表未来结果")
